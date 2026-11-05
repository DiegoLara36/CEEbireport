import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from etl.normalization import canonicalize_course_url
from etl.schemas import (
    ObservationStatus,
    PriceStatus,
    ScrapedCourseDTO,
    ScrapedOfferingDTO,
)

logger = logging.getLogger(__name__)

SUPPORTED_CURRENCIES = {"PEN", "USD"}
AUTHORITATIVE_STATUSES = {
    ObservationStatus.OBSERVED.value,
    ObservationStatus.NOT_FOUND.value,
}


def _json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value), ensure_ascii=False, default=str, sort_keys=True
    )


def _canonical_value(value: Any, key: str | None = None) -> Any:
    decimal_fields = {
        "amount",
        "duration_hours",
        "duration_weeks",
        "list_amount",
        "final_amount",
        "value",
        "interest_rate",
        "installment_amount",
        "total_amount",
    }
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {field: _canonical_value(item, field) for field, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if key in decimal_fields and isinstance(value, str):
        try:
            return format(Decimal(value).normalize(), "f")
        except (InvalidOperation, ValueError):
            return value
    return value


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _course_fingerprint(attributes: dict, canonical_url: str) -> str:
    content = {
        key: value
        for key, value in attributes.items()
        if key not in {"raw_payload", "course_url"}
    }
    content["canonical_url"] = canonical_url
    return _fingerprint(content)


def _offering_fingerprint(attributes: dict) -> str:
    content = {
        key: value
        for key, value in attributes.items()
        if key not in {"schedule_text", "modality_raw"}
    }
    return _fingerprint(content)


def _dump_model(model) -> dict:
    return model.model_dump(mode="json")


def _preserve_unobserved(incoming: dict, previous: dict | None, groups: dict[str, str]) -> dict:
    if not previous:
        return incoming
    result = incoming.copy()
    for data_field, status_field in groups.items():
        if result.get(status_field) not in AUTHORITATIVE_STATUSES:
            for key in (data_field, status_field):
                if key in previous:
                    result[key] = previous[key]
    return result


def _sort_people(attributes: dict) -> None:
    for field in ("professors", "contacts"):
        if field in attributes:
            attributes[field] = sorted(
                attributes[field],
                key=lambda item: _json(item),
            )


def _course_duration_values(attributes: dict) -> tuple[Any, Any, Any]:
    hours = attributes.get("duration_hours")
    weeks = None
    sessions = None
    for duration in attributes.get("durations", []):
        amount = Decimal(str(duration["amount"]))
        if duration["unit"] == "HOUR" and hours is None:
            hours = amount
        elif duration["unit"] == "WEEK" and weeks is None:
            weeks = amount
        elif (
            duration["unit"] == "SESSION"
            and sessions is None
            and amount == amount.to_integral_value()
        ):
            sessions = int(amount)
    return hours, weeks, sessions


class IngestionRepository:
    def ensure_sources(self, source_metadata: dict[str, tuple[str, str]], codes: list[str]) -> None:
        from django.db import connection, transaction

        with transaction.atomic(), connection.cursor() as cursor:
            for code in codes:
                try:
                    name, base_url = source_metadata[code]
                except KeyError as exc:
                    raise ValueError(f"Fuente no soportada: {code}") from exc
                cursor.execute(
                    """
                    INSERT INTO source (code, name, base_url, active)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (code) DO NOTHING;
                    """,
                    (code, name, base_url),
                )

    def start(self, source_code: str, started_at) -> tuple[int, int]:
        from django.db import connection, transaction

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT id FROM source WHERE code = %s;", (source_code,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"No existe la fuente {source_code} en la tabla source.")
            source_id = row[0]
            cursor.execute(
                """
                INSERT INTO ingestion_log (source_id, status, started_at, counts)
                VALUES (%s, 'PARTIAL', %s, '{}'::jsonb)
                RETURNING id;
                """,
                (source_id, started_at),
            )
            return source_id, cursor.fetchone()[0]

    @staticmethod
    def active_course_count(cursor, source_id: int) -> int:
        cursor.execute(
            "SELECT count(*) FROM course WHERE source_id = %s AND state = 'ACTIVE';",
            (source_id,),
        )
        return cursor.fetchone()[0]

    @staticmethod
    def insert_raw_page(cursor, source_id: int, url: str, html_bytes: bytes) -> None:
        digest = hashlib.sha256(html_bytes).hexdigest()
        cursor.execute(
            """
            INSERT INTO raw_page (source_id, url, html_sha256, html, fetched_at)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (source_id, url, digest, html_bytes, timezone.now()),
        )

    def quarantine(self, source_id, ingestion_id, payload, error) -> None:
        from django.db import connection, transaction
        from pydantic import ValidationError

        errors = error.errors() if isinstance(error, ValidationError) else [
            {"type": type(error).__name__, "msg": str(error)}
        ]
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO quarantine_record
                    (source_id, ingestion_id, raw_payload, errors, reviewed)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, FALSE);
                """,
                (
                    source_id,
                    ingestion_id,
                    _json(payload),
                    _json(errors),
                ),
            )
            cursor.execute(
                """
                INSERT INTO alert (ingestion_id, severity, type, message, entity_ref)
                VALUES (%s, 'WARN', 'VALIDATION', %s, %s);
                """,
                (ingestion_id, str(error), payload.get("url")),
            )

    @staticmethod
    def finish(cursor, ingestion_id, status, counts, error_msg=None) -> None:
        cursor.execute(
            """
            UPDATE ingestion_log
               SET status = %s, finished_at = %s, counts = %s::jsonb, error_msg = %s
             WHERE id = %s;
            """,
            (status, timezone.now(), _json(counts), error_msg, ingestion_id),
        )

    @staticmethod
    def purge_raw_pages(cursor, values: dict[str, str]) -> None:
        from etl.services.resilience_service import setting_int

        retention = setting_int(values, "retention_raw_page_days", 90)
        cursor.execute(
            "DELETE FROM raw_page WHERE fetched_at < %s;",
            (timezone.now() - timedelta(days=retention),),
        )

    def mark_missing(self, cursor, source_id, ingestion_id, run_start) -> None:
        cursor.execute(
            """
            SELECT o.id FROM course_offering o
              JOIN course c ON c.id = o.course_id
             WHERE c.source_id = %s AND c.state = 'ACTIVE'
               AND o.state = 'ACTIVE' AND o.last_seen_at < %s
             FOR UPDATE OF o;
            """,
            (source_id, run_start),
        )
        for (offering_id,) in cursor.fetchall():
            cursor.execute(
                "UPDATE course_offering SET state = 'FINISHED' WHERE id = %s;",
                (offering_id,),
            )
            IngestionRepository._version_offering_state(
                cursor, ingestion_id, offering_id, "FINISHED"
            )
            self._change_log(
                cursor, ingestion_id, "course_offering", offering_id, "REMOVED",
                {"state": {"old": "ACTIVE", "new": "FINISHED"}},
            )
        cursor.execute(
            """
            SELECT id FROM course
             WHERE source_id = %s AND state = 'ACTIVE' AND last_seen_at < %s
             FOR UPDATE;
            """,
            (source_id, run_start),
        )
        for (course_id,) in cursor.fetchall():
            cursor.execute(
                "UPDATE course SET state = 'INACTIVE' WHERE id = %s;",
                (course_id,),
            )
            IngestionRepository._version_course_state(
                cursor, ingestion_id, course_id, "INACTIVE"
            )
            self._change_log(
                cursor, ingestion_id, "course", course_id, "REMOVED",
                {"state": {"old": "ACTIVE", "new": "INACTIVE"}},
            )

    @staticmethod
    def _version_offering_state(cursor, ingestion_id, offering_id, state):
        cursor.execute(
            """
            SELECT attributes FROM course_offering_version
             WHERE offering_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (offering_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        attributes = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        attributes["state"] = state
        fingerprint = _offering_fingerprint(attributes)
        cursor.execute(
            """
            UPDATE course_offering_version
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE offering_id = %s AND valid_to IS NULL;
            """,
            (offering_id,),
        )
        cursor.execute(
            """
            INSERT INTO course_offering_version
                (offering_id, ingestion_id, ingestion_id_original, fingerprint,
                 attributes, valid_from)
            VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp());
            """,
            (offering_id, ingestion_id, ingestion_id, fingerprint, _json(attributes)),
        )

    @staticmethod
    def _version_course_state(cursor, ingestion_id, course_id, state):
        cursor.execute(
            """
            SELECT attributes FROM course_version
             WHERE course_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (course_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        attributes = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        attributes["state"] = state
        cursor.execute(
            "SELECT canonical_url FROM course WHERE id = %s;",
            (course_id,),
        )
        canonical_url = cursor.fetchone()[0]
        fingerprint = _course_fingerprint(attributes, canonical_url)
        cursor.execute(
            """
            UPDATE course_version
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE course_id = %s AND valid_to IS NULL;
            """,
            (course_id,),
        )
        cursor.execute(
            """
            INSERT INTO course_version
                (course_id, ingestion_id, ingestion_id_original, fingerprint,
                 attributes, valid_from)
            VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp());
            """,
            (course_id, ingestion_id, ingestion_id, fingerprint, _json(attributes)),
        )

    @staticmethod
    def _change_log(cursor, ingestion_id, entity_type, entity_id, action, diff) -> None:
        cursor.execute(
            """
            INSERT INTO change_log (entity_type, entity_id, action, diff, ingestion_id)
            VALUES (%s, %s, %s, %s::jsonb, %s);
            """,
            (entity_type, entity_id, action, _json(diff), ingestion_id),
        )


class PriceRepository:
    def persist(self, cursor, ingestion_id: int, source_id: int, offering_id: int,
                offering: ScrapedOfferingDTO) -> int | None:
        snapshot_id = None
        if (
            offering.price_status == PriceStatus.PUBLISHED
            and offering.currency in SUPPORTED_CURRENCIES
            and offering.list_amount is not None
            and offering.final_amount is not None
        ):
            snapshot_id = self._persist_snapshot(
                cursor, ingestion_id, offering_id, offering
            )

        self._persist_observation(cursor, ingestion_id, offering_id, offering)
        if offering.price_status in {
            PriceStatus.NOT_PUBLISHED,
            PriceStatus.CURRENCY_UNIDENTIFIED,
            PriceStatus.CURRENCY_UNSUPPORTED,
        }:
            cursor.execute(
                """
                UPDATE price_snapshot
                   SET valid_to = GREATEST(
                       clock_timestamp(), valid_from + interval '1 microsecond'
                   )
                 WHERE offering_id = %s AND valid_to IS NULL;
                """,
                (offering_id,),
            )
        self._persist_tiers(cursor, ingestion_id, offering_id, offering)
        self._persist_payment_plans(
            cursor, ingestion_id, offering_id, offering, snapshot_id
        )
        self._persist_discounts(
            cursor, source_id, offering_id, snapshot_id, offering
        )
        if snapshot_id is None:
            cursor.execute(
                """
                SELECT id FROM price_snapshot
                 WHERE offering_id = %s AND is_current = TRUE
                 FOR UPDATE;
                """,
                (offering_id,),
            )
            current = cursor.fetchone()
            snapshot_id = current[0] if current else None
        return snapshot_id

    def _persist_snapshot(self, cursor, ingestion_id, offering_id, offering):
        cursor.execute(
            """
            SELECT id, list_amount, final_amount, currency
              FROM price_snapshot WHERE offering_id = %s AND is_current = TRUE
             FOR UPDATE;
            """,
            (offering_id,),
        )
        current = cursor.fetchone()
        reason = "INITIAL" if not current else "CHANGE_DETECTED"
        changed = (
            not current
            or Decimal(current[1]) != offering.list_amount
            or Decimal(current[2]) != offering.final_amount
            or current[3].strip() != offering.currency
        )
        if not changed:
            return current[0]
        cursor.execute(
            """
            INSERT INTO price_snapshot (
                offering_id, ingestion_id, currency, list_amount, final_amount,
                tax_included, valid_from, snapshot_reason, raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, TRUE, clock_timestamp(), %s, %s::jsonb)
            RETURNING id;
            """,
            (
                offering_id,
                ingestion_id,
                offering.currency,
                offering.list_amount,
                offering.final_amount,
                reason,
                _json({"price_raw": offering.price_raw}),
            ),
        )
        snapshot_id = cursor.fetchone()[0]
        IngestionRepository._change_log(
            cursor,
            ingestion_id,
            "price_snapshot",
            snapshot_id,
            "CREATED" if reason == "INITIAL" else "UPDATED",
            {
                "list_amount": str(offering.list_amount),
                "final_amount": str(offering.final_amount),
                "currency": offering.currency,
            },
        )
        return snapshot_id

    @staticmethod
    def _persist_observation(cursor, ingestion_id, offering_id, offering):
        attributes = {
            "status": offering.price_status.value,
            "price_observation": offering.price_observation.value,
            "list_amount": str(offering.list_amount)
            if offering.list_amount is not None
            else None,
            "final_amount": str(offering.final_amount)
            if offering.final_amount is not None
            else None,
            "currency": offering.currency,
            "currency_raw": offering.currency_raw,
            "price_raw": offering.price_raw,
        }
        fingerprint = _fingerprint(
            {key: value for key, value in attributes.items() if not key.endswith("_raw")}
        )
        cursor.execute(
            """
            SELECT id, fingerprint FROM offering_price_observation
             WHERE offering_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (offering_id,),
        )
        current = cursor.fetchone()
        if current and current[1] == fingerprint:
            return
        cursor.execute(
            """
            UPDATE offering_price_observation
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE offering_id = %s AND valid_to IS NULL;
            """,
            (offering_id,),
        )
        cursor.execute(
            """
            INSERT INTO offering_price_observation
                (offering_id, ingestion_id, status, list_amount, final_amount,
                 currency, currency_raw, price_raw, fingerprint, valid_from)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp());
            """,
            (
                offering_id,
                ingestion_id,
                offering.price_status.value,
                offering.list_amount,
                offering.final_amount,
                offering.currency,
                offering.currency_raw,
                offering.price_raw,
                fingerprint,
            ),
        )

    @staticmethod
    def _persist_tiers(cursor, ingestion_id, offering_id, offering):
        if offering.price_tiers_observation.value not in AUTHORITATIVE_STATUSES:
            return
        current_segment_ids = set()
        complete_observation = True
        for tier in offering.price_tiers:
            if tier.currency not in SUPPORTED_CURRENCIES:
                complete_observation = False
                continue
            cursor.execute("SELECT id FROM segment WHERE code = %s;", (tier.segment_code,))
            segment = cursor.fetchone()
            if not segment:
                logger.warning("No existe segmento %s; se conserva en historial comercial", tier.segment_code)
                complete_observation = False
                continue
            segment_id = segment[0]
            current_segment_ids.add(segment_id)
            cursor.execute(
                """
                SELECT id, amount, currency, notes FROM offering_price_tier
                 WHERE offering_id = %s AND segment_id = %s AND is_current = TRUE
                 FOR UPDATE;
                """,
                (offering_id, segment_id),
            )
            current = cursor.fetchone()
            if current and (
                Decimal(current[1]) == tier.amount
                and current[2].strip() == tier.currency
                and current[3] == tier.notes
            ):
                continue
            cursor.execute(
                """
                INSERT INTO offering_price_tier
                    (offering_id, segment_id, amount, currency, notes, valid_from, ingestion_id)
                VALUES (%s, %s, %s, %s, %s, clock_timestamp(), %s);
                """,
                (
                    offering_id,
                    segment_id,
                    tier.amount,
                    tier.currency,
                    tier.notes,
                    ingestion_id,
                ),
            )
        if complete_observation:
            cursor.execute(
                """
                UPDATE offering_price_tier
                   SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
                 WHERE offering_id = %s AND valid_to IS NULL
                   AND NOT (segment_id = ANY(%s));
                """,
                (offering_id, list(current_segment_ids)),
            )

    @staticmethod
    def _persist_payment_plans(cursor, ingestion_id, offering_id, offering, snapshot_id):
        if offering.payment_plans_observation.value not in AUTHORITATIVE_STATUSES:
            return
        if snapshot_id is None or offering.final_amount is None:
            return
        active = set()
        for plan in offering.payment_plans:
            for card_name in plan.cards:
                bank, brand = OfferingRepository.card_identity(card_name)
                cursor.execute(
                    """
                    INSERT INTO payment_card (bank, brand, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (bank, brand) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id;
                    """,
                    (bank, brand, card_name),
                )
                card_id = cursor.fetchone()[0]
                active.add((card_id, plan.installments))
                installment_amount = (
                    offering.final_amount / plan.installments
                ).quantize(Decimal("0.01"))
                cursor.execute(
                    """
                    SELECT installments, installment_amount, total_amount, interest_rate, currency
                      FROM payment_plan
                         WHERE offering_id = %s AND card_id = %s
                           AND installments = %s AND is_current = TRUE
                         ORDER BY valid_from DESC LIMIT 1 FOR UPDATE;
                        """,
                        (offering_id, card_id, plan.installments),
                )
                current = cursor.fetchone()
                expected = (
                    plan.installments,
                    installment_amount,
                    offering.final_amount,
                    plan.interest_rate,
                    offering.currency,
                )
                if current and (
                    current[0],
                    Decimal(current[1]),
                    Decimal(current[2]),
                    Decimal(current[3]),
                    current[4].strip(),
                ) == expected:
                    continue
                cursor.execute(
                    """
                    INSERT INTO payment_plan (
                        offering_id, card_id, installments, installment_amount,
                        total_amount, interest_rate, currency, valid_from, ingestion_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp(), %s);
                    """,
                    (
                        offering_id,
                        card_id,
                        plan.installments,
                        installment_amount,
                        offering.final_amount,
                        plan.interest_rate,
                        offering.currency,
                        ingestion_id,
                    ),
                )
        cursor.execute(
            """
            UPDATE payment_plan
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE offering_id = %s AND valid_to IS NULL
               AND (card_id, installments) NOT IN (
                   SELECT key.card_id, key.installments
                     FROM unnest(%s::int[], %s::int[]) AS key(card_id, installments)
               );
            """,
            (
                offering_id,
                [card_id for card_id, _ in active],
                [installments for _, installments in active],
            ),
        )

    @staticmethod
    def _persist_discounts(cursor, source_id, offering_id, snapshot_id, offering):
        if offering.discounts_observation.value not in AUTHORITATIVE_STATUSES:
            return
        for discount in offering.discounts:
            segment_id = None
            if discount.segment_code:
                cursor.execute(
                    "SELECT id FROM segment WHERE code = %s;",
                    (discount.segment_code,),
                )
                segment = cursor.fetchone()
                segment_id = segment[0] if segment else None
            cursor.execute(
                """
                SELECT id FROM discount
                 WHERE source_id = %s AND offering_id = %s AND course_id IS NULL
                   AND kind = %s AND value = %s
                   AND segment_id IS NOT DISTINCT FROM %s
                   AND description IS NOT DISTINCT FROM %s
                   AND conditions IS NOT DISTINCT FROM %s
                 ORDER BY id LIMIT 1;
                """,
                (
                    source_id,
                    offering_id,
                    discount.kind,
                    discount.value,
                    segment_id,
                    discount.description,
                    discount.conditions,
                ),
            )
            row = cursor.fetchone()
            if row:
                discount_id = row[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO discount (
                        source_id, offering_id, segment_id, kind, value,
                        description, conditions
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        source_id,
                        offering_id,
                        segment_id,
                        discount.kind,
                        discount.value,
                        discount.description,
                        discount.conditions,
                    ),
                )
                discount_id = cursor.fetchone()[0]
            if (
                snapshot_id is not None
                and offering.price_status == PriceStatus.PUBLISHED
                and offering.currency in SUPPORTED_CURRENCIES
                and offering.final_amount is not None
            ):
                cursor.execute(
                    """
                    INSERT INTO price_snapshot_discount
                        (price_snapshot_id, discount_id, applied)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (price_snapshot_id, discount_id) DO NOTHING;
                    """,
                    (snapshot_id, discount_id),
                )


class OfferingRepository:
    def __init__(self, price_repository: PriceRepository | None = None):
        self.price_repository = price_repository or PriceRepository()

    def persist(self, cursor, source_id, ingestion_id, course_id, offering) -> dict[str, Any]:
        attributes = self._attributes(offering)
        modality_id = (
            self._modality_id(cursor, attributes["modality_code"])
            if attributes.get("modality_observation") in AUTHORITATIVE_STATUSES
            else None
        )
        cursor.execute(
            """
            SELECT id, canonical_hash, state, modality_id
              FROM course_offering
             WHERE course_id = %s AND external_code = %s
             FOR UPDATE;
            """,
            (course_id, offering.external_code),
        )
        existing = cursor.fetchone()
        offering_id = existing[0] if existing else self._insert_offering(
            cursor, course_id, attributes, modality_id
        )
        previous_attributes = None
        if existing:
            previous_attributes = self._current_version(cursor, offering_id)
        attributes = _preserve_unobserved(
            attributes,
            previous_attributes,
            {
                "start_date": "start_date_observation",
                "end_date": "end_date_observation",
                "duration_hours": "duration_observation",
                "duration_weeks": "duration_observation",
                "sessions_count": "duration_observation",
                "schedules": "schedules_observation",
                "modality_code": "modality_observation",
                "normalized_modality": "modality_observation",
                "modality_raw": "modality_observation",
                "publication_status": "publication_status_observation",
            },
        )
        if attributes.get("modality_observation") in AUTHORITATIVE_STATUSES:
            modality_id = self._modality_id(cursor, attributes["modality_code"])
        elif existing:
            modality_id = existing[3]
        fingerprint = _offering_fingerprint(attributes)
        changed = not existing or existing[1] != fingerprint or existing[2] != "ACTIVE"
        if existing:
            self._touch_or_update(cursor, offering_id, attributes, modality_id, fingerprint, changed)
        if changed:
            self._synchronize_schedules(cursor, offering_id, attributes, ingestion_id)
            self._write_version(cursor, offering_id, ingestion_id, attributes, fingerprint)
            IngestionRepository._change_log(
                cursor,
                ingestion_id,
                "course_offering",
                offering_id,
                "CREATED" if not existing else "UPDATED",
                attributes,
            )
        snapshot_id = self.price_repository.persist(
            cursor, ingestion_id, source_id, offering_id, offering
        )
        commercial_changed = self._persist_commercial_version(
            cursor, ingestion_id, offering_id, offering, snapshot_id
        )
        return {
            "changed": changed,
            "commercial_changed": commercial_changed,
            "offering_id": offering_id,
        }

    @staticmethod
    def sync_course_relations(cursor, source_id, course_id, offering_id, attributes):
        for kind, flag, observation, code in (
            ("CERTIFICATE", "has_certificate", "certificate_observation", "CERTIFICADO"),
            (
                "PARTICIPATION",
                "has_participation_certificate",
                "participation_certificate_observation",
                "CONSTANCIA_PARTICIPACION",
            ),
        ):
            if attributes.get(observation) not in AUTHORITATIVE_STATUSES:
                continue
            cursor.execute(
                "SELECT id FROM certificate_type WHERE code = %s;",
                (code,),
            )
            type_row = cursor.fetchone()
            if not type_row:
                continue
            cursor.execute(
                """
                DELETE FROM offering_certificate
                 WHERE offering_id = %s AND certificate_type_id = %s;
                """,
                (offering_id, type_row[0]),
            )
            certificates = [
                cert for cert in attributes.get("certificates", [])
                if cert["kind"] == kind
            ]
            if attributes.get(flag) and not certificates:
                certificates = [{"kind": kind}]
            if certificates:
                cursor.execute(
                    """
                    INSERT INTO offering_certificate
                        (offering_id, certificate_type_id, price, currency,
                         requires_approval, optional)
                    VALUES (%s, %s, NULL, NULL, TRUE, TRUE)
                    ON CONFLICT (offering_id, certificate_type_id) DO NOTHING;
                    """,
                    (offering_id, type_row[0]),
                )

        if attributes.get("professors_observation") not in AUTHORITATIVE_STATUSES:
            return
        cursor.execute(
            "DELETE FROM offering_professor WHERE offering_id = %s;",
            (offering_id,),
        )
        for professor in attributes.get("professors", []):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(lower(%s)));",
                (professor["full_name"],),
            )
            cursor.execute(
                """
                SELECT id FROM professor
                 WHERE lower(full_name) = lower(%s)
                 ORDER BY (specialty IS NOT NULL) DESC, id
                 LIMIT 1 FOR UPDATE;
                """,
                (professor["full_name"],),
            )
            row = cursor.fetchone()
            if row:
                professor_id = row[0]
                if professor.get("specialty"):
                    cursor.execute(
                        """
                        UPDATE professor
                           SET specialty = COALESCE(specialty, %s)
                         WHERE id = %s;
                        """,
                        (professor["specialty"], professor_id),
                    )
            else:
                cursor.execute(
                    """
                    INSERT INTO professor (full_name, specialty)
                    VALUES (%s, %s)
                    RETURNING id;
                    """,
                    (professor["full_name"], professor.get("specialty")),
                )
                professor_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO professor_source_link
                    (professor_id, source_id, specialty)
                VALUES (%s, %s, %s)
                ON CONFLICT (professor_id, source_id)
                DO UPDATE SET specialty = COALESCE(
                    EXCLUDED.specialty, professor_source_link.specialty
                );
                """,
                (professor_id, source_id, professor.get("specialty")),
            )
            cursor.execute(
                """
                INSERT INTO offering_professor (offering_id, professor_id, role)
                VALUES (%s, %s, 'TITULAR')
                ON CONFLICT (offering_id, professor_id, role) DO NOTHING;
                """,
                (offering_id, professor_id),
            )

    @staticmethod
    def _modality_id(cursor, code):
        cursor.execute("SELECT id FROM modality WHERE code = %s;", (code,))
        row = cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _attributes(offering):
        attrs = _dump_model(offering)
        attrs["state"] = "ACTIVE"
        for key in (
            "list_amount",
            "final_amount",
            "currency",
            "currency_raw",
            "currency_observation",
            "price_observation",
            "price_status",
            "discounts",
            "discounts_observation",
            "price_tiers",
            "price_tiers_observation",
            "payment_plans",
            "payment_plans_observation",
            "commercial_conditions",
            "commercial_conditions_observation",
            "price_raw",
            "raw_payload",
        ):
            attrs.pop(key, None)
        for field, status_field in (
            ("start_date", "start_date_observation"),
            ("end_date", "end_date_observation"),
            ("modality_code", "modality_observation"),
            ("normalized_modality", "modality_observation"),
            ("modality_raw", "modality_observation"),
            ("publication_status", "publication_status_observation"),
        ):
            if attrs.get(status_field) not in AUTHORITATIVE_STATUSES:
                attrs[field] = None
        if attrs.get("duration_observation") not in AUTHORITATIVE_STATUSES:
            for field in ("duration_hours", "duration_weeks", "sessions_count"):
                attrs[field] = None
            attrs["durations"] = []
        if attrs.get("schedules_observation") not in AUTHORITATIVE_STATUSES:
            attrs["schedules"] = []
        attrs["schedules"] = sorted(
            attrs["schedules"],
            key=lambda row: (
                row["day_of_week"],
                row["start_time"],
                row["end_time"],
            ),
        )
        for duration in attrs["durations"]:
            duration.pop("original_text", None)
        return attrs

    @staticmethod
    def _insert_offering(cursor, course_id, attributes, modality_id):
        cursor.execute(
            """
            INSERT INTO course_offering (
                course_id, external_code, modality_id, start_date, end_date,
                duration_hours, duration_weeks, sessions_count, state,
                canonical_hash, first_seen_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s,
                    clock_timestamp(), clock_timestamp())
            ON CONFLICT (course_id, external_code) WHERE external_code IS NOT NULL
            DO NOTHING
            RETURNING id;
            """,
            (
                course_id,
                attributes["external_code"],
                modality_id,
                attributes.get("start_date"),
                attributes.get("end_date"),
                attributes.get("duration_hours"),
                attributes.get("duration_weeks"),
                attributes.get("sessions_count"),
                _fingerprint(attributes),
            ),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute(
            """
            SELECT id FROM course_offering
             WHERE course_id = %s AND external_code = %s
             FOR UPDATE;
            """,
            (course_id, attributes["external_code"]),
        )
        return cursor.fetchone()[0]

    @staticmethod
    def _current_version(cursor, offering_id):
        cursor.execute(
            """
            SELECT attributes FROM course_offering_version
             WHERE offering_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (offering_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT o.external_code, o.start_date, o.end_date,
                       o.duration_hours, o.duration_weeks, o.sessions_count,
                       o.state, m.code
                  FROM course_offering o
                  LEFT JOIN modality m ON m.id = o.modality_id
                 WHERE o.id = %s;
                """,
                (offering_id,),
            )
            legacy = cursor.fetchone()
            if not legacy:
                return None
            external_code, start, end, hours, weeks, sessions, state, modality = legacy
            cursor.execute(
                """
                SELECT day_of_week, start_time, end_time
                  FROM offering_schedule
                 WHERE offering_id = %s
                 ORDER BY day_of_week, start_time, end_time;
                """,
                (offering_id,),
            )
            schedules = [
                {
                    "day_of_week": day,
                    "start_time": start.isoformat(),
                    "end_time": finish.isoformat(),
                }
                for day, start, finish in cursor.fetchall()
            ]
            return {
                "external_code": external_code,
                "start_date": start,
                "start_date_observation": (
                    ObservationStatus.OBSERVED.value
                    if start
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "end_date": end,
                "end_date_observation": (
                    ObservationStatus.OBSERVED.value
                    if end
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "duration_hours": hours,
                "duration_weeks": weeks,
                "sessions_count": sessions,
                "duration_observation": (
                    ObservationStatus.OBSERVED.value
                    if any(value is not None for value in (hours, weeks, sessions))
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "durations": [],
                "modality_code": modality,
                "normalized_modality": None,
                "modality_raw": None,
                "modality_observation": (
                    ObservationStatus.OBSERVED.value
                    if modality
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "schedules": schedules,
                "schedules_observation": (
                    ObservationStatus.OBSERVED.value
                    if schedules
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "publication_status": None,
                "publication_status_observation": ObservationStatus.NOT_COLLECTED.value,
                "state": state,
            }
        value = row[0]
        return json.loads(value) if isinstance(value, str) else value

    @staticmethod
    def _touch_or_update(cursor, offering_id, attributes, modality_id, fingerprint, changed):
        if not changed:
            cursor.execute(
                "UPDATE course_offering SET last_seen_at = clock_timestamp() WHERE id = %s;",
                (offering_id,),
            )
            return
        cursor.execute(
            """
            UPDATE course_offering
               SET modality_id = %s, start_date = %s, end_date = %s,
                   duration_hours = %s, duration_weeks = %s, sessions_count = %s,
                   state = 'ACTIVE', canonical_hash = %s,
                   last_seen_at = clock_timestamp()
             WHERE id = %s;
            """,
            (
                modality_id,
                attributes.get("start_date"),
                attributes.get("end_date"),
                attributes.get("duration_hours"),
                attributes.get("duration_weeks"),
                attributes.get("sessions_count"),
                fingerprint,
                offering_id,
            ),
        )

    @staticmethod
    def _synchronize_schedules(cursor, offering_id, attributes, ingestion_id):
        if attributes.get("schedules_observation") not in AUTHORITATIVE_STATUSES:
            return
        expected = sorted(
            (
                schedule["day_of_week"],
                schedule["start_time"],
                schedule["end_time"],
            )
            for schedule in attributes.get("schedules", [])
        )
        cursor.execute(
            """
            SELECT day_of_week, start_time, end_time
              FROM offering_schedule
             WHERE offering_id = %s
             ORDER BY day_of_week, start_time, end_time;
            """,
            (offering_id,),
        )
        current = [
            (day, start.isoformat(), end.isoformat())
            for day, start, end in cursor.fetchall()
        ]
        if current == expected:
            return
        cursor.execute("DELETE FROM offering_schedule WHERE offering_id = %s;", (offering_id,))
        for day, start, end in expected:
            cursor.execute(
                """
                INSERT INTO offering_schedule (offering_id, day_of_week, start_time, end_time)
                VALUES (%s, %s, %s, %s);
                """,
                (
                    offering_id,
                    day,
                    start,
                    end,
                ),
            )

    @staticmethod
    def _write_version(cursor, offering_id, ingestion_id, attributes, fingerprint):
        cursor.execute(
            """
            UPDATE course_offering_version
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE offering_id = %s AND valid_to IS NULL;
            """,
            (offering_id,),
        )
        cursor.execute(
            """
            INSERT INTO course_offering_version
                (offering_id, ingestion_id, ingestion_id_original, fingerprint,
                 attributes, valid_from)
            VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp());
            """,
            (offering_id, ingestion_id, ingestion_id, fingerprint, _json(attributes)),
        )

    @staticmethod
    def _persist_commercial_version(cursor, ingestion_id, offering_id, offering, snapshot_id):
        cursor.execute(
            """
            SELECT fingerprint, attributes FROM offering_commercial_version
             WHERE offering_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (offering_id,),
        )
        current = cursor.fetchone()
        previous = None
        if current:
            previous = json.loads(current[1]) if isinstance(current[1], str) else current[1]
        payload = {
            "discounts": sorted(
                (_dump_model(item) for item in offering.discounts), key=_json
            ),
            "discounts_observation": offering.discounts_observation.value,
            "commercial_conditions": sorted(offering.commercial_conditions),
            "commercial_conditions_observation": (
                offering.commercial_conditions_observation.value
            ),
            "price_tiers_observation": offering.price_tiers_observation.value,
            "payment_plans_observation": offering.payment_plans_observation.value,
            "price_snapshot_id": snapshot_id,
            "unpersisted_price_tiers": sorted(
                (
                    _dump_model(tier)
                    for tier in offering.price_tiers
                    if tier.currency not in SUPPORTED_CURRENCIES
                ),
                key=_json,
            ),
            "unpersisted_payment_plans": (
                sorted(
                    (_dump_model(plan) for plan in offering.payment_plans),
                    key=_json,
                )
                if (
                    snapshot_id is None
                    or offering.final_amount is None
                    or offering.currency not in SUPPORTED_CURRENCIES
                )
                else []
            ),
        }
        for field, status_field in (
            ("discounts", "discounts_observation"),
            ("commercial_conditions", "commercial_conditions_observation"),
            ("unpersisted_price_tiers", "price_tiers_observation"),
            ("unpersisted_payment_plans", "payment_plans_observation"),
        ):
            if (
                offering.model_dump(mode="json").get(status_field)
                not in AUTHORITATIVE_STATUSES
                and previous
            ):
                payload[field] = previous.get(field, payload[field])
                payload[status_field] = previous.get(
                    status_field, payload[status_field]
                )
        cursor.execute(
            """
            SELECT segment_id, amount, currency, notes
              FROM offering_price_tier
             WHERE offering_id = %s AND valid_to IS NULL
             ORDER BY segment_id;
            """,
            (offering_id,),
        )
        tier_state = cursor.fetchall()
        cursor.execute(
            """
            SELECT card_id, installments, installment_amount, total_amount,
                   interest_rate, currency
              FROM payment_plan
             WHERE offering_id = %s AND valid_to IS NULL
             ORDER BY card_id, installments;
            """,
            (offering_id,),
        )
        payment_state = cursor.fetchall()
        fingerprint_data = {
            **payload,
            "price_status": offering.price_status.value,
            "list_amount": str(offering.list_amount)
            if offering.list_amount is not None
            else None,
            "final_amount": str(offering.final_amount)
            if offering.final_amount is not None
            else None,
            "currency": offering.currency,
            "price_tiers_state": tier_state,
            "payment_plans_state": payment_state,
            "unpersisted_price_tiers": payload["unpersisted_price_tiers"],
            "unpersisted_payment_plans": payload["unpersisted_payment_plans"],
        }
        fingerprint = _fingerprint(fingerprint_data)
        if current and current[0] == fingerprint:
            return False
        cursor.execute(
            """
            UPDATE offering_commercial_version
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE offering_id = %s AND valid_to IS NULL;
            """,
            (offering_id,),
        )
        cursor.execute(
            """
            INSERT INTO offering_commercial_version
                (offering_id, ingestion_id, ingestion_id_original, price_snapshot_id,
                 fingerprint, attributes, valid_from)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, clock_timestamp());
            """,
            (
                offering_id,
                ingestion_id,
                ingestion_id,
                snapshot_id,
                fingerprint,
                _json(payload),
            ),
        )
        IngestionRepository._change_log(
            cursor,
            ingestion_id,
            "offering_commercial",
            offering_id,
            "CREATED" if not current else "UPDATED",
            {"fingerprint": fingerprint},
        )
        return True

    @staticmethod
    def card_identity(name: str) -> tuple[str, str]:
        normalized = name.strip().upper()
        if normalized.startswith("DINERS"):
            return "Diners", "DINERS"
        if normalized.startswith("VISA"):
            bank = "SCOTIABANK" if "SCOTIABANK" in normalized else "VISA"
            return bank, "VISA"
        if normalized == "MASTERCARD":
            return "MASTERCARD", "MC"
        if normalized in {"MC", "AMEX"}:
            return normalized, normalized
        return name.strip(), "OTRO"


class CourseRepository:
    def __init__(self, offering_repository: OfferingRepository | None = None):
        self.offering_repository = offering_repository or OfferingRepository()

    def persist(self, cursor, source_id: int, ingestion_id: int,
                dto: ScrapedCourseDTO) -> dict[str, int]:
        canonical_url = canonicalize_course_url(str(dto.course_url))
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s));",
            (source_id, f"external:{dto.external_id}"),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s));",
            (source_id, canonical_url),
        )
        course_id, existing = self._find_course(cursor, source_id, dto.external_id, canonical_url)
        previous = self._current_version(cursor, course_id) if existing else None
        attributes = self._attributes(dto)
        attributes["canonical_url"] = canonical_url
        category_id = (
            self._category_id(cursor, source_id, dto.category_name)
            if attributes.get("category_observation") in AUTHORITATIVE_STATUSES
            else None
        )
        attributes = _preserve_unobserved(
            attributes,
            previous,
            {
                "category_name": "category_observation",
                "category": "category_observation",
                "has_certificate": "certificate_observation",
                "certificates": "certificate_observation",
                "has_participation_certificate": "participation_certificate_observation",
                "content_items": "content_observation",
                "prerequisite_items": "prerequisites_observation",
                "hardware_requirements": "hardware_observation",
                "software_requirements": "software_observation",
                "duration_hours": "duration_observation",
                "durations": "duration_observation",
                "professors": "professors_observation",
                "contacts": "contacts_observation",
            },
        )
        _sort_people(attributes)
        fingerprint = _course_fingerprint(attributes, canonical_url)
        changed = not existing or existing.get("fingerprint") != fingerprint or existing["state"] != "ACTIVE"
        if not existing:
            course_id = self._insert_course(
                cursor,
                source_id,
                dto,
                attributes,
                category_id,
                canonical_url,
                fingerprint,
            )
        else:
            self._update_course(
                cursor, course_id, dto, attributes, category_id,
                canonical_url, fingerprint, changed, existing,
            )
        if not self._alias_exists(cursor, source_id, dto.external_id, course_id):
            self._insert_alias(cursor, source_id, course_id, dto.external_id, canonical_url)
        if changed:
            self._sync_children(cursor, course_id, attributes)
            self._write_version(cursor, course_id, ingestion_id, attributes, fingerprint)
            IngestionRepository._change_log(
                cursor,
                ingestion_id,
                "course",
                course_id,
                "CREATED" if not existing else "UPDATED",
                {"fingerprint": fingerprint},
            )
        offering_changes = 0
        for offering in dto.offerings:
            result = self.offering_repository.persist(
                cursor, source_id, ingestion_id, course_id, offering
            )
            if changed or result["changed"]:
                self.offering_repository.sync_course_relations(
                    cursor, source_id, course_id, result["offering_id"], attributes
                )
            offering_changes += int(result["changed"] or result["commercial_changed"])
        return {
            "course_changed": int(changed),
            "offerings_changed": offering_changes,
        }

    @staticmethod
    def _attributes(dto):
        attributes = _dump_model(dto)
        attributes.pop("offerings", None)
        attributes.pop("raw_payload", None)
        attributes["state"] = "ACTIVE"
        for field, status_field in (
            ("category_name", "category_observation"),
            ("category", "category_observation"),
            ("duration_hours", "duration_observation"),
            ("durations", "duration_observation"),
            ("has_certificate", "certificate_observation"),
            ("certificates", "certificate_observation"),
            (
                "has_participation_certificate",
                "participation_certificate_observation",
            ),
            ("content_items", "content_observation"),
            ("prerequisite_items", "prerequisites_observation"),
            ("hardware_requirements", "hardware_observation"),
            ("software_requirements", "software_observation"),
            ("professors", "professors_observation"),
            ("contacts", "contacts_observation"),
        ):
            if attributes.get(status_field) not in AUTHORITATIVE_STATUSES:
                attributes[field] = (
                    False
                    if field in {"has_certificate", "has_participation_certificate"}
                    else []
                    if field.endswith("items")
                    or field.endswith("requirements")
                    or field in {"durations", "certificates", "professors", "contacts"}
                    else None
                )
        return attributes

    @staticmethod
    def _category_id(cursor, source_id, name):
        if not name:
            return None
        cursor.execute(
            """
            INSERT INTO category (source_id, name)
            VALUES (%s, %s)
            ON CONFLICT (source_id, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id;
            """,
            (source_id, name),
        )
        return cursor.fetchone()[0]

    @staticmethod
    def _find_course(cursor, source_id, external_id, canonical_url):
        cursor.execute(
            """
            SELECT id, canonical_hash, state, category_id, has_certificate,
                   has_participation_certificate
              FROM course
             WHERE source_id = %s AND external_id = %s
             FOR UPDATE;
            """,
            (source_id, external_id),
        )
        row = cursor.fetchone()
        if row:
            return row[0], {
                "fingerprint": row[1],
                "state": row[2],
                "category_id": row[3],
                "has_certificate": row[4],
                "has_participation_certificate": row[5],
            }
        cursor.execute(
            """
            SELECT c.id, c.canonical_hash, c.state, c.category_id,
                   c.has_certificate, c.has_participation_certificate
              FROM course_identity_alias a
              JOIN course c ON c.id = a.course_id
             WHERE a.source_id = %s AND a.external_id = %s
             FOR UPDATE OF c;
            """,
            (source_id, external_id),
        )
        row = cursor.fetchone()
        if not row and canonical_url:
            cursor.execute(
                """
                SELECT id, canonical_hash, state, category_id,
                       has_certificate, has_participation_certificate
                  FROM course
                 WHERE source_id = %s AND canonical_url = %s
                 FOR UPDATE;
                """,
                (source_id, canonical_url),
            )
            row = cursor.fetchone()
        return (
            (row[0], {
                "fingerprint": row[1],
                "state": row[2],
                "category_id": row[3],
                "has_certificate": row[4],
                "has_participation_certificate": row[5],
            })
            if row
            else (None, None)
        )

    @staticmethod
    def _insert_course(
        cursor, source_id, dto, attributes, category_id, canonical_url, fingerprint
    ):
        duration_hours, duration_weeks, sessions_count = _course_duration_values(attributes)
        cursor.execute(
            """
            INSERT INTO course (
                source_id, external_id, canonical_url, name, category_id,
                description, course_url, duration_hours, duration_weeks,
                sessions_count, has_certificate,
                has_participation_certificate, state, canonical_hash,
                first_seen_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s,
                    clock_timestamp(), clock_timestamp())
            ON CONFLICT (source_id, external_id) DO NOTHING
            RETURNING id;
            """,
            (
                source_id,
                dto.external_id,
                canonical_url,
                dto.name,
                category_id,
                dto.description,
                str(dto.course_url),
                duration_hours,
                duration_weeks,
                sessions_count,
                (
                    attributes["has_certificate"]
                    if attributes.get("certificate_observation")
                    in AUTHORITATIVE_STATUSES
                    else False
                ),
                (
                    attributes["has_participation_certificate"]
                    if attributes.get("participation_certificate_observation")
                    in AUTHORITATIVE_STATUSES
                    else False
                ),
                fingerprint,
            ),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute(
            """
            SELECT id FROM course
             WHERE source_id = %s AND external_id = %s
             FOR UPDATE;
            """,
            (source_id, dto.external_id),
        )
        return cursor.fetchone()[0]

    @staticmethod
    def _update_course(cursor, course_id, dto, attributes, category_id, canonical_url,
                       fingerprint, changed, existing):
        if not changed:
            cursor.execute(
                "UPDATE course SET last_seen_at = clock_timestamp() WHERE id = %s;",
                (course_id,),
            )
            return
        category_id = (
            category_id
            if attributes.get("category_observation") in AUTHORITATIVE_STATUSES
            else existing["category_id"]
        )
        certificate = (
            attributes["has_certificate"]
            if attributes.get("certificate_observation") in AUTHORITATIVE_STATUSES
            else existing["has_certificate"]
        )
        participation = (
            attributes["has_participation_certificate"]
            if attributes.get("participation_certificate_observation") in AUTHORITATIVE_STATUSES
            else existing["has_participation_certificate"]
        )
        duration_hours, duration_weeks, sessions_count = _course_duration_values(
            attributes
        )
        cursor.execute(
            """
            UPDATE course
               SET canonical_url = %s, name = %s, category_id = %s,
                   description = %s, course_url = %s, duration_hours = %s,
                   duration_weeks = %s, sessions_count = %s,
                   has_certificate = %s, has_participation_certificate = %s,
                   state = 'ACTIVE', canonical_hash = %s,
                   last_seen_at = clock_timestamp()
             WHERE id = %s;
            """,
            (
                canonical_url,
                dto.name,
                category_id,
                dto.description,
                str(dto.course_url),
                duration_hours,
                duration_weeks,
                sessions_count,
                certificate,
                participation,
                fingerprint,
                course_id,
            ),
        )

    @staticmethod
    def _sync_children(cursor, course_id, attributes):
        CourseRepository._replace_list_if_observed(
            cursor,
            course_id,
            "course_content_item",
            attributes.get("content_items", []),
            attributes.get("content_observation"),
        )
        CourseRepository._replace_list_if_observed(
            cursor,
            course_id,
            "course_prerequisite_item",
            attributes.get("prerequisite_items", []),
            attributes.get("prerequisites_observation"),
        )
        requirements_observed = (
            attributes.get("hardware_observation") in AUTHORITATIVE_STATUSES
            or attributes.get("software_observation") in AUTHORITATIVE_STATUSES
        )
        if requirements_observed:
            cursor.execute("DELETE FROM course_tech_req_item WHERE course_id = %s;", (course_id,))
            position = 0
            for kind, field, observation in (
                ("HW", "hardware_requirements", "hardware_observation"),
                ("SW", "software_requirements", "software_observation"),
            ):
                if attributes.get(observation) not in AUTHORITATIVE_STATUSES:
                    continue
                for text in attributes.get(field, []):
                    cursor.execute(
                        """
                        INSERT INTO course_tech_req_item (course_id, position, kind, text)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (course_id, position, kind, text),
                    )
                    position += 1
        if attributes.get("contacts_observation") in AUTHORITATIVE_STATUSES:
            cursor.execute("DELETE FROM course_contact WHERE course_id = %s;", (course_id,))
            for contact in attributes.get("contacts", []):
                cursor.execute(
                    """
                    INSERT INTO course_contact (course_id, name, phone, email)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (course_id, contact["name"], contact.get("phone"), contact.get("email")),
                )

    @staticmethod
    def _replace_list_if_observed(cursor, course_id, table, items, status):
        if status not in AUTHORITATIVE_STATUSES:
            return
        cursor.execute(f"DELETE FROM {table} WHERE course_id = %s;", (course_id,))
        for position, text in enumerate(items):
            cursor.execute(
                f"INSERT INTO {table} (course_id, position, text) VALUES (%s, %s, %s);",
                (course_id, position, text),
            )

    @staticmethod
    def _current_version(cursor, course_id):
        cursor.execute(
            """
            SELECT attributes FROM course_version
             WHERE course_id = %s AND valid_to IS NULL
             FOR UPDATE;
            """,
            (course_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT c.source_id, c.category_id, cat.name, c.duration_hours,
                       c.duration_weeks, c.sessions_count, c.has_certificate,
                       c.has_participation_certificate, c.state
                  FROM course c
                  LEFT JOIN category cat ON cat.id = c.category_id
                 WHERE c.id = %s;
                """,
                (course_id,),
            )
            legacy = cursor.fetchone()
            if not legacy:
                return None
            (
                source_id,
                _category_id,
                category_name,
                hours,
                weeks,
                sessions,
                has_certificate,
                has_participation,
                state,
            ) = legacy
            cursor.execute(
                """
                SELECT position, text FROM course_content_item
                 WHERE course_id = %s ORDER BY position;
                """,
                (course_id,),
            )
            content = [text for _, text in cursor.fetchall()]
            cursor.execute(
                """
                SELECT position, text FROM course_prerequisite_item
                 WHERE course_id = %s ORDER BY position;
                """,
                (course_id,),
            )
            prerequisites = [text for _, text in cursor.fetchall()]
            cursor.execute(
                """
                SELECT kind, position, text FROM course_tech_req_item
                 WHERE course_id = %s ORDER BY position;
                """,
                (course_id,),
            )
            requirements = cursor.fetchall()
            hardware = [text for kind, _, text in requirements if kind == "HW"]
            software = [text for kind, _, text in requirements if kind == "SW"]
            cursor.execute(
                """
                SELECT name, phone, email FROM course_contact
                 WHERE course_id = %s ORDER BY id;
                """,
                (course_id,),
            )
            contacts = [
                {"name": name, "phone": phone, "email": email}
                for name, phone, email in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT DISTINCT p.full_name,
                       COALESCE(psl.specialty, p.specialty)
                  FROM course_offering o
                  JOIN offering_professor op ON op.offering_id = o.id
                  JOIN professor p ON p.id = op.professor_id
                  LEFT JOIN professor_source_link psl
                    ON psl.professor_id = p.id AND psl.source_id = %s
                 WHERE o.course_id = %s
                 ORDER BY p.full_name;
                """,
                (source_id, course_id),
            )
            professors = [
                {"full_name": name, "specialty": specialty}
                for name, specialty in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT DISTINCT ct.code
                  FROM course_offering o
                  JOIN offering_certificate oc ON oc.offering_id = o.id
                  JOIN certificate_type ct ON ct.id = oc.certificate_type_id
                 WHERE o.course_id = %s;
                """,
                (course_id,),
            )
            certificate_codes = {code for (code,) in cursor.fetchall()}
            durations = []
            if hours is not None:
                durations.append({"amount": hours, "unit": "HOUR", "original_text": None})
            if weeks is not None:
                durations.append({"amount": weeks, "unit": "WEEK", "original_text": None})
            if sessions is not None:
                durations.append(
                    {"amount": sessions, "unit": "SESSION", "original_text": None}
                )
            return {
                "category_name": category_name,
                "category": (
                    {
                        "source_label": category_name,
                        "normalized_label": None,
                        "homologation_code": None,
                        "observation": ObservationStatus.OBSERVED.value,
                    }
                    if category_name
                    else None
                ),
                "category_observation": (
                    ObservationStatus.OBSERVED.value
                    if category_name
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "duration_hours": hours,
                "durations": durations,
                "duration_observation": (
                    ObservationStatus.OBSERVED.value
                    if durations
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "has_certificate": has_certificate,
                "certificate_observation": (
                    ObservationStatus.OBSERVED.value
                    if "CERTIFICADO" in certificate_codes
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "has_participation_certificate": has_participation,
                "participation_certificate_observation": (
                    ObservationStatus.OBSERVED.value
                    if "CONSTANCIA_PARTICIPACION" in certificate_codes
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "certificates": [
                    {
                        "kind": (
                            "CERTIFICATE"
                            if code == "CERTIFICADO"
                            else "PARTICIPATION"
                        ),
                        "issuer": None,
                        "description": None,
                    }
                    for code in sorted(certificate_codes)
                    if code in {"CERTIFICADO", "CONSTANCIA_PARTICIPACION"}
                ],
                "content_items": content,
                "content_observation": (
                    ObservationStatus.OBSERVED.value
                    if content
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "prerequisite_items": prerequisites,
                "prerequisites_observation": (
                    ObservationStatus.OBSERVED.value
                    if prerequisites
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "hardware_requirements": hardware,
                "hardware_observation": (
                    ObservationStatus.OBSERVED.value
                    if hardware
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "software_requirements": software,
                "software_observation": (
                    ObservationStatus.OBSERVED.value
                    if software
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "professors": professors,
                "professors_observation": (
                    ObservationStatus.OBSERVED.value
                    if professors
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "contacts": contacts,
                "contacts_observation": (
                    ObservationStatus.OBSERVED.value
                    if contacts
                    else ObservationStatus.NOT_COLLECTED.value
                ),
                "state": state,
            }
        value = row[0]
        return json.loads(value) if isinstance(value, str) else value

    @staticmethod
    def _write_version(cursor, course_id, ingestion_id, attributes, fingerprint):
        cursor.execute(
            """
            UPDATE course_version
               SET valid_to = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond')
             WHERE course_id = %s AND valid_to IS NULL;
            """,
            (course_id,),
        )
        cursor.execute(
            """
            INSERT INTO course_version
                (course_id, ingestion_id, ingestion_id_original, fingerprint,
                 attributes, valid_from)
            VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp());
            """,
            (course_id, ingestion_id, ingestion_id, fingerprint, _json(attributes)),
        )

    @staticmethod
    def _alias_exists(cursor, source_id, external_id, course_id):
        cursor.execute(
            """
            SELECT course_id FROM course_identity_alias
             WHERE source_id = %s AND external_id = %s
             FOR UPDATE;
            """,
            (source_id, external_id),
        )
        row = cursor.fetchone()
        if row and row[0] != course_id:
            raise ValueError(
                f"El identificador externo {external_id!r} ya está asociado a otro curso."
            )
        return bool(row)

    @staticmethod
    def _insert_alias(cursor, source_id, course_id, external_id, canonical_url):
        cursor.execute(
            """
            INSERT INTO course_identity_alias
                (source_id, course_id, external_id, canonical_url, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, clock_timestamp(), clock_timestamp())
            ON CONFLICT (source_id, external_id)
            DO UPDATE SET canonical_url = EXCLUDED.canonical_url,
                          last_seen_at = EXCLUDED.last_seen_at
            RETURNING course_id;
            """,
            (source_id, course_id, external_id, canonical_url),
        )
        row = cursor.fetchone()
        if row[0] != course_id:
            raise ValueError(
                f"El identificador externo {external_id!r} ya está asociado a otro curso."
            )
