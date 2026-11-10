import os
import unittest

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

    def test_bi_views_compile_and_read_role_has_only_bi_access(self):
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

            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(%s, 'public.course', 'SELECT');",
                    (config["user"],),
                ).fetchone()[0]
            )
