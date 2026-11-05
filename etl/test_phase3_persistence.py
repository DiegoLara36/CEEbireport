from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from django.test import SimpleTestCase

from etl.normalization import canonicalize_course_url, classify_price
from etl.persistence.repositories import (
    CourseRepository,
    OfferingRepository,
    PriceRepository,
    _preserve_unobserved,
)
from etl.schemas import (
    ObservationStatus,
    PriceStatus,
    ScrapedCourseDTO,
    ScrapedOfferingDTO,
)


class PersistenceContractTests(SimpleTestCase):
    def test_url_identity_removes_tracking_query_and_fragment(self):
        self.assertEqual(
            canonicalize_course_url(
                "HTTPS://Example.Test:443/course/?utm_source=email#details"
            ),
            "https://example.test/course",
        )

    def test_price_status_keeps_missing_and_unsupported_currency_distinct(self):
        self.assertEqual(
            classify_price(None, None, ObservationStatus.UNKNOWN),
            PriceStatus.UNKNOWN,
        )
        self.assertEqual(
            classify_price(
                Decimal("0"), "EUR", ObservationStatus.OBSERVED
            ),
            PriceStatus.CURRENCY_UNSUPPORTED,
        )
        self.assertEqual(
            classify_price(
                Decimal("0"), "PEN", ObservationStatus.OBSERVED
            ),
            PriceStatus.PUBLISHED,
        )

    def test_course_fingerprint_attributes_ignore_raw_payload(self):
        first = ScrapedCourseDTO(
            source_code="PUCP",
            external_id="curso",
            course_url="https://example.test/curso/",
            name="Curso",
            raw_payload={"html": "first"},
            offerings=[ScrapedOfferingDTO(list_amount=None, final_amount=None)],
        )
        second = first.model_copy(deep=True)
        second.raw_payload = {"html": "second"}
        self.assertEqual(
            CourseRepository._attributes(first),
            CourseRepository._attributes(second),
        )

    def test_unobserved_offering_fields_are_not_persisted_as_new_facts(self):
        offering = ScrapedOfferingDTO(
            start_date=date(2027, 1, 2),
            start_date_observation=ObservationStatus.NOT_COLLECTED,
            duration_hours=Decimal("10"),
            duration_observation=ObservationStatus.NOT_COLLECTED,
            schedules_observation=ObservationStatus.NOT_COLLECTED,
            list_amount=None,
            final_amount=None,
        )

        attributes = OfferingRepository._attributes(offering)

        self.assertIsNone(attributes["start_date"])
        self.assertIsNone(attributes["duration_hours"])
        self.assertEqual(attributes["schedules"], [])

    def test_unobserved_fields_reuse_last_known_values(self):
        incoming = {
            "start_date": None,
            "start_date_observation": "NOT_COLLECTED",
            "duration_hours": None,
            "duration_observation": "NOT_COLLECTED",
        }
        previous = {
            "start_date": "2026-11-14",
            "start_date_observation": "OBSERVED",
            "duration_hours": "30",
            "duration_observation": "OBSERVED",
        }

        merged = _preserve_unobserved(
            incoming,
            previous,
            {
                "start_date": "start_date_observation",
                "duration_hours": "duration_observation",
            },
        )

        self.assertEqual(merged["start_date"], "2026-11-14")
        self.assertEqual(merged["duration_hours"], "30")
        self.assertEqual(merged["start_date_observation"], "OBSERVED")

    def test_missing_price_records_observation_without_creating_price_snapshot(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        offering = ScrapedOfferingDTO(
            price_status=PriceStatus.NOT_PUBLISHED,
            price_observation=ObservationStatus.NOT_FOUND,
            list_amount=None,
            final_amount=None,
        )

        snapshot_id = PriceRepository().persist(cursor, 10, 2, 20, offering)

        self.assertIsNone(snapshot_id)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(
            any("INSERT INTO offering_price_observation" in sql for sql in statements)
        )
        self.assertFalse(any("INSERT INTO price_snapshot" in sql for sql in statements))

    def test_unsupported_currency_is_observed_but_not_saved_as_supported_price(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        offering = ScrapedOfferingDTO(
            price_status=PriceStatus.CURRENCY_UNSUPPORTED,
            currency="EUR",
            currency_raw="€",
            list_amount=Decimal("100"),
            final_amount=Decimal("90"),
        )

        snapshot_id = PriceRepository().persist(cursor, 10, 2, 20, offering)

        self.assertIsNone(snapshot_id)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(
            any("INSERT INTO offering_price_observation" in sql for sql in statements)
        )
        self.assertFalse(any("INSERT INTO price_snapshot" in sql for sql in statements))
