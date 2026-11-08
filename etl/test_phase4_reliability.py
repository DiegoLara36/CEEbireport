from contextlib import nullcontext
from io import StringIO
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from etl.extractors.cibertec import CibertecExtractionError, CibertecExtractor
from etl.persistence.repositories import (
    CourseRepository,
    IdentityCollisionError,
    IngestionRepository,
)
from etl.services.etl_engine import ETLEngine


class Phase4ReliabilityOfflineTests(SimpleTestCase):
    def test_playwright_context_uses_notebook_user_agent_and_bounded_timeouts(self):
        playwright_module = ModuleType("playwright")
        playwright_module.__path__ = []
        sync_api_module = ModuleType("playwright.sync_api")
        sync_api_module.TimeoutError = TimeoutError
        runtime = Mock()
        browser = Mock()
        context = Mock()
        page = Mock()
        runtime.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page
        manager = Mock()
        manager.start.return_value = runtime
        sync_api_module.sync_playwright = Mock(return_value=manager)
        playwright_module.sync_api = sync_api_module
        extractor = CibertecExtractor(timeout=3)

        with patch.dict(
            sys.modules,
            {
                "playwright": playwright_module,
                "playwright.sync_api": sync_api_module,
            },
        ):
            extractor.start()

        runtime.chromium.launch.assert_called_once_with(
            headless=True, timeout=3000
        )
        self.assertIn("Chrome/120.0.0.0", browser.new_context.call_args.kwargs["user_agent"])
        page.set_default_timeout.assert_called_once_with(3000)
        page.set_default_navigation_timeout.assert_called_once_with(3000)
        extractor.close()
        browser.close.assert_called_once_with()
        runtime.stop.assert_called_once_with()

    def test_external_id_and_canonical_url_collision_is_rejected(self):
        cursor = Mock()
        cursor.fetchone.side_effect = [
            (11, "external", "ACTIVE", None, False, False),
            None,
            (12, "url", "ACTIVE", None, False, False),
        ]

        with self.assertRaises(IdentityCollisionError):
            CourseRepository._find_course(
                cursor, 5, "external-id", "https://example.test/other-course"
            )

        self.assertEqual(cursor.execute.call_count, 3)

    def test_identity_collision_follows_the_quarantine_path(self):
        engine = ETLEngine("PUCP")
        engine.ensure_sources = Mock()
        engine.ingestion_repository.source_id = Mock(return_value=1)
        engine.ingestion_repository.try_source_lock = Mock(return_value=True)
        engine.ingestion_repository.recover_abandoned = Mock(return_value=[])
        engine.ingestion_repository.release_source_lock = Mock(return_value=True)
        item = {"url": "https://example.test/course"}
        engine.extractor.fetch_catalog_index = Mock(return_value=[item])
        engine.extractor.catalog_complete = True
        engine._start_ingestion = Mock(return_value=(1, 2))
        engine._active_course_count = Mock(return_value=1)
        engine._extract_one = Mock(return_value=Mock())
        collision = IdentityCollisionError("ambiguous identity")
        engine._persist_course = Mock(side_effect=collision)
        engine._quarantine = Mock()
        engine._save_raw_pages = Mock(return_value=0)
        engine._minimum_courses_pct = Mock(return_value=20)
        engine._mark_missing = Mock()
        engine._finish_ingestion = Mock()
        engine._purge_retained_data = Mock()
        engine.resilience.failed = Mock()
        cursor = Mock()

        with (
            patch(
                "etl.services.etl_engine.connection",
                SimpleNamespace(cursor=Mock(return_value=nullcontext(cursor))),
            ),
            patch(
                "etl.services.etl_engine.transaction.atomic",
                return_value=nullcontext(),
            ),
        ):
            result = engine.run()

        engine._quarantine.assert_called_once_with(1, 2, item, collision)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["status"], "FAILED")

    def test_engine_skips_source_when_another_session_holds_lock(self):
        engine = ETLEngine("PUCP")
        engine.ensure_sources = Mock()
        engine.ingestion_repository.source_id = Mock(return_value=7)
        engine.ingestion_repository.try_source_lock = Mock(return_value=False)
        engine.ingestion_repository.release_source_lock = Mock()
        engine.extractor.fetch_catalog_index = Mock()
        cursor = Mock()
        engine_cursor_context = nullcontext(cursor)

        with patch(
            "etl.services.etl_engine.connection",
            SimpleNamespace(cursor=Mock(return_value=engine_cursor_context)),
        ):
            result = engine.run()

        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(result["skip_reason"], "SOURCE_LOCKED")
        engine.extractor.fetch_catalog_index.assert_not_called()
        engine.ingestion_repository.release_source_lock.assert_not_called()

    def test_critical_cibertec_initialization_failure_marks_run_failed_and_releases_lock(
        self,
    ):
        engine = ETLEngine("CIBERTEC")
        engine.ensure_sources = Mock()
        engine.ingestion_repository.source_id = Mock(return_value=7)
        engine.ingestion_repository.try_source_lock = Mock(return_value=True)
        engine.ingestion_repository.recover_abandoned = Mock(return_value=[])
        engine.ingestion_repository.release_source_lock = Mock(return_value=True)
        engine._start_ingestion = Mock(return_value=(7, 8))
        engine._active_course_count = Mock(return_value=1)
        engine.extractor.start = Mock(
            side_effect=CibertecExtractionError(
                "Timeout (30000 ms) al iniciar Chromium para Cibertec."
            )
        )
        engine._finish_ingestion = Mock()
        engine._purge_retained_data = Mock()
        engine.resilience.failed = Mock()
        cursor = Mock()

        with (
            patch(
                "etl.services.etl_engine.connection",
                SimpleNamespace(cursor=Mock(return_value=nullcontext(cursor))),
            ),
            patch(
                "etl.services.etl_engine.transaction.atomic",
                return_value=nullcontext(),
            ),
        ):
            result = engine.run()

        self.assertEqual(result["status"], "FAILED")
        engine.extractor.start.assert_called_once_with()
        engine._finish_ingestion.assert_called_once()
        self.assertEqual(
            engine._finish_ingestion.call_args.args[2],
            "FAILED",
        )
        engine.ingestion_repository.release_source_lock.assert_called_once_with(
            cursor, 7
        )

    def test_scheduled_engine_rechecks_next_run_after_lock_acquisition(self):
        engine = ETLEngine("PUCP")
        engine.ensure_sources = Mock()
        engine.ingestion_repository.source_id = Mock(return_value=7)
        engine.ingestion_repository.try_source_lock = Mock(return_value=True)
        engine.ingestion_repository.release_source_lock = Mock(return_value=True)
        engine._source_is_due = Mock(return_value=False)
        engine._start_ingestion = Mock()
        cursor = Mock()

        with patch(
            "etl.services.etl_engine.connection",
            SimpleNamespace(cursor=Mock(return_value=nullcontext(cursor))),
        ):
            result = engine.run(respect_schedule=True)

        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(result["skip_reason"], "NOT_DUE")
        engine._start_ingestion.assert_not_called()
        engine.ingestion_repository.release_source_lock.assert_called_once_with(
            cursor, 7
        )

    def test_abandoned_recovery_updates_only_running_rows(self):
        cursor = Mock()
        cursor.fetchall.return_value = [(21,), (22,)]

        recovered = IngestionRepository.recover_abandoned(cursor, 9)

        self.assertEqual(recovered, [21, 22])
        self.assertIn("status = 'ABANDONED'", cursor.execute.call_args.args[0])
        self.assertIn("status = 'RUNNING'", cursor.execute.call_args.args[0])

    def test_command_preserves_scheduled_and_forced_run_modes(self):
        cases = (
            (None, False),
            (None, True),
            ("PUCP", True),
            ("CIBERTEC", True),
        )
        for source, force in cases:
            with self.subTest(source=source, force=force):
                cursor = Mock()
                cursor.fetchall.return_value = [(source or "PUCP",)]
                with (
                    patch(
                        "catalog.management.commands.run_ingestion.connection",
                        SimpleNamespace(
                            cursor=Mock(return_value=nullcontext(cursor))
                        ),
                    ),
                    patch(
                        "catalog.management.commands.run_ingestion.transaction.atomic",
                        return_value=nullcontext(),
                    ),
                    patch(
                        "catalog.management.commands.run_ingestion.read_settings",
                        return_value={},
                    ),
                    patch(
                        "catalog.management.commands.run_ingestion.IngestionRepository.purge_retained_data"
                    ),
                    patch(
                        "catalog.management.commands.run_ingestion.ETLEngine.ensure_sources"
                    ),
                    patch(
                        "catalog.management.commands.run_ingestion.ETLEngine"
                    ) as engine_class,
                ):
                    engine_class.return_value.run.return_value = {
                        "status": "COMPLETED",
                        "quality_status": "OK",
                        "discovered": 1,
                        "processed_ok": 1,
                        "quarantined": 0,
                    }
                    arguments = []
                    if source:
                        arguments.extend(["--source", source])
                    if force:
                        arguments.append("--force")
                    call_command("run_ingestion", *arguments, stdout=StringIO())

                engine_class.assert_called_once_with(source or "PUCP")
                engine_class.return_value.run.assert_called_once_with(
                    respect_schedule=not force
                )


if __name__ == "__main__":
    unittest.main()
