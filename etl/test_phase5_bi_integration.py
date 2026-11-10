import os
import unittest
from uuid import uuid4

import psycopg
from django.test import SimpleTestCase

from etl.phase3_pg_test_support import test_database_config


POSTGRES_TEST_CONFIGURED = os.getenv("CEEBI_RUN_PHASE5_PG_TESTS") == "1"


@unittest.skipUnless(
    POSTGRES_TEST_CONFIGURED,
    "Use python -m etl.run_phase3_pg_integration para validar PostgreSQL.",
)
class Phase5BIViewsIntegrationTests(SimpleTestCase):
    databases = set()

    def test_bi_views_compile_and_configured_role_can_read_them(self):
        config = test_database_config()
        with psycopg.connect(**config) as conn:
            self.assertTrue(
                conn.execute(
                    "SELECT has_schema_privilege(%s, 'bi', 'USAGE');",
                    (config["user"],),
                ).fetchone()[0]
            )
            for view_name in (
                "v_current_catalog",
                "v_kpi_summary",
                "v_courses_by_category",
                "v_price_history",
            ):
                with self.subTest(view=view_name):
                    self.assertTrue(
                        conn.execute(
                            "SELECT has_table_privilege(%s, %s, 'SELECT');",
                            (config["user"], f"bi.{view_name}"),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        f"SELECT * FROM bi.{view_name} LIMIT 0;"
                    )

    def test_catalog_grain_and_hourly_cost_do_not_multiply_child_rows(self):
        config = test_database_config()
        conn = psycopg.connect(**config)
        try:
            suffix = uuid4().hex
            source_id = conn.execute(
                """
                INSERT INTO source (code, name, active)
                VALUES ('PUCP', 'BI integration', TRUE)
                ON CONFLICT (code) DO UPDATE SET active = TRUE
                RETURNING id;
                """
            ).fetchone()[0]
            category_id = conn.execute(
                """
                INSERT INTO category (source_id, name)
                VALUES (%s, %s)
                RETURNING id;
                """,
                (source_id, f"Test category {suffix}"),
            ).fetchone()[0]
            modality_id = conn.execute(
                """
                INSERT INTO modality (code, name)
                VALUES (%s, 'Virtual')
                RETURNING id;
                """,
                (f"BI_TEST_{suffix}",),
            ).fetchone()[0]
            course_id = conn.execute(
                """
                INSERT INTO course
                    (source_id, external_id, name, category_id, course_url,
                     duration_hours, state)
                VALUES (%s, %s, 'BI hourly-cost fixture', %s,
                        'https://example.test/bi-hourly-cost', 24, 'ACTIVE')
                RETURNING id;
                """,
                (source_id, f"bi-{suffix}", category_id),
            ).fetchone()[0]
            offering_id = conn.execute(
                """
                INSERT INTO course_offering (course_id, modality_id, state)
                VALUES (%s, %s, 'ACTIVE')
                RETURNING id;
                """,
                (course_id, modality_id),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO price_snapshot
                    (offering_id, currency, list_amount, final_amount, valid_from)
                VALUES (%s, 'PEN', 500, 450, now());
                """,
                (offering_id,),
            )
            professor_ids = [
                conn.execute(
                    "INSERT INTO professor (full_name) VALUES (%s) RETURNING id;",
                    (f"BI Teacher {suffix} {index}",),
                ).fetchone()[0]
                for index in range(2)
            ]
            for professor_id in professor_ids:
                conn.execute(
                    "INSERT INTO offering_professor (offering_id, professor_id) VALUES (%s, %s);",
                    (offering_id, professor_id),
                )
            for day in (1, 2):
                conn.execute(
                    """
                    INSERT INTO offering_schedule
                        (offering_id, day_of_week, start_time, end_time)
                    VALUES (%s, %s, '09:00', '10:00');
                    """,
                    (offering_id, day),
                )

            catalog_rows = conn.execute(
                """
                SELECT cost_per_hour, instructors, normalized_category
                FROM bi.v_current_catalog
                WHERE course_id = %s;
                """,
                (course_id,),
            ).fetchall()
            self.assertEqual(len(catalog_rows), 1)
            self.assertEqual(catalog_rows[0][0], 18.75)
            self.assertEqual(len(catalog_rows[0][1]), 2)
            self.assertEqual(catalog_rows[0][2], f"test_category_{suffix}")

            summary_cost = conn.execute(
                """
                SELECT average_cost_per_hour
                FROM bi.v_kpi_summary
                WHERE source_code = 'PUCP' AND currency = 'PEN';
                """
            ).fetchone()[0]
            expected_cost = conn.execute(
                """
                SELECT AVG(ps.final_amount /
                           NULLIF(COALESCE(o.duration_hours, c.duration_hours), 0))
                FROM course_offering o
                JOIN course c ON c.id = o.course_id
                JOIN source s ON s.id = c.source_id
                JOIN price_snapshot ps ON ps.offering_id = o.id AND ps.is_current
                WHERE s.code = 'PUCP' AND o.state = 'ACTIVE'
                  AND c.state = 'ACTIVE' AND ps.currency = 'PEN';
                """
            ).fetchone()[0]
            self.assertAlmostEqual(float(summary_cost), float(expected_cost))
        finally:
            conn.rollback()
            conn.close()
