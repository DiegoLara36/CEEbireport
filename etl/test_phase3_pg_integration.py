import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import psycopg
from django.test import SimpleTestCase

from etl.persistence.repositories import CourseRepository, OfferingRepository
from etl.phase3_pg_test_support import test_database_config
from etl.schemas import (
    ObservationStatus,
    ProfessorSchema,
    ScrapedCourseDTO,
    ScrapedOfferingDTO,
)


class Phase3TestDatabaseSafetyTests(SimpleTestCase):
    def test_test_database_name_defaults_to_isolated_test_database(self):
        with patch(
            "etl.phase3_pg_test_support.dotenv_values",
            return_value={"DB_NAME": "application"},
        ):
            config = test_database_config(
                {
                    "CEEBI_TEST_DB_USER": "test-role",
                    "CEEBI_TEST_DB_PASSWORD": "test-password",
                }
            )

        self.assertEqual(config["dbname"], "ceebi_phase3_test")

    def test_test_database_configuration_can_be_loaded_from_dotenv(self):
        test_values = {
            "DB_NAME": "application",
            "CEEBI_TEST_DB_NAME": "ceebi_phase3_test",
            "CEEBI_TEST_DB_USER": "test-role",
            "CEEBI_TEST_DB_PASSWORD": "test-password",
            "CEEBI_TEST_DB_HOST": "localhost",
            "CEEBI_TEST_DB_PORT": "5432",
        }
        with patch("etl.phase3_pg_test_support.dotenv_values", return_value=test_values):
            config = test_database_config({})

        self.assertEqual(config["dbname"], "ceebi_phase3_test")
        self.assertEqual(config["user"], "test-role")

    def test_unsafe_database_names_are_rejected_before_connection(self):
        with patch(
            "etl.phase3_pg_test_support.dotenv_values",
            return_value={"DB_NAME": "ceebi_phase3_test"},
        ):
            with self.assertRaisesRegex(RuntimeError, "terminar en 'test'"):
                test_database_config(
                    {
                        "CEEBI_TEST_DB_NAME": "ceebi_phase3",
                        "CEEBI_TEST_DB_USER": "test-role",
                        "CEEBI_TEST_DB_PASSWORD": "test-password",
                    }
                )
            with self.assertRaisesRegex(RuntimeError, "coincide con DB_NAME"):
                test_database_config(
                    {
                        "CEEBI_TEST_DB_NAME": "ceebi_phase3_test",
                        "CEEBI_TEST_DB_USER": "test-role",
                        "CEEBI_TEST_DB_PASSWORD": "test-password",
                    }
                )


POSTGRES_TEST_CONFIGURED = (
    os.getenv("CEEBI_RUN_PHASE3_PG_TESTS") == "1"
)


@unittest.skipUnless(
    POSTGRES_TEST_CONFIGURED,
    "Configure CEEBI_TEST_DB_* y use python -m etl.run_phase3_pg_integration.",
)
class Phase3PostgreSQLIntegrationTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self.conn = psycopg.connect(**test_database_config())

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _source(self):
        source_id = self.conn.execute(
            """
            INSERT INTO source (code, name)
            VALUES (%s, %s)
            RETURNING id;
            """,
            (f"PHASE3_{uuid4().hex}", "Phase 3 integration"),
        ).fetchone()[0]
        return source_id

    def _ingestion(self, source_id):
        return self.conn.execute(
            """
            INSERT INTO ingestion_log (source_id, status, counts)
            VALUES (%s, 'COMPLETED', '{}'::jsonb)
            RETURNING id;
            """,
            (source_id,),
        ).fetchone()[0]

    @staticmethod
    def _dto(external_id="course-1", url="https://example.test/course/"):
        return ScrapedCourseDTO(
            source_code="TEST",
            external_id=external_id,
            course_url=url,
            name="Integration course",
            description="Initial description",
            offerings=[
                ScrapedOfferingDTO(
                    external_code="edition-1",
                    list_amount=None,
                    final_amount=None,
                )
            ],
        )

    def _persist(self, source_id, ingestion_id, dto):
        with self.conn.cursor() as cursor:
            return CourseRepository().persist(
                cursor, source_id, ingestion_id, dto
            )

    def test_migration_objects_and_permanent_ingestion_foreign_key(self):
        extension = self.conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist');"
        ).fetchone()[0]
        self.assertTrue(extension)

        fk = self.conn.execute(
            """
            SELECT a.attnotnull, c.confdeltype
              FROM pg_attribute a
              JOIN pg_constraint c
                ON c.conrelid = a.attrelid AND c.conkey = ARRAY[a.attnum]
             WHERE a.attrelid = 'course_version'::regclass
               AND a.attname = 'ingestion_id'
               AND c.contype = 'f';
            """
        ).fetchone()
        self.assertEqual(fk, (True, "r"))
        self.assertTrue(
            self.conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'ex_course_version_no_overlap'
                );
                """
            ).fetchone()[0]
        )

    def test_ingestion_rows_cannot_be_purged(self):
        source_id = self._source()
        ingestion_id = self._ingestion(source_id)

        with self.assertRaises(psycopg.errors.RaiseException):
            with self.conn.transaction():
                self.conn.execute(
                    "DELETE FROM ingestion_log WHERE id = %s;",
                    (ingestion_id,),
                )

        self.assertTrue(
            self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM ingestion_log WHERE id = %s);",
                (ingestion_id,),
            ).fetchone()[0]
        )

    def test_exclusion_constraint_rejects_overlapping_course_versions(self):
        source_id = self._source()
        ingestion_id = self._ingestion(source_id)
        course_id = self.conn.execute(
            """
            INSERT INTO course (source_id, external_id, name)
            VALUES (%s, 'temporal-course', 'Temporal course')
            RETURNING id;
            """,
            (source_id,),
        ).fetchone()[0]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.conn.execute(
            """
            INSERT INTO course_version
                (course_id, ingestion_id, fingerprint, attributes, valid_from, valid_to)
            VALUES (%s, %s, 'one', '{}'::jsonb, %s, %s);
            """,
            (course_id, ingestion_id, start, end),
        )

        with self.assertRaises(psycopg.errors.ExclusionViolation):
            with self.conn.transaction():
                self.conn.execute(
                    """
                    INSERT INTO course_version
                        (course_id, ingestion_id, fingerprint, attributes,
                         valid_from, valid_to)
                    VALUES (%s, %s, 'overlap', '{}'::jsonb, %s, NULL);
                    """,
                    (course_id, ingestion_id, datetime(2026, 1, 15, tzinfo=timezone.utc)),
                )

    def test_price_snapshot_trigger_closes_the_previous_current_row(self):
        source_id = self._source()
        course_id = self.conn.execute(
            """
            INSERT INTO course (source_id, external_id, name)
            VALUES (%s, 'price-course', 'Price course')
            RETURNING id;
            """,
            (source_id,),
        ).fetchone()[0]
        offering_id = self.conn.execute(
            """
            INSERT INTO course_offering (course_id, external_code)
            VALUES (%s, 'edition')
            RETURNING id;
            """,
            (course_id,),
        ).fetchone()[0]
        first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.conn.execute(
            """
            INSERT INTO price_snapshot
                (offering_id, currency, list_amount, final_amount, valid_from)
            VALUES (%s, 'PEN', 100, 90, %s);
            """,
            (offering_id, first),
        )
        self.conn.execute(
            """
            INSERT INTO price_snapshot
                (offering_id, currency, list_amount, final_amount, valid_from)
            VALUES (%s, 'PEN', 120, 110, %s);
            """,
            (offering_id, second),
        )

        rows = self.conn.execute(
            """
            SELECT valid_from, valid_to FROM price_snapshot
             WHERE offering_id = %s ORDER BY valid_from;
            """,
            (offering_id,),
        ).fetchall()
        self.assertEqual(rows, [(first, second), (second, None)])

    def test_repeated_capture_is_idempotent_and_business_changes_create_versions(self):
        source_id = self._source()
        runs = [self._ingestion(source_id) for _ in range(3)]
        dto = self._dto()

        first = self._persist(source_id, runs[0], dto)
        unchanged = self._persist(source_id, runs[1], dto.model_copy(deep=True))
        changed_dto = dto.model_copy(deep=True)
        changed_dto.description = "Changed description"
        changed_dto.offerings[0].duration_hours = Decimal("12")
        changed_dto.offerings[0].duration_observation = ObservationStatus.OBSERVED
        changed = self._persist(source_id, runs[2], changed_dto)

        self.assertEqual(first["course_changed"], 1)
        self.assertEqual(unchanged, {"course_changed": 0, "offerings_changed": 0})
        self.assertEqual(changed["course_changed"], 1)
        counts = self.conn.execute(
            """
            SELECT
                (SELECT count(*) FROM course_version WHERE course_id = c.id),
                (SELECT count(*) FROM course_offering_version v
                  JOIN course_offering o ON o.id = v.offering_id
                 WHERE o.course_id = c.id),
                (SELECT count(*) FROM offering_commercial_version v
                  JOIN course_offering o ON o.id = v.offering_id
                 WHERE o.course_id = c.id)
              FROM course c
             WHERE c.source_id = %s AND c.external_id = 'course-1';
            """,
            (source_id,),
        ).fetchone()
        self.assertEqual(counts, (2, 2, 1))

    def test_canonical_url_reconciles_a_new_external_identifier(self):
        source_id = self._source()
        runs = [self._ingestion(source_id) for _ in range(2)]
        initial = self._dto(
            external_id="old-id",
            url="https://example.test/course/?utm_source=first",
        )
        replacement = self._dto(
            external_id="new-id",
            url="https://example.test/course/#details",
        )
        self._persist(source_id, runs[0], initial)
        self._persist(source_id, runs[1], replacement)

        rows = self.conn.execute(
            """
            SELECT count(DISTINCT c.id), count(a.id)
              FROM course c
              JOIN course_identity_alias a ON a.course_id = c.id
             WHERE c.source_id = %s;
            """,
            (source_id,),
        ).fetchone()
        self.assertEqual(rows, (1, 2))

    def test_failed_course_savepoint_rolls_back_without_aborting_other_courses(self):
        source_id = self._source()
        ingestion_id = self._ingestion(source_id)
        repository = CourseRepository()
        failed_dto = self._dto("failed-course")

        with self.assertRaisesRegex(RuntimeError, "simulated course failure"):
            with self.conn.transaction():
                with self.conn.cursor() as cursor:
                    repository.persist(cursor, source_id, ingestion_id, failed_dto)
                raise RuntimeError("simulated course failure")

        with self.conn.cursor() as cursor:
            saved = repository.persist(
                cursor, source_id, ingestion_id, self._dto("successful-course")
            )
        self.assertEqual(saved["course_changed"], 1)
        courses = self.conn.execute(
            "SELECT external_id FROM course WHERE source_id = %s;",
            (source_id,),
        ).fetchall()
        self.assertEqual(courses, [("successful-course",)])

    def test_homonymous_professors_remain_scoped_to_each_source(self):
        source_ids = [self._source(), self._source()]
        offering_ids = []
        for index, source_id in enumerate(source_ids):
            course_id = self.conn.execute(
                """
                INSERT INTO course (source_id, external_id, name)
                VALUES (%s, %s, 'Professor course')
                RETURNING id;
                """,
                (source_id, f"prof-course-{index}"),
            ).fetchone()[0]
            offering_ids.append(
                self.conn.execute(
                    """
                    INSERT INTO course_offering (course_id, external_code)
                    VALUES (%s, 'edition')
                    RETURNING id;
                    """,
                    (course_id,),
                ).fetchone()[0]
            )

        attributes = {
            "professors_observation": ObservationStatus.OBSERVED.value,
            "professors": [
                ProfessorSchema(full_name="Alex Example", specialty="Data").model_dump()
            ],
        }
        for source_id, offering_id in zip(source_ids, offering_ids):
            with self.conn.cursor() as cursor:
                OfferingRepository.sync_course_relations(
                    cursor, source_id, 0, offering_id, attributes
                )

        professor_ids = self.conn.execute(
            """
            SELECT psl.source_id, psl.professor_id
              FROM professor_source_link psl
              JOIN professor p ON p.id = psl.professor_id
             WHERE lower(p.full_name) = lower('Alex Example')
             ORDER BY psl.source_id;
            """
        ).fetchall()
        self.assertEqual(len(professor_ids), 2)
        self.assertNotEqual(professor_ids[0][1], professor_ids[1][1])
