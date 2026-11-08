import os
import unittest
from datetime import timedelta
from uuid import uuid4

import psycopg
from django.test import SimpleTestCase
from django.utils import timezone

from etl.persistence.repositories import (
    CourseRepository,
    IdentityCollisionError,
    IngestionRepository,
)
from etl.phase3_pg_test_support import test_database_config
from etl.schemas import ScrapedCourseDTO, ScrapedOfferingDTO


POSTGRES_TEST_CONFIGURED = os.getenv("CEEBI_RUN_PHASE4_PG_TESTS") == "1"


@unittest.skipUnless(
    POSTGRES_TEST_CONFIGURED,
    "Configure CEEBI_TEST_DB_* y use python -m etl.run_phase3_pg_integration.",
)
class Phase4PostgreSQLIntegrationTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self.conn = psycopg.connect(**test_database_config())

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _source(self):
        return self.conn.execute(
            """
            INSERT INTO source (code, name)
            VALUES (%s, 'Phase 4 integration')
            RETURNING id;
            """,
            (f"PHASE4_{uuid4().hex}",),
        ).fetchone()[0]

    def _ingestion(self, source_id, *, status="COMPLETED", started_at=None,
                   finished_at=None, error_msg=None):
        return self.conn.execute(
            """
            INSERT INTO ingestion_log
                (source_id, status, started_at, finished_at, counts, error_msg)
            VALUES (%s, %s, %s, %s, '{}'::jsonb, %s)
            RETURNING id;
            """,
            (
                source_id,
                status,
                started_at or timezone.now(),
                finished_at,
                error_msg,
            ),
        ).fetchone()[0]

    def test_session_lock_competition_and_interrupted_run_recovery(self):
        source_id = self._source()
        ingestion_id = self._ingestion(
            source_id, status="RUNNING", started_at=timezone.now()
        )
        self.conn.commit()
        first = psycopg.connect(**test_database_config())
        second = psycopg.connect(**test_database_config())
        try:
            with first.cursor() as cursor:
                self.assertTrue(IngestionRepository.try_source_lock(cursor, source_id))
            with second.cursor() as cursor:
                self.assertFalse(IngestionRepository.try_source_lock(cursor, source_id))

            first.close()
            with second.cursor() as cursor:
                self.assertTrue(IngestionRepository.try_source_lock(cursor, source_id))
                recovered = IngestionRepository.recover_abandoned(cursor, source_id)
            self.assertEqual(recovered, [ingestion_id])
            row = second.execute(
                """
                SELECT status, finished_at, error_msg, counts->>'recovery_reason'
                  FROM ingestion_log WHERE id = %s;
                """,
                (ingestion_id,),
            ).fetchone()
            self.assertEqual(row[0], "ABANDONED")
            self.assertIsNotNone(row[1])
            self.assertIn("interrumpido", row[2])
            self.assertEqual(row[3], "process_interrupted")
            with second.cursor() as cursor:
                self.assertTrue(IngestionRepository.release_source_lock(cursor, source_id))
            second.commit()
        finally:
            if not first.closed:
                first.close()
            if not second.closed:
                second.close()

    def test_retention_redacts_diagnostics_but_preserves_ingestion_history(self):
        source_id = self._source()
        now = timezone.now()
        old_finished = now - timedelta(days=365 * 3)
        ingestion_id = self._ingestion(
            source_id,
            started_at=old_finished,
            finished_at=old_finished,
            error_msg="old diagnostic",
        )
        course_id = self.conn.execute(
            """
            INSERT INTO course (source_id, external_id, name)
            VALUES (%s, 'retention-course', 'Retention course')
            RETURNING id;
            """,
            (source_id,),
        ).fetchone()[0]
        version_id = self.conn.execute(
            """
            INSERT INTO course_version
                (course_id, ingestion_id, fingerprint, attributes, valid_from)
            VALUES (%s, %s, 'retention-version', '{}'::jsonb, %s)
            RETURNING id;
            """,
            (course_id, ingestion_id, old_finished),
        ).fetchone()[0]
        old_html = b"old page"
        self.conn.execute(
            """
            INSERT INTO raw_page (source_id, url, html_sha256, html, fetched_at)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                source_id,
                "https://example.test/old",
                "old-digest",
                old_html,
                now - timedelta(days=91),
            ),
        )
        old_quarantine = self.conn.execute(
            """
            INSERT INTO quarantine_record
                (source_id, ingestion_id, raw_payload, errors, reviewed, resolved,
                 resolved_at)
            VALUES (%s, %s, '{}'::jsonb, '[]'::jsonb, TRUE, TRUE, %s)
            RETURNING id;
            """,
            (source_id, ingestion_id, now - timedelta(days=91)),
        ).fetchone()[0]
        unresolved = self.conn.execute(
            """
            INSERT INTO quarantine_record
                (source_id, ingestion_id, raw_payload, errors, reviewed)
            VALUES (%s, %s, '{}'::jsonb, '[]'::jsonb, FALSE)
            RETURNING id;
            """,
            (source_id, ingestion_id),
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO raw_page (source_id, url, html_sha256, html, fetched_at)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                source_id,
                "https://example.test/recent",
                "recent-digest",
                b"recent page",
                now - timedelta(days=20),
            ),
        )

        with self.conn.cursor() as cursor:
            purged = IngestionRepository.purge_retained_data(
                cursor,
                {
                    "retention_raw_page_days": "90",
                    "retention_resolved_quarantine_days": "90",
                    "retention_ingestion_diagnostic_months": "24",
                },
                now=now,
            )

        self.assertGreaterEqual(purged["raw_pages"], 1)
        self.assertGreaterEqual(purged["quarantines"], 1)
        self.assertGreaterEqual(purged["diagnostics"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT error_msg, counts FROM ingestion_log WHERE id = %s;",
                (ingestion_id,),
            ).fetchone(),
                (None, None),
        )
        self.assertTrue(
            self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM ingestion_log WHERE id = %s);",
                (ingestion_id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT ingestion_id FROM course_version WHERE id = %s;",
                (version_id,),
            ).fetchone()[0],
            ingestion_id,
        )
        self.assertFalse(
            self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM quarantine_record WHERE id = %s);",
                (old_quarantine,),
            ).fetchone()[0]
        )
        self.assertTrue(
            self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM quarantine_record WHERE id = %s);",
                (unresolved,),
            ).fetchone()[0]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM raw_page WHERE source_id = %s;",
                (source_id,),
            ).fetchone()[0],
            1,
        )

        self.conn.execute(
            "UPDATE quarantine_record SET resolved = TRUE WHERE id = %s;",
            (unresolved,),
        )
        resolved_at = self.conn.execute(
            "SELECT resolved_at FROM quarantine_record WHERE id = %s;",
            (unresolved,),
        ).fetchone()[0]
        self.assertIsNotNone(resolved_at)

    def test_canonical_url_reconciles_identity_and_conflicts_are_rejected(self):
        source_id = self._source()
        ingestion_id = self._ingestion(source_id)
        repo = CourseRepository()

        def persist(external_id, url, name):
            dto = ScrapedCourseDTO(
                source_code="PHASE4",
                external_id=external_id,
                course_url=url,
                name=name,
                offerings=[
                    ScrapedOfferingDTO(
                        external_code="edition-1",
                        list_amount=None,
                        final_amount=None,
                    )
                ],
            )
            with self.conn.cursor() as cursor:
                return repo.persist(cursor, source_id, ingestion_id, dto)

        persist("old-id", "https://example.test/course-a", "Course A")
        course_a = self.conn.execute(
            "SELECT id FROM course WHERE source_id = %s AND external_id = 'old-id';",
            (source_id,),
        ).fetchone()[0]
        reconciliation = persist(
            "new-id", "https://example.test/course-a", "Course A renamed"
        )
        self.assertEqual(reconciliation["course_changed"], 1)
        alias_target = self.conn.execute(
            """
            SELECT course_id FROM course_identity_alias
             WHERE source_id = %s AND external_id = 'new-id';
            """,
            (source_id,),
        ).fetchone()[0]
        self.assertEqual(alias_target, course_a)

        persist("other-id", "https://example.test/course-b", "Course B")
        with self.assertRaises(IdentityCollisionError):
            persist("old-id", "https://example.test/course-b", "Ambiguous course")


if __name__ == "__main__":
    unittest.main()
