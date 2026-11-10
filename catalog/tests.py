from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from catalog.views import (
    CATEGORY_CHART_SQL,
    COURSE_TOTALS_SQL,
    COURSES_SQL,
    FILTER_OPTIONS_SQL,
    KPI_SQL,
)


class DashboardTests(SimpleTestCase):
    def setUp(self):
        self.connection_context = patch("catalog.views.connection")
        self.connection_mock = self.connection_context.start()
        self.cursor = (
            self.connection_mock.cursor.return_value.__enter__.return_value
        )
        columns_by_sql = {
            KPI_SQL: (
                "source_code", "currency", "active_courses", "current_offers",
                "average_price", "average_cost_per_hour", "average_duration_hours",
                "discount_or_installment_pct",
            ),
            FILTER_OPTIONS_SQL: ("category_name", "modality"),
            COURSE_TOTALS_SQL: ("source_code", "count"),
            COURSES_SQL: (
                "course_id", "offering_id", "source_code", "course_name",
                "category_name", "modality", "duration_hours", "duration_weeks",
                "course_url", "has_certificate", "list_amount", "final_amount",
                "currency", "cost_per_hour", "has_discount", "has_financing",
                "instructors", "certificates", "start_date", "end_date",
            ),
            CATEGORY_CHART_SQL: (
                "source_code", "currency", "normalized_category", "category_name",
                "modality", "course_count", "average_cost_per_hour",
            ),
        }

        def set_description(sql, *args):
            self.cursor.description = [
                (column,) for column in columns_by_sql[sql]
            ]

        self.cursor.execute.side_effect = set_description
        self.cursor.fetchall.side_effect = [
            [
                (
                    "PUCP",
                    "PEN",
                    1,
                    1,
                    Decimal("450"),
                    Decimal("18.75"),
                    Decimal("24"),
                    Decimal("50"),
                ),
                (
                    "CIBERTEC",
                    "PEN",
                    1,
                    1,
                    Decimal("600"),
                    Decimal("25"),
                    Decimal("24"),
                    Decimal("0"),
                ),
            ],
            [("Tecnología", "Virtual"), ("Humanidades", "Presencial")],
            [("PUCP", 1), ("CIBERTEC", 1)],
            [
                (
                    1, 11, "PUCP", "Curso de Python", "Tecnología", "Virtual",
                    Decimal("24"), Decimal("4"), "https://example.com/python",
                    True, Decimal("500"), Decimal("450"), "PEN",
                    Decimal("18.75"), True, False, ["Ada Lovelace"],
                    ["Certificado"], None, None,
                ),
                (
                    2, 22, "CIBERTEC", "Curso de Historia", "Humanidades",
                    "Presencial", Decimal("24"), None,
                    "https://example.com/history", False, Decimal("600"),
                    Decimal("600"), "PEN", Decimal("25"), False, False,
                    [], [], None, None,
                ),
            ],
            [
                (
                    "PUCP", "PEN", "tecnologia", "Tecnología", "Virtual", 1,
                    Decimal("18.75"),
                ),
                (
                    "CIBERTEC", "PEN", "humanidades", "Humanidades",
                    "Presencial", 1, Decimal("25"),
                ),
            ],
        ]

    def tearDown(self):
        self.connection_context.stop()

    def test_dashboard_shows_title_sources_and_hourly_kpi_from_bi_views(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Reporte BI de Centro de Especialización Ejecutiva"
        )
        self.assertContains(response, "PUCP")
        self.assertContains(response, "CIBERTEC")
        self.assertContains(response, "Curso de Python")
        self.assertContains(response, "Curso de Historia")
        self.assertContains(response, "18,75")
        self.assertContains(response, "Ada Lovelace")
        self.assertEqual(response.context["course_counts"]["PUCP"], 1)
        self.assertEqual(
            response.context["currency_kpis"]["PEN"][0]["average_cost_per_hour"],
            Decimal("18.75"),
        )

    def test_dashboard_applies_source_modality_category_currency_and_search(self):
        self.cursor.fetchall.side_effect = [
            [],
            [("Tecnología", "Virtual"), ("Humanidades", "Presencial")],
            [("PUCP", 1), ("CIBERTEC", 1)],
            [],
            [],
        ]

        response = self.client.get(
            reverse("dashboard"),
            {
                "source": "PUCP",
                "modality": "Virtual",
                "category": "Tecnología",
                "currency": "PEN",
                "q": "python",
            },
        )

        self.assertEqual(response.status_code, 200)
        query_call = self.cursor.execute.call_args_list[3]
        self.assertEqual(
            query_call.args[1],
            (
                "PUCP", "PUCP", "Virtual", "Virtual", "Tecnología", "Tecnología",
                "PEN", "PEN", "python", "%python%", "%python%",
            ),
        )


class ETLFlowTests(SimpleTestCase):
    def test_etl_flow_explains_the_database_pipeline(self):
        response = self.client.get(reverse("etl_flow"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "seed_catalog")
        self.assertContains(response, "PostgreSQL")
