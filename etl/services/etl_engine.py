import hashlib
import json
import logging
import traceback
from datetime import timedelta
from decimal import Decimal
from typing import Any

import requests
from django.db import connection, transaction
from django.utils import timezone
from pydantic import ValidationError

from etl.extractors.cibertec import CibertecExtractor
from etl.extractors.pucp import PucpExtractor
from etl.schemas import (
    ScrapedCourseDTO,
    pucp_course_to_dto,
)
from etl.services.resilience_service import (
    ResilienceService,
    read_settings,
    setting_int,
)


logger = logging.getLogger(__name__)


class IngestionFailure(RuntimeError):
    def __init__(self, message: str, failure_type: str = "HTTP_ERROR"):
        super().__init__(message)
        self.failure_type = failure_type


class ETLEngine:
    SOURCE_METADATA = {
        "PUCP": (
            "Educación Continua PUCP",
            "https://educacioncontinua.pucp.edu.pe/tipo-de-actividad/cursos/",
        ),
        "CIBERTEC": (
            "Educación Continua Cibertec",
            "https://educacioncontinua.cibertec.edu.pe/buscador/",
        ),
    }

    def __init__(self, source_code: str):
        self.source_code = source_code.upper()
        self.resilience = ResilienceService()
        self.extractor = self._make_extractor()

    def _make_extractor(self):
        if self.source_code == "PUCP":
            return PucpExtractor()
        if self.source_code == "CIBERTEC":
            return CibertecExtractor()
        raise ValueError(f"Fuente no soportada: {self.source_code}")

    def run(self, limit: int | None = None) -> dict[str, Any]:
        run_start = timezone.now()
        counts = {
            "discovered": 0,
            "processed_ok": 0,
            "quarantined": 0,
            "http_errors": 0,
            "courses_changed": 0,
            "offerings_changed": 0,
        }
        self.ensure_sources([self.source_code])
        source_id, ingestion_id = self._start_ingestion(run_start)
        saved_page_count = 0
        try:
            active_count_before = self._active_course_count(source_id)
            catalog = self.extractor.fetch_catalog_index()
            if limit is not None:
                catalog = catalog[:limit]
            counts["discovered"] = len(catalog)
            if not catalog:
                raise IngestionFailure(
                    "El catálogo no contiene cursos; posible cambio de estructura DOM.",
                    "ZERO_RESULTS",
                )
            saved_page_count = self._save_raw_pages(source_id, saved_page_count)

            for item in catalog:
                try:
                    dto = self._extract_one(item)
                except (ValidationError, ValueError) as exc:
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                except requests.RequestException as exc:
                    counts["http_errors"] += 1
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                    logger.warning("Fallo HTTP en detalle %s: %s", item.get("url"), exc)
                except Exception as exc:
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                    logger.exception("No se pudo procesar curso %s", item.get("url"))
                else:
                    with transaction.atomic(), connection.cursor() as cursor:
                        changes = self._persist_course(cursor, source_id, ingestion_id, dto)
                    counts["processed_ok"] += 1
                    counts["courses_changed"] += changes["course_changed"]
                    counts["offerings_changed"] += changes["offerings_changed"]
                finally:
                    saved_page_count = self._save_raw_pages(
                        source_id, saved_page_count
                    )

            minimum_pct = self._minimum_courses_pct()
            minimum_count = (active_count_before * minimum_pct + 99) // 100
            if counts["processed_ok"] == 0:
                raise IngestionFailure(
                    "No se pudo validar ningún curso de la fuente.",
                    "DOM_STRUCTURE_CHANGED",
                )
            if counts["processed_ok"] < minimum_count:
                raise IngestionFailure(
                    f"Se procesaron {counts['processed_ok']} cursos, menos del "
                    f"{minimum_pct}% requerido de los {active_count_before} "
                    "cursos activos al iniciar la corrida.",
                    "DOM_STRUCTURE_CHANGED",
                )

            with transaction.atomic(), connection.cursor() as cursor:
                self._mark_missing(cursor, source_id, ingestion_id, run_start)
                self._finish_ingestion(
                    cursor,
                    ingestion_id,
                    "PARTIAL" if counts["quarantined"] else "OK",
                    counts,
                    None,
                )
                self.resilience.succeeded(cursor, source_id)
                self._purge_raw_pages(cursor)
            counts["status"] = "PARTIAL" if counts["quarantined"] else "OK"
            return counts
        except Exception as exc:
            error_message = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            failure_type = (
                exc.failure_type if isinstance(exc, IngestionFailure) else "HTTP_ERROR"
            )
            with transaction.atomic(), connection.cursor() as cursor:
                self._finish_ingestion(cursor, ingestion_id, "FAILED", counts, error_message)
                self.resilience.failed(
                    cursor, source_id, ingestion_id, failure_type, str(exc)
                )
                self._purge_raw_pages(cursor)
            logger.error("Ingesta %s fallida: %s", self.source_code, exc)
            counts["status"] = "FAILED"
            return counts
        finally:
            close = getattr(self.extractor, "close", None)
            if close:
                close()

    @classmethod
    def ensure_sources(cls, source_codes: list[str]) -> None:
        with transaction.atomic(), connection.cursor() as cursor:
            for code in source_codes:
                try:
                    name, base_url = cls.SOURCE_METADATA[code]
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

    def _start_ingestion(self, started_at) -> tuple[int, int]:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT id FROM source WHERE code = %s;", (self.source_code,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"No existe la fuente {self.source_code} en la tabla source.")
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

    def _extract_one(self, item: dict[str, Any]) -> ScrapedCourseDTO:
        if self.source_code == "CIBERTEC":
            dto, _, _ = self.extractor.fetch_course(item)
            return dto
        html, _, _ = self.extractor.fetch_page(item["url"])
        return pucp_course_to_dto(
            self.extractor.parse_course_detail(
                html, item["url"], item.get("categoria")
            )
        )

    def _save_raw_pages(self, source_id: int, start_index: int) -> int:
        pages = getattr(self.extractor, "raw_pages", [])
        if start_index >= len(pages):
            return start_index
        with connection.cursor() as cursor:
            for url, html_bytes in pages[start_index:]:
                self._insert_raw_page(cursor, source_id, url, html_bytes)
        return len(pages)

    @staticmethod
    def _insert_raw_page(cursor, source_id: int, url: str, html_bytes: bytes) -> None:
        digest = hashlib.sha256(html_bytes).hexdigest()
        cursor.execute(
            """
            INSERT INTO raw_page (source_id, url, html_sha256, html, fetched_at)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (source_id, url, digest, html_bytes, timezone.now()),
        )

    def _quarantine(self, source_id, ingestion_id, payload, error) -> None:
        errors = error.errors() if isinstance(error, ValidationError) else [
            {"type": type(error).__name__, "msg": str(error)}
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO quarantine_record
                    (source_id, ingestion_id, raw_payload, errors, reviewed)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, FALSE);
                """,
                (
                    source_id,
                    ingestion_id,
                    json.dumps(payload, default=str),
                    json.dumps(errors, default=str),
                ),
            )
            cursor.execute(
                """
                INSERT INTO alert (ingestion_id, severity, type, message, entity_ref)
                VALUES (%s, 'WARN', 'VALIDATION', %s, %s);
                """,
                (ingestion_id, str(error), payload.get("url")),
            )

    def _minimum_courses_pct(self) -> int:
        with connection.cursor() as cursor:
            return setting_int(read_settings(cursor), "min_courses_threshold_pct", 20)

    @staticmethod
    def _active_course_count(source_id: int) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM course WHERE source_id = %s AND state = 'ACTIVE';",
                (source_id,),
            )
            return cursor.fetchone()[0]

    def _persist_course(self, cursor, source_id, ingestion_id, dto: ScrapedCourseDTO):
        cursor.execute(
            """
            INSERT INTO category (source_id, name)
            SELECT %s, %s WHERE %s IS NOT NULL
            ON CONFLICT (source_id, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id;
            """,
            (source_id, dto.category_name, dto.category_name),
        )
        category_row = cursor.fetchone()
        category_id = category_row[0] if category_row else None
        canonical_hash = self._hash({
            "name": dto.name,
            "description": dto.description,
            "duration_hours": str(dto.duration_hours),
            "has_certificate": dto.has_certificate,
        })
        cursor.execute(
            """
            SELECT id, name, description, duration_hours, has_certificate,
                   state, canonical_hash
              FROM course WHERE source_id = %s AND external_id = %s
             FOR UPDATE;
            """,
            (source_id, dto.external_id),
        )
        existing = cursor.fetchone()
        course_changed = False
        if not existing:
            cursor.execute(
                """
                INSERT INTO course (
                    source_id, external_id, name, category_id, description, course_url,
                    duration_hours, has_certificate, has_participation_certificate,
                    state, canonical_hash, first_seen_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s)
                RETURNING id;
                """,
                (
                    source_id, dto.external_id, dto.name, category_id, dto.description,
                    str(dto.course_url), dto.duration_hours, dto.has_certificate,
                    dto.has_participation_certificate, canonical_hash,
                    timezone.now(), timezone.now(),
                ),
            )
            course_id = cursor.fetchone()[0]
            course_changed = True
            self._change_log(cursor, ingestion_id, "course", course_id, "CREATED", {})
        else:
            course_id, old_name, old_description, old_duration, old_certificate, old_state, old_hash = existing
            if old_hash != canonical_hash or old_state != "ACTIVE":
                diff = self._diff(
                    {
                        "name": old_name,
                        "description": old_description,
                        "duration_hours": old_duration,
                        "has_certificate": old_certificate,
                        "state": old_state,
                    },
                    {
                        "name": dto.name,
                        "description": dto.description,
                        "duration_hours": dto.duration_hours,
                        "has_certificate": dto.has_certificate,
                        "state": "ACTIVE",
                    },
                )
                cursor.execute(
                    """
                    UPDATE course SET name = %s, category_id = %s, description = %s,
                        course_url = %s, duration_hours = %s, has_certificate = %s,
                        has_participation_certificate = %s, state = 'ACTIVE',
                        canonical_hash = %s, last_seen_at = %s
                     WHERE id = %s;
                    """,
                    (
                        dto.name, category_id, dto.description, str(dto.course_url),
                        dto.duration_hours, dto.has_certificate,
                        dto.has_participation_certificate, canonical_hash,
                        timezone.now(), course_id,
                    ),
                )
                self._replace_course_items(cursor, course_id, dto)
                self._change_log(cursor, ingestion_id, "course", course_id, "UPDATED", diff)
                course_changed = True
            else:
                cursor.execute(
                    "UPDATE course SET last_seen_at = %s WHERE id = %s;",
                    (timezone.now(), course_id),
                )

        if course_changed and existing is None:
            self._replace_course_items(cursor, course_id, dto)
        offering_changes = 0
        for offering in dto.offerings:
            if self._persist_offering(cursor, source_id, ingestion_id, course_id, offering):
                offering_changes += 1
        return {"course_changed": int(course_changed), "offerings_changed": offering_changes}

    @staticmethod
    def _replace_course_items(cursor, course_id, dto):
        cursor.execute("DELETE FROM course_content_item WHERE course_id = %s;", (course_id,))
        cursor.execute("DELETE FROM course_prerequisite_item WHERE course_id = %s;", (course_id,))
        cursor.execute("DELETE FROM course_tech_req_item WHERE course_id = %s;", (course_id,))
        for position, text in enumerate(dto.content_items):
            cursor.execute(
                "INSERT INTO course_content_item (course_id, position, text) VALUES (%s, %s, %s);",
                (course_id, position, text),
            )
        for position, text in enumerate(dto.prerequisite_items):
            cursor.execute(
                "INSERT INTO course_prerequisite_item (course_id, position, text) VALUES (%s, %s, %s);",
                (course_id, position, text),
            )
        requirements = [
            ("HW", text) for text in dto.hardware_requirements
        ] + [("SW", text) for text in dto.software_requirements]
        for position, (kind, text) in enumerate(requirements):
            cursor.execute(
                """
                INSERT INTO course_tech_req_item (course_id, position, kind, text)
                VALUES (%s, %s, %s, %s);
                """,
                (course_id, position, kind, text),
            )

    def _persist_offering(self, cursor, source_id, ingestion_id, course_id, offering):
        schedule_signature = [
            (row.day_of_week, row.start_time.isoformat(), row.end_time.isoformat())
            for row in offering.schedules
        ]
        canonical_hash = self._hash({
            "start_date": offering.start_date.isoformat() if offering.start_date else None,
            "end_date": offering.end_date.isoformat() if offering.end_date else None,
            "modality": offering.modality_code,
            "duration_hours": str(offering.duration_hours),
            "duration_weeks": str(offering.duration_weeks),
            "schedules": schedule_signature,
        })
        cursor.execute("SELECT id FROM modality WHERE code = %s;", (offering.modality_code,))
        modality_row = cursor.fetchone()
        modality_id = modality_row[0] if modality_row else None
        cursor.execute(
            """
            SELECT id, start_date, end_date, modality_id, duration_hours,
                   duration_weeks, canonical_hash, state
              FROM course_offering WHERE course_id = %s AND external_code = %s
             FOR UPDATE;
            """,
            (course_id, offering.external_code),
        )
        existing = cursor.fetchone()
        changed = False
        if not existing:
            cursor.execute(
                """
                INSERT INTO course_offering (
                    course_id, external_code, modality_id, start_date, end_date,
                    duration_hours, duration_weeks, sessions_count, state,
                    canonical_hash, first_seen_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s)
                RETURNING id;
                """,
                (
                    course_id, offering.external_code, modality_id, offering.start_date,
                    offering.end_date, offering.duration_hours, offering.duration_weeks,
                    offering.sessions_count, canonical_hash, timezone.now(), timezone.now(),
                ),
            )
            offering_id = cursor.fetchone()[0]
            changed = True
            self._change_log(cursor, ingestion_id, "course_offering", offering_id, "CREATED", {})
        else:
            offering_id, old_start, old_end, old_modality, old_hours, old_weeks, old_hash, old_state = existing
            if old_hash != canonical_hash or old_state != "ACTIVE":
                diff = self._diff(
                    {
                        "start_date": old_start, "end_date": old_end,
                        "modality_id": old_modality, "duration_hours": old_hours,
                        "duration_weeks": old_weeks, "state": old_state,
                    },
                    {
                        "start_date": offering.start_date, "end_date": offering.end_date,
                        "modality_id": modality_id, "duration_hours": offering.duration_hours,
                        "duration_weeks": offering.duration_weeks, "state": "ACTIVE",
                    },
                )
                cursor.execute(
                    """
                    UPDATE course_offering SET modality_id = %s, start_date = %s,
                        end_date = %s, duration_hours = %s, duration_weeks = %s,
                        sessions_count = %s, state = 'ACTIVE', canonical_hash = %s,
                        last_seen_at = %s WHERE id = %s;
                    """,
                    (
                        modality_id, offering.start_date, offering.end_date,
                        offering.duration_hours, offering.duration_weeks,
                        offering.sessions_count, canonical_hash, timezone.now(), offering_id,
                    ),
                )
                self._change_log(
                    cursor, ingestion_id, "course_offering", offering_id, "UPDATED", diff
                )
                changed = True
            else:
                cursor.execute(
                    "UPDATE course_offering SET last_seen_at = %s WHERE id = %s;",
                    (timezone.now(), offering_id),
                )

        if changed:
            cursor.execute("DELETE FROM offering_schedule WHERE offering_id = %s;", (offering_id,))
            for schedule in offering.schedules:
                cursor.execute(
                    """
                    INSERT INTO offering_schedule (offering_id, day_of_week, start_time, end_time)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (offering_id, schedule.day_of_week, schedule.start_time, schedule.end_time),
                )
        snapshot_id = self._persist_price(cursor, ingestion_id, source_id, offering_id, offering)
        self._persist_tiers(cursor, offering_id, offering)
        self._persist_payment_plans(cursor, offering_id, offering)
        self._persist_discounts(cursor, source_id, offering_id, snapshot_id, offering)
        return changed

    def _persist_price(self, cursor, ingestion_id, source_id, offering_id, offering):
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
                offering_id, currency, list_amount, final_amount, tax_included,
                valid_from, snapshot_reason, raw_payload
            )
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s::jsonb)
            RETURNING id;
            """,
            (
                offering_id, offering.currency, offering.list_amount,
                offering.final_amount, timezone.now(), reason,
                json.dumps(offering.raw_payload, default=str),
            ),
        )
        snapshot_id = cursor.fetchone()[0]
        self._change_log(
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

    def _persist_discounts(self, cursor, source_id, offering_id, snapshot_id, offering):
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
                    source_id, offering_id, discount.kind, discount.value, segment_id,
                    discount.description, discount.conditions,
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
                        source_id, offering_id, segment_id, discount.kind, discount.value,
                        discount.description, discount.conditions,
                    ),
                )
                discount_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO price_snapshot_discount (price_snapshot_id, discount_id, applied)
                VALUES (%s, %s, TRUE) ON CONFLICT (price_snapshot_id, discount_id) DO NOTHING;
                """,
                (snapshot_id, discount_id),
            )

    def _persist_tiers(self, cursor, offering_id, offering):
        for tier in offering.price_tiers:
            cursor.execute("SELECT id FROM segment WHERE code = %s;", (tier.segment_code,))
            segment = cursor.fetchone()
            if not segment:
                logger.warning("No existe segmento %s; se omite el tier", tier.segment_code)
                continue
            segment_id = segment[0]
            cursor.execute(
                """
                SELECT amount, currency FROM offering_price_tier
                 WHERE offering_id = %s AND segment_id = %s AND is_current = TRUE
                 FOR UPDATE;
                """,
                (offering_id, segment_id),
            )
            current = cursor.fetchone()
            if current and Decimal(current[0]) == tier.amount and current[1].strip() == tier.currency:
                continue
            cursor.execute(
                """
                INSERT INTO offering_price_tier
                    (offering_id, segment_id, amount, currency, notes, valid_from)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    offering_id, segment_id, tier.amount, tier.currency,
                    tier.notes, timezone.now(),
                ),
            )

    def _persist_payment_plans(self, cursor, offering_id, offering):
        for plan in offering.payment_plans:
            for card_name in plan.cards:
                bank, brand = self._card_identity(card_name)
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
                installment_amount = (offering.final_amount / plan.installments).quantize(
                    Decimal("0.01")
                )
                cursor.execute(
                    """
                    SELECT installments, installment_amount, total_amount, interest_rate, currency
                      FROM payment_plan
                     WHERE offering_id = %s AND card_id = %s AND is_current = TRUE
                     ORDER BY valid_from DESC LIMIT 1 FOR UPDATE;
                    """,
                    (offering_id, card_id),
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
                        total_amount, interest_rate, currency, valid_from
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        offering_id, card_id, plan.installments, installment_amount,
                        offering.final_amount, plan.interest_rate, offering.currency,
                        timezone.now(),
                    ),
                )

    @staticmethod
    def _card_identity(name: str) -> tuple[str, str]:
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

    def _mark_missing(self, cursor, source_id, ingestion_id, run_start):
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
                """
                UPDATE course_offering SET state = 'FINISHED' WHERE id = %s;
                """,
                (offering_id,),
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
            cursor.execute("UPDATE course SET state = 'INACTIVE' WHERE id = %s;", (course_id,))
            self._change_log(
                cursor, ingestion_id, "course", course_id, "REMOVED",
                {"state": {"old": "ACTIVE", "new": "INACTIVE"}},
            )

    def _purge_raw_pages(self, cursor):
        retention = setting_int(read_settings(cursor), "retention_raw_page_days", 90)
        cursor.execute(
            "DELETE FROM raw_page WHERE fetched_at < %s;",
            (timezone.now() - timedelta(days=retention),),
        )

    @staticmethod
    def _finish_ingestion(cursor, ingestion_id, status, counts, error_msg):
        cursor.execute(
            """
            UPDATE ingestion_log
               SET status = %s, finished_at = %s, counts = %s::jsonb, error_msg = %s
             WHERE id = %s;
            """,
            (status, timezone.now(), json.dumps(counts), error_msg, ingestion_id),
        )

    @staticmethod
    def _change_log(cursor, ingestion_id, entity_type, entity_id, action, diff):
        cursor.execute(
            """
            INSERT INTO change_log (entity_type, entity_id, action, diff, ingestion_id)
            VALUES (%s, %s, %s, %s::jsonb, %s);
            """,
            (entity_type, entity_id, action, json.dumps(diff, default=str), ingestion_id),
        )

    @staticmethod
    def _hash(value: dict) -> str:
        encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _diff(old: dict, new: dict) -> dict:
        return {
            key: {"old": old.get(key), "new": new.get(key)}
            for key in new
            if old.get(key) != new.get(key)
        }
