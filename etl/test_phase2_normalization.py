from datetime import date, time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from pydantic import ValidationError

from etl.adapters import adapt_cibertec_course, adapt_pucp_course
from etl.extractors.cibertec import CibertecExtractor
from etl.extractors.pucp import PucpExtractor
from etl.normalization import (
    normalize_amount,
    normalize_category,
    normalize_currency,
    normalize_date,
    normalize_duration,
    normalize_modality,
)
from etl.schemas import (
    CourseExtractedSchema,
    NormalizedModality,
    ObservationStatus,
    ScheduleItemSchema,
    ScrapedCourseDTO,
    ScrapedOfferingDTO,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class SharedNormalizationTests(SimpleTestCase):
    def test_dates_parse_supported_formats_without_inventing_a_year(self):
        self.assertEqual(normalize_date("2026-11-14"), date(2026, 11, 14))
        self.assertEqual(normalize_date("14/11/2026"), date(2026, 11, 14))
        self.assertEqual(
            normalize_date("14 de noviembre de 2026"), date(2026, 11, 14)
        )
        self.assertIsNone(normalize_date("14 de noviembre"))
        self.assertIsNone(normalize_date("31/02/2026"))

    def test_amount_and_currency_require_unambiguous_currency_evidence(self):
        self.assertEqual(normalize_amount("S/ 1,234.50"), Decimal("1234.50"))
        self.assertEqual(normalize_amount("1.234,50"), Decimal("1234.50"))
        self.assertEqual(normalize_amount("1,5 horas"), Decimal("1.5"))
        self.assertEqual(normalize_currency("S/ 520"), "PEN")
        self.assertEqual(normalize_currency("USD 99.00"), "USD")
        self.assertEqual(normalize_currency("EUR 99.00"), "EUR")
        self.assertEqual(normalize_currency("€ 99.00"), "EUR")
        self.assertIsNone(normalize_currency("$ 99.00"))
        self.assertIsNone(normalize_currency(None))

    def test_duration_modality_and_category_keep_source_context(self):
        self.assertEqual(
            [(item.amount, item.unit) for item in normalize_duration("14 semanas (108 hrs.)")],
            [(Decimal("108"), "HOUR"), (Decimal("14"), "WEEK")],
        )
        self.assertEqual(normalize_modality("Virtual asincrónico"), NormalizedModality.VIRTUAL)
        self.assertEqual(normalize_modality("Híbrido"), NormalizedModality.HYBRID)
        self.assertEqual(normalize_modality("Presencial"), NormalizedModality.PRESENTIAL)
        self.assertIsNone(normalize_modality("Flexible"))
        category = normalize_category("Educación Continua")
        self.assertEqual(category.source_label, "Educación Continua")
        self.assertEqual(category.normalized_label, "educacion_continua")
        self.assertIsNone(category.homologation_code)

    def test_currency_dto_accepts_iso_shaped_codes_and_rejects_bad_codes(self):
        offering = ScrapedOfferingDTO(
            currency="EUR",
            list_amount=Decimal("10"),
            final_amount=Decimal("10"),
        )
        self.assertEqual(offering.currency, "EUR")
        with self.assertRaises(ValidationError):
            ScrapedOfferingDTO(
                currency="EURO",
                list_amount=Decimal("10"),
                final_amount=Decimal("10"),
            )

    def test_schedule_validation_and_optional_observation_states(self):
        schedule = ScheduleItemSchema(
            day_of_week=2,
            start_time=time(19),
            end_time=time(22),
        )
        self.assertEqual(schedule.day_of_week, 2)
        course = ScrapedCourseDTO(
            source_code="CIBERTEC",
            external_id="course",
            course_url="https://example.test/course/",
            name="Example Course",
            contacts_observation=ObservationStatus.NOT_COLLECTED,
            offerings=[
                ScrapedOfferingDTO(
                    list_amount=Decimal("0"),
                    final_amount=Decimal("0"),
                    currency=None,
                )
            ],
        )
        self.assertEqual(course.contacts, [])
        self.assertEqual(course.contacts_observation, ObservationStatus.NOT_COLLECTED)
        with self.assertRaises(ValidationError):
            ScheduleItemSchema(
                day_of_week=8,
                start_time=time(19),
                end_time=time(22),
            )


class InstitutionalAdapterTests(SimpleTestCase):
    def test_pucp_fixture_preserves_teacher_contact_and_normalizes_fields(self):
        with patch(
            "etl.extractors.pucp.django_timezone.localdate",
            return_value=date(2026, 10, 1),
        ):
            extracted = PucpExtractor().parse_course_detail(
                fixture_text("pucp_course_detail.html"),
                "https://educacioncontinua.pucp.edu.pe/cursos/analitica-datos/",
            )
        dto = adapt_pucp_course(extracted)

        self.assertEqual(dto.professors[0].full_name, "Ana Docente")
        self.assertEqual(dto.professors[0].specialty, "Ciencia de datos")
        self.assertEqual(dto.contacts[0].name, "María Contacto")
        self.assertEqual(dto.category.source_label, "Tecnología")
        self.assertEqual(dto.duration_hours, Decimal("30"))
        self.assertEqual(dto.offerings[0].currency, "PEN")
        self.assertEqual(dto.offerings[0].start_date, date(2026, 11, 14))
        self.assertEqual(dto.offerings[0].schedules[0].day_of_week, 2)
        self.assertEqual(dto.offerings[0].durations[0].unit, "HOUR")
        self.assertEqual(dto.offerings[0].modality_observation, ObservationStatus.UNKNOWN)

    def test_cibertec_fixture_keeps_unknown_contacts_and_maps_common_values(self):
        with patch(
            "etl.extractors.cibertec.timezone.localdate",
            return_value=date(2026, 10, 1),
        ):
            dto = CibertecExtractor().parse_course_detail(
                fixture_text("cibertec_course_detail.html"),
                "https://educacioncontinua.cibertec.edu.pe/programas/tecnologia/analitica/",
                fallback_category="PROGRAMAS",
                modules=[{"name": "Módulo 1", "items": ["Modelado de datos"]}],
                cohorts=[
                    {
                        "start_text": "23 NOV.",
                        "duration_text": "14 semanas (108 hrs.)",
                        "schedule_text": "Miércoles de 7:00 p.m. a 10:00 p.m.",
                    }
                ],
            )

        offering = dto.offerings[0]
        self.assertEqual(dto.category.source_label, "Tecnología")
        self.assertEqual(dto.contacts, [])
        self.assertEqual(dto.contacts_observation, ObservationStatus.NOT_COLLECTED)
        self.assertEqual(dto.content_items, ["Módulo 1 - Modelado de datos"])
        self.assertEqual(offering.normalized_modality, NormalizedModality.VIRTUAL)
        self.assertEqual(offering.currency, "PEN")
        self.assertEqual(offering.start_date, date(2026, 11, 23))
        self.assertEqual(offering.start_date_observation, ObservationStatus.OBSERVED)
        self.assertEqual(offering.end_date_observation, ObservationStatus.NOT_COLLECTED)
        self.assertEqual(
            {(item.amount, item.unit) for item in offering.durations},
            {(Decimal("108"), "HOUR"), (Decimal("14"), "WEEK")},
        )
        self.assertEqual(offering.duration_observation, ObservationStatus.OBSERVED)
        self.assertEqual(offering.schedules[0].day_of_week, 3)
        self.assertEqual(offering.schedules_observation, ObservationStatus.OBSERVED)

    def test_cibertec_does_not_replace_missing_price_with_zero(self):
        course = CibertecExtractor().parse_course_detail(
            "<html><body><h1>Curso sin precio</h1></body></html>",
            "https://educacioncontinua.cibertec.edu.pe/cursos/sin-precio/",
        )
        offering = course.offerings[0]

        self.assertIsNone(offering.list_amount)
        self.assertIsNone(offering.final_amount)
        self.assertIsNone(offering.currency)
        self.assertEqual(offering.price_observation, ObservationStatus.NOT_FOUND)
        self.assertEqual(offering.start_date_observation, ObservationStatus.NOT_COLLECTED)

    def test_pucp_explicit_year_is_not_replaced_by_the_current_year(self):
        start, end, _, _ = PucpExtractor._parse_dates(
            "14 de noviembre de 2028",
            "20 de diciembre de 2028",
            reference_date=date(2026, 10, 1),
        )
        self.assertEqual(start, date(2028, 11, 14))
        self.assertEqual(end, date(2028, 12, 20))
        currency, amount = PucpExtractor._parse_price("Inversión: $ 520")
        self.assertIsNone(currency)
        self.assertEqual(amount, Decimal("520"))

    def test_equivalent_source_data_has_the_same_shared_normalized_values(self):
        schedule = ScheduleItemSchema(
            day_of_week=3,
            start_time=time(19),
            end_time=time(22),
        )
        pucp = adapt_pucp_course(
            CourseExtractedSchema(
                external_id="pucp-course",
                course_url="https://pucp.example.test/course/",
                category_name="Tecnología",
                modality_raw="Virtual",
                name="Actualización en Analítica",
                duration_hours=Decimal("108"),
                duration_original="108 horas",
                external_offering_code="pucp-edition",
                start_date=date(2026, 11, 23),
                schedules=[schedule],
                currency="PEN",
                price_amount=Decimal("727.50"),
            )
        )
        cibertec = adapt_cibertec_course(
            ScrapedCourseDTO(
                source_code="CIBERTEC",
                external_id="cibertec-course",
                course_url="https://cibertec.example.test/course/",
                category_name="Tecnología",
                name="Actualización en Analítica",
                duration_hours=Decimal("108"),
                offerings=[
                    ScrapedOfferingDTO(
                        modality_code="VIRTUAL_SINC",
                        modality_raw="Virtual",
                        start_date=date(2026, 11, 23),
                        duration_hours=Decimal("108"),
                        schedules=[schedule],
                        currency="PEN",
                        list_amount=Decimal("727.50"),
                        final_amount=Decimal("727.50"),
                    )
                ],
            )
        )
        pucp_offering = pucp.offerings[0]
        cibertec_offering = cibertec.offerings[0]

        self.assertEqual(pucp.category.normalized_label, cibertec.category.normalized_label)
        self.assertEqual(pucp_offering.normalized_modality, cibertec_offering.normalized_modality)
        self.assertEqual(pucp_offering.start_date, cibertec_offering.start_date)
        self.assertEqual(pucp_offering.currency, cibertec_offering.currency)
        self.assertEqual(pucp_offering.final_amount, cibertec_offering.final_amount)
        self.assertEqual(pucp_offering.schedules, cibertec_offering.schedules)
        self.assertEqual(
            (pucp_offering.durations[0].amount, pucp_offering.durations[0].unit),
            (cibertec_offering.durations[0].amount, cibertec_offering.durations[0].unit),
        )

    def test_canonical_fingerprint_is_deterministic_and_ignores_raw_payload(self):
        first = ScrapedCourseDTO(
            source_code="PUCP",
            external_id="course",
            course_url="https://example.test/course/",
            name="Example Course",
            raw_payload={"captured_at": "first"},
            offerings=[
                ScrapedOfferingDTO(
                    list_amount=Decimal("100"),
                    final_amount=Decimal("90"),
                    raw_payload={"html": "first"},
                    schedules=[
                        ScheduleItemSchema(
                            day_of_week=3,
                            start_time=time(19),
                            end_time=time(21),
                        ),
                        ScheduleItemSchema(
                            day_of_week=1,
                            start_time=time(19),
                            end_time=time(21),
                        ),
                    ],
                )
            ],
        )
        second = first.model_copy(deep=True)
        second.raw_payload = {"captured_at": "second"}
        second.offerings[0].raw_payload = {"html": "second"}
        second.offerings[0].schedules.reverse()

        self.assertEqual(first.canonical_fingerprint(), second.canonical_fingerprint())
        second.offerings[0].final_amount = Decimal("80")
        self.assertNotEqual(first.canonical_fingerprint(), second.canonical_fingerprint())
