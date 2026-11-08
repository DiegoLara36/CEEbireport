from datetime import date, time
from decimal import Decimal
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase
from pydantic import ValidationError

from etl.extractors.cibertec import CibertecExtractionError, CibertecExtractor
from etl.extractors.pucp import PucpExtractor
from etl.persistence.repositories import PriceRepository
from etl.schemas import (
    ContactSchema,
    CourseExtractedSchema,
    ProfessorSchema,
    ScheduleItemSchema,
    ScrapedCourseDTO,
    ScrapedDiscountDTO,
    ScrapedOfferingDTO,
    ScrapedPaymentPlanDTO,
    ScrapedPriceTierDTO,
    pucp_course_to_dto,
)
from etl.services.etl_engine import ETLEngine


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class PydanticContractBaselineTests(SimpleTestCase):
    def test_schedule_and_nested_contracts_validate(self):
        schedule = ScheduleItemSchema(
            day_of_week=2,
            start_time=time(19),
            end_time=time(22),
        )
        professor = ProfessorSchema(full_name="Ana Docente", specialty="Analítica")
        contact = ContactSchema(name="María Contacto", email="maria@example.test")
        discount = ScrapedDiscountDTO(kind="PERCENT", value=Decimal("25"))
        plan = ScrapedPaymentPlanDTO(installments=3, cards=["Visa"])
        tier = ScrapedPriceTierDTO(segment_code="PUBLICO_GENERAL", amount=Decimal("520"))
        offering = ScrapedOfferingDTO(
            start_date=date(2026, 11, 23),
            schedules=[schedule],
            list_amount=Decimal("970"),
            final_amount=Decimal("727.50"),
            discounts=[discount],
            payment_plans=[plan],
            price_tiers=[tier],
        )
        course = ScrapedCourseDTO(
            source_code="CIBERTEC",
            external_id="analitica",
            course_url="https://example.test/cursos/analitica/",
            name="Actualización en Analítica",
            offerings=[offering],
        )

        self.assertEqual(course.offerings[0].schedules[0].day_of_week, 2)
        self.assertEqual(course.offerings[0].discounts[0].value, Decimal("25"))
        self.assertEqual(course.offerings[0].payment_plans[0].cards, ["Visa"])
        self.assertEqual(course.offerings[0].price_tiers[0].amount, Decimal("520"))
        self.assertEqual(professor.specialty, "Analítica")
        self.assertEqual(contact.email, "maria@example.test")

    def test_required_fields_and_business_validators_reject_invalid_values(self):
        with self.assertRaises(ValidationError):
            ScheduleItemSchema(
                day_of_week=8,
                start_time=time(19),
                end_time=time(22),
            )
        with self.assertRaises(ValidationError):
            ScheduleItemSchema(
                day_of_week=1,
                start_time=time(22),
                end_time=time(19),
            )
        with self.assertRaises(ValidationError):
            ScrapedDiscountDTO(kind="PERCENT", value=Decimal("100.01"))
        with self.assertRaises(ValidationError):
            ScrapedOfferingDTO(
                start_date=date(2026, 12, 2),
                end_date=date(2026, 12, 1),
                list_amount=Decimal("10"),
                final_amount=Decimal("10"),
            )
        with self.assertRaises(ValidationError):
            ScrapedCourseDTO(
                source_code="PUCP",
                external_id="course",
                course_url="https://example.test/course/",
                name="Example Course",
            )

    def test_course_extraction_schema_has_defaults_and_required_price(self):
        course = CourseExtractedSchema(
            external_id="analitica",
            course_url="https://example.test/cursos/analitica/",
            name="Analítica de Datos",
            external_offering_code="2026-11-23",
            price_amount=Decimal("520"),
        )
        self.assertEqual(course.source_code, "PUCP")
        self.assertEqual(course.modality_code, "VIRTUAL_SINC")
        self.assertEqual(course.content_items, [])
        self.assertIsNone(course.professor)
        with self.assertRaises(ValidationError):
            CourseExtractedSchema(
                external_id="analitica",
                course_url="https://example.test/cursos/analitica/",
                name="Analítica de Datos",
                external_offering_code="2026-11-23",
            )


class PucpFixtureBaselineTests(SimpleTestCase):
    def test_catalog_pagination_uses_fixture_pages_and_deduplicates_urls(self):
        first = fixture_text("pucp_catalog_page_1.html")
        second = fixture_text("pucp_catalog_page_2.html")
        extractor = PucpExtractor()

        with (
            patch.object(
                extractor,
                "fetch_page",
                side_effect=[
                    (first, first.encode(), "hash-1"),
                    (second, second.encode(), "hash-2"),
                ],
            ) as fetch_page,
            patch("etl.extractors.pucp.time_module.sleep") as sleep,
        ):
            courses = extractor.fetch_catalog_index()

        self.assertEqual(
            [course["url"] for course in courses],
            [
                "https://educacioncontinua.pucp.edu.pe/cursos/analitica-datos/",
                "https://educacioncontinua.pucp.edu.pe/cursos/gestion-proyectos/",
            ],
        )
        self.assertEqual(courses[0]["categoria"], "Tecnología")
        self.assertEqual(courses[1]["curso"], "Curso de Gestión de Proyectos")
        self.assertEqual(fetch_page.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertTrue(extractor.catalog_complete)

    def test_catalog_skips_a_later_page_when_http_request_fails(self):
        first = fixture_text("pucp_catalog_page_1.html")
        extractor = PucpExtractor()
        with (
            patch.object(
                extractor,
                "fetch_page",
                side_effect=[
                    (first, first.encode(), "hash-1"),
                    requests.ConnectionError("fixture failure"),
                ],
            ),
            patch("etl.extractors.pucp.time_module.sleep"),
        ):
            courses = extractor.fetch_catalog_index()

        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["curso"], "Curso de Analítica de Datos")
        self.assertFalse(extractor.catalog_complete)

    def test_detail_and_adapter_baseline(self):
        html = fixture_text("pucp_course_detail.html")
        extractor = PucpExtractor()
        with patch(
            "etl.extractors.pucp.django_timezone.localdate",
            return_value=date(2026, 10, 1),
        ):
            extracted = extractor.parse_course_detail(
                html,
                "https://educacioncontinua.pucp.edu.pe/cursos/analitica-datos/",
            )
        dto = pucp_course_to_dto(extracted)

        self.assertEqual(extracted.name, "Analítica de Datos")
        self.assertEqual(extracted.start_date, date(2026, 11, 14))
        self.assertEqual(extracted.end_date, date(2026, 12, 20))
        self.assertEqual(extracted.schedules[0].day_of_week, 2)
        self.assertEqual(extracted.professor.full_name, "Ana Docente")
        self.assertEqual(extracted.contact.name, "María Contacto")
        self.assertEqual(dto.source_code, "PUCP")
        self.assertEqual(dto.external_id, "analitica-datos")
        self.assertEqual(dto.offerings[0].list_amount, Decimal("520"))
        self.assertEqual(dto.offerings[0].end_date, date(2026, 12, 20))
        self.assertEqual(dto.content_items, ["Preparación de datos", "Visualización"])

    def test_etl_engine_pucp_path_fetches_and_adapts_detail_without_database(self):
        html = fixture_text("pucp_course_detail.html")
        engine = ETLEngine("PUCP")
        with patch.object(
            engine.extractor,
            "fetch_page",
            return_value=(html, html.encode(), "fixture-hash"),
        ) as fetch_page:
            dto = engine._extract_one(
                {
                    "url": "https://educacioncontinua.pucp.edu.pe/cursos/analitica-datos/",
                    "categoria": "Catálogo fallback",
                }
            )

        fetch_page.assert_called_once()
        self.assertEqual(dto.name, "Analítica de Datos")
        self.assertEqual(dto.source_code, "PUCP")


class CibertecFixtureBaselineTests(SimpleTestCase):
    def test_timeout_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "mayor que cero"):
            CibertecExtractor(timeout=0)

    def test_catalog_navigation_timeout_raises_with_url_context(self):
        extractor = CibertecExtractor(timeout=1)
        page = Mock()
        page.goto.side_effect = TimeoutError("fixture navigation timeout")
        extractor._page = page

        with (
            patch.object(extractor, "start"),
            self.assertRaisesRegex(CibertecExtractionError, "buscador"),
        ):
            extractor.fetch_catalog_index()

    def test_catalog_extraction_parses_fixture_without_browser_or_network(self):
        html = fixture_text("cibertec_catalog.html")
        extractor = CibertecExtractor()
        page = Mock()
        page.goto.return_value.status = 200
        page.url = extractor.base_url
        page.content.return_value = html
        page.locator.return_value.first.count.return_value = 0
        extractor._page = page

        with patch.object(extractor, "start"):
            courses = extractor.fetch_catalog_index()

        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0]["curso"], "Actualización en Analítica")
        self.assertEqual(courses[0]["categoria"], "PROGRAMAS")
        self.assertEqual(courses[0]["list_price"], "S/970")
        self.assertEqual(courses[1]["url"], (
            "https://educacioncontinua.cibertec.edu.pe/cursos/power-bi-avanzado/"
        ))
        self.assertEqual(len(extractor.raw_pages), 1)
        self.assertTrue(extractor.catalog_complete)

    def test_catalog_selector_timeout_raises_and_keeps_raw_html(self):
        html = fixture_text("cibertec_catalog.html")
        extractor = CibertecExtractor()
        page = Mock()
        page.goto.return_value.status = 200
        page.url = extractor.base_url
        page.content.return_value = html
        page.wait_for_selector.side_effect = TimeoutError("fixture timeout")
        extractor._page = page

        with patch.object(extractor, "start"):
            with self.assertRaisesRegex(
                CibertecExtractionError, "#catalog-cursos article"
            ):
                extractor.fetch_catalog_index()

        self.assertEqual(extractor.raw_pages, [(extractor.base_url, html.encode("utf-8"))])
        self.assertFalse(extractor.catalog_complete)

    def test_catalog_pagination_clicks_next_page_and_records_both_fixtures(self):
        first_html = fixture_text("cibertec_catalog.html")
        second_html = fixture_text("cibertec_catalog_page_2.html")
        extractor = CibertecExtractor()
        page = Mock()
        page.goto.return_value.status = 200
        page.url = extractor.base_url
        page.content.side_effect = [first_html, second_html]

        next_button = Mock()
        next_button.first = next_button
        next_button.count.return_value = 1
        next_button.is_disabled.return_value = False
        no_next_button = Mock()
        no_next_button.first = no_next_button
        no_next_button.count.return_value = 0
        page.locator.side_effect = [next_button, no_next_button]
        extractor._page = page

        with patch.object(extractor, "start"):
            courses = extractor.fetch_catalog_index()

        self.assertEqual(
            [course["curso"] for course in courses],
            ["Actualización en Analítica", "Power BI Avanzado", "SQL para Analítica"],
        )
        next_button.click.assert_called_once_with(timeout=extractor.timeout_ms)
        page.wait_for_function.assert_called_once()
        self.assertEqual(len(extractor.raw_pages), 2)
        self.assertTrue(extractor.catalog_complete)

    def test_catalog_pagination_timeout_raises_instead_of_returning_partial_data(self):
        first_html = fixture_text("cibertec_catalog.html")
        extractor = CibertecExtractor(timeout=1)
        page = Mock()
        page.goto.return_value.status = 200
        page.url = extractor.base_url
        page.content.return_value = first_html
        next_button = Mock()
        next_button.first = next_button
        next_button.count.return_value = 1
        next_button.is_disabled.return_value = False
        page.locator.side_effect = [next_button]
        page.wait_for_function.side_effect = TimeoutError("pagination timeout")
        extractor._page = page

        with patch.object(extractor, "start"):
            with self.assertRaisesRegex(CibertecExtractionError, "Timeout.*avanzar"):
                extractor.fetch_catalog_index()

        self.assertFalse(extractor.catalog_complete)

    def test_detail_parser_maps_fixture_content_cohort_and_commercial_fields(self):
        html = fixture_text("cibertec_course_detail.html")
        with patch(
            "etl.extractors.cibertec.timezone.localdate",
            return_value=date(2026, 10, 1),
        ):
            course = CibertecExtractor().parse_course_detail(
                html,
                "https://educacioncontinua.cibertec.edu.pe/programas/tecnologia/analitica/",
                fallback_category="PROGRAMAS",
                modules=[
                    {"name": "Módulo 1", "items": ["Modelado de datos"]},
                ],
                cohorts=[
                    {
                        "start_text": "23 NOV.",
                        "duration_text": "14 semanas (108 hrs.)",
                        "schedule_text": "Miércoles de 7:00 p.m. a 10:00 p.m.",
                    },
                ],
            )
        offering = course.offerings[0]

        self.assertEqual(course.external_id, "analitica")
        self.assertEqual(course.category_name, "Tecnología")
        self.assertEqual(course.content_items, ["Módulo 1 - Modelado de datos"])
        self.assertEqual(course.prerequisite_items, ["Conocimientos básicos de Excel"])
        self.assertEqual(course.hardware_requirements, ["Computadora con conexión a internet"])
        self.assertEqual(course.software_requirements, ["Power BI Desktop"])
        self.assertTrue(course.has_certificate)
        self.assertTrue(course.has_participation_certificate)
        self.assertEqual(offering.start_date, date(2026, 11, 23))
        self.assertEqual(offering.duration_hours, Decimal("108"))
        self.assertEqual(offering.duration_weeks, Decimal("14"))
        self.assertEqual(offering.schedules[0].day_of_week, 3)
        self.assertEqual(offering.list_amount, Decimal("970"))
        self.assertEqual(offering.final_amount, Decimal("727.50"))
        self.assertEqual(offering.discounts[0].value, Decimal("25"))
        self.assertEqual(offering.payment_plans[0].cards, ["Diners", "BCP", "BBVA"])

    def test_etl_engine_cibertec_path_returns_extractor_dto_without_database(self):
        expected = ScrapedCourseDTO(
            source_code="CIBERTEC",
            external_id="analitica",
            course_url="https://example.test/analitica/",
            name="Actualización en Analítica",
            offerings=[
                ScrapedOfferingDTO(
                    list_amount=Decimal("970"),
                    final_amount=Decimal("727.50"),
                )
            ],
        )
        engine = ETLEngine("CIBERTEC")
        item = {"url": str(expected.course_url)}
        with patch.object(
            engine.extractor,
            "fetch_course",
            return_value=(expected, item["url"], b"fixture"),
        ) as fetch_course:
            dto = engine._extract_one(item)

        fetch_course.assert_called_once_with(item)
        self.assertEqual(dto, expected)


class EtlEnginePureLogicBaselineTests(SimpleTestCase):
    def test_extractor_factory_and_canonical_hash_are_deterministic(self):
        self.assertIsInstance(ETLEngine("PUCP").extractor, PucpExtractor)
        self.assertIsInstance(ETLEngine("CIBERTEC").extractor, CibertecExtractor)
        self.assertEqual(ETLEngine._hash({"b": 2, "a": 1}),
                         ETLEngine._hash({"a": 1, "b": 2}))

    def test_unsupported_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Fuente no soportada"):
            ETLEngine("UPC")

    def test_incomplete_catalog_never_marks_unseen_courses_missing(self):
        engine = ETLEngine("PUCP")
        engine.extractor = Mock()
        engine.extractor.fetch_catalog_index.return_value = [{"url": "https://example.test/course"}]
        engine.extractor.catalog_complete = False
        engine._start_ingestion = Mock(return_value=(1, 2))
        engine.ingestion_repository.source_id = Mock(return_value=1)
        engine.ingestion_repository.try_source_lock = Mock(return_value=True)
        engine.ingestion_repository.recover_abandoned = Mock(return_value=[])
        engine.ingestion_repository.release_source_lock = Mock(return_value=True)
        engine._active_course_count = Mock(return_value=1)
        engine._extract_one = Mock(return_value=ScrapedCourseDTO(
            source_code="PUCP",
            external_id="course",
            course_url="https://example.test/course",
            name="Test course",
            offerings=[ScrapedOfferingDTO(list_amount=None, final_amount=None)],
        ))
        engine._persist_course = Mock(
            return_value={"course_changed": 0, "offerings_changed": 0}
        )
        engine._save_raw_pages = Mock(return_value=0)
        engine._minimum_courses_pct = Mock(return_value=20)
        engine._mark_missing = Mock()
        engine._finish_ingestion = Mock()
        engine._purge_retained_data = Mock()
        engine.resilience.succeeded = Mock()
        engine.ensure_sources = Mock()
        cursor = Mock()

        with (
            patch(
                "etl.services.etl_engine.connection",
                SimpleNamespace(cursor=Mock(return_value=nullcontext(cursor))),
            ),
            patch("etl.services.etl_engine.transaction.atomic", return_value=nullcontext()),
        ):
            result = engine.run()

        engine._mark_missing.assert_not_called()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["quality_status"], "PARTIAL")
        self.assertFalse(result["catalog_complete"])

    def test_unchanged_price_reuses_current_snapshot(self):
        engine = ETLEngine("PUCP")
        cursor = Mock()
        cursor.fetchone.return_value = (
            15,
            Decimal("520"),
            Decimal("490"),
            "PEN",
        )
        offering = ScrapedOfferingDTO(
            currency="PEN",
            list_amount=Decimal("520"),
            final_amount=Decimal("490"),
        )

        snapshot_id = PriceRepository()._persist_snapshot(cursor, 9, 6, offering)

        self.assertEqual(snapshot_id, 15)
        cursor.execute.assert_called_once()

    def test_changed_price_creates_snapshot_and_audit_entry(self):
        engine = ETLEngine("PUCP")
        cursor = Mock()
        cursor.fetchone.side_effect = [None, (16,)]
        offering = ScrapedOfferingDTO(
            list_amount=Decimal("520"),
            final_amount=Decimal("490"),
        )

        snapshot_id = PriceRepository()._persist_snapshot(cursor, 9, 6, offering)

        self.assertEqual(snapshot_id, 16)
        sql_calls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO price_snapshot" in sql for sql in sql_calls))
        self.assertTrue(any("INSERT INTO change_log" in sql for sql in sql_calls))
