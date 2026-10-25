from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse


class DashboardTests(SimpleTestCase):
    def setUp(self):
        self.connection_context = patch("catalog.views.connection")
        self.connection_mock = self.connection_context.start()
        self.cursor = (
            self.connection_mock.cursor.return_value.__enter__.return_value
        )
        self.cursor.fetchone.return_value = (
            2,
            datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.cursor.fetchall.side_effect = [
            [("Humanidades",), ("Tecnología",)],
            [
                (
                    1,
                    "Curso de Python",
                    "Tecnología",
                    "https://example.com/python",
                    24,
                    True,
                    None,
                    None,
                    "Virtual",
                    500,
                    450,
                    "PEN",
                ),
                (
                    2,
                    "Curso de Historia",
                    "Humanidades",
                    "https://example.com/history",
                    18,
                    False,
                    None,
                    None,
                    "Presencial",
                    None,
                    None,
                    None,
                ),
            ],
        ]

    def tearDown(self):
        self.connection_context.stop()

    def test_dashboard_renders_database_metrics_and_course_details(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Curso de Python")
        self.assertContains(response, "Tecnología")
        self.assertContains(response, "450,00")
        self.assertEqual(response.context["total_courses"], 2)
        self.assertEqual(response.context["priced_courses"], 1)
        self.assertEqual(response.context["category_total"], 2)

    def test_dashboard_passes_search_and_category_filters_to_database(self):
        self.cursor.fetchall.side_effect = [
            [("Humanidades",), ("Tecnología",)],
            [
                (
                    1,
                    "Curso de Python",
                    "Tecnología",
                    "https://example.com/python",
                    24,
                    True,
                    None,
                    None,
                    "Virtual",
                    500,
                    450,
                    "PEN",
                )
            ],
        ]

        response = self.client.get(
            reverse("dashboard"),
            {"category": "Tecnología", "q": "python"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Curso de Python")
        self.assertEqual(response.context["total_courses"], 1)
        self.assertEqual(self.cursor.execute.call_args_list[2].args[1][-2:], [
            "Tecnología",
            "Tecnología",
        ])
        self.assertEqual(
            self.cursor.execute.call_args_list[2].args[1][1],
            "python",
        )

    def test_etl_flow_explains_the_database_pipeline(self):
        response = self.client.get(reverse("etl_flow"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "seed_catalog")
        self.assertContains(response, "PostgreSQL")
