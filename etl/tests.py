from datetime import date, time
from decimal import Decimal

from django.test import SimpleTestCase

from etl.extractors.cibertec import (
    CibertecExtractor,
    parse_cibertec_schedule,
    parse_cibertec_start_date,
)
from etl.schemas import ScrapedOfferingDTO
from etl.schemas import CourseExtractedSchema, pucp_course_to_dto


class CibertecTransformationTests(SimpleTestCase):
    def test_infers_year_using_local_reference_month(self):
        today = date(2026, 11, 1)

        self.assertEqual(parse_cibertec_start_date("23 NOV.", today), date(2026, 11, 23))
        self.assertEqual(parse_cibertec_start_date("11 DIC.", today), date(2026, 12, 11))
        self.assertEqual(parse_cibertec_start_date("15 ENE.", today), date(2027, 1, 15))

    def test_parses_compound_weekday_schedule(self):
        schedules = parse_cibertec_schedule(
            "Miércoles de 7:00 p.m. a 10:00 p.m. y "
            "Sábados de 9:00 a.m. a 1:00 p.m."
        )

        self.assertEqual(
            [
                (item.day_of_week, item.start_time, item.end_time)
                for item in schedules
            ],
            [
                (3, time(19), time(22)),
                (6, time(9), time(13)),
            ],
        )

    def test_parses_shared_schedule_for_multiple_days(self):
        schedules = parse_cibertec_schedule(
            "Lunes, Miércoles y Viernes de 7:00 p.m. a 10:00 p.m."
        )

        self.assertEqual([item.day_of_week for item in schedules], [1, 3, 5])
        self.assertTrue(all(item.start_time == time(19) for item in schedules))

    def test_offering_code_defaults_to_permanent_without_start_date(self):
        offering = ScrapedOfferingDTO(
            list_amount=Decimal("100"),
            final_amount=Decimal("100"),
        )

        self.assertEqual(offering.external_code, "PERMANENT")

    def test_offering_code_is_deterministic_for_dated_cohorts(self):
        offering = ScrapedOfferingDTO(
            start_date=date(2026, 11, 23),
            schedule_text="Lunes y miércoles 19:00-22:00",
            list_amount=Decimal("100"),
            final_amount=Decimal("75"),
        )

        self.assertEqual(
            offering.external_code,
            "2026-11-23_lunes-y-miercoles-1900-2200",
        )

    def test_pucp_price_details_map_tiers_discounts_and_payment_plans(self):
        course = CourseExtractedSchema(
            external_id="analytics",
            course_url="https://example.com/cursos/analytics/",
            name="Data Analytics",
            external_offering_code="123",
            price_amount=Decimal("520"),
            price_details_raw=(
                "Tarifa general: S/ 520. Descuentos especiales: "
                "Comunidad PUCP colaboradores, egresados, estudiantes de pregrado "
                "y posgrado: S/ 398. Alumnos de pregrado otras universidades "
                "actualmente matriculados: S/ 409. Descuento para Asociado Hábil "
                "(ASOCEGRE): 30 %. Descuento para Instituciones Educativas en "
                "convenio: 30 %. Descuento para empresas en convenio: 10 %. "
                "Tarjetas de crédito Diners Club: hasta 12 cuotas sin intereses. "
                "Tarjetas de crédito VISA SCOTIABANK: 3; 6; 9 y 12 cuotas sin intereses."
            ),
        )

        offering = pucp_course_to_dto(course).offerings[0]

        self.assertEqual(offering.list_amount, Decimal("520"))
        self.assertEqual(len(offering.price_tiers), 6)
        self.assertEqual(
            [discount.segment_code for discount in offering.discounts],
            ["ASOCEGRE", "INSTITUCION_CONVENIO", "EMPRESA_CONVENIO"],
        )
        self.assertEqual(
            [(plan.installments, plan.cards) for plan in offering.payment_plans],
            [
                (12, ["Diners Club"]),
                (3, ["VISA SCOTIABANK"]),
                (6, ["VISA SCOTIABANK"]),
                (9, ["VISA SCOTIABANK"]),
                (12, ["VISA SCOTIABANK"]),
            ],
        )

    def test_cibertec_maps_list_price_discount_and_installments(self):
        html = """
        <nav aria-label="Breadcrumb"><a>Inicio</a><a>Curso</a><a>Tecnología</a></nav>
        <h1>Power BI Avanzado</h1>
        <ul><li>Virtual</li><li>12 cuotas sin intereses con tarjetas de Diners, BCP y BBVA</li></ul>
        <p>Antes <span class="line-through">S/970</span></p>
        <p>S/727.50</p>
        <p>25% de Dscto</p>
        """

        course = CibertecExtractor().parse_course_detail(
            html,
            "https://educacioncontinua.cibertec.edu.pe/cursos/power-bi-avanzado/",
        )
        offering = course.offerings[0]

        self.assertEqual(offering.list_amount, Decimal("970"))
        self.assertEqual(offering.final_amount, Decimal("727.50"))
        self.assertEqual(offering.discounts[0].value, Decimal("25"))
        self.assertEqual(
            offering.payment_plans[0].cards,
            ["Diners", "BCP", "BBVA"],
        )
