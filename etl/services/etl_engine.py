import hashlib
import json
import logging
import traceback
from typing import Any

import requests
from django.db import connection, transaction
from django.utils import timezone
from pydantic import ValidationError

from etl.extractors.cibertec import CibertecExtractor
from etl.extractors.pucp import PucpExtractor
from etl.persistence.repositories import CourseRepository, IngestionRepository
from etl.schemas import ScrapedCourseDTO, pucp_course_to_dto
from etl.services.resilience_service import (
    ResilienceService,
    read_settings,
    setting_int,
)

logger = logging.getLogger(__name__)


class IngestionFailure(RuntimeError):
    def __init__(self, message: str, failure_type: str = "HTTP_ERROR"):
        super().__init__(message)
        self.failure_type = failure_type


class ETLEngine:
    SOURCE_METADATA = {
        "PUCP": (
            "Educación Continua PUCP",
            "https://educacioncontinua.pucp.edu.pe/tipo-de-actividad/cursos/",
        ),
        "CIBERTEC": (
            "Educación Continua Cibertec",
            "https://educacioncontinua.cibertec.edu.pe/buscador/",
        ),
    }

    def __init__(self, source_code: str):
        self.source_code = source_code.upper()
        self.resilience = ResilienceService()
        self.extractor = self._make_extractor()
        self.ingestion_repository = IngestionRepository()
        self.course_repository = CourseRepository()

    def _make_extractor(self):
        if self.source_code == "PUCP":
            return PucpExtractor()
        if self.source_code == "CIBERTEC":
            return CibertecExtractor()
        raise ValueError(f"Fuente no soportada: {self.source_code}")

    def run(self, limit: int | None = None) -> dict[str, Any]:
        run_start = timezone.now()
        counts = {
            "discovered": 0,
            "processed_ok": 0,
            "quarantined": 0,
            "http_errors": 0,
            "courses_changed": 0,
            "offerings_changed": 0,
        }
        self.ensure_sources([self.source_code])
        source_id, ingestion_id = self._start_ingestion(run_start)
        saved_page_count = 0
        try:
            active_count_before = self._active_course_count(source_id)
            catalog = self.extractor.fetch_catalog_index()
            if limit is not None:
                catalog = catalog[:limit]
            counts["discovered"] = len(catalog)
            if not catalog:
                raise IngestionFailure(
                    "El catálogo no contiene cursos; posible cambio de estructura DOM.",
                    "ZERO_RESULTS",
                )
            saved_page_count = self._save_raw_pages(source_id, saved_page_count)

            for item in catalog:
                try:
                    dto = self._extract_one(item)
                    with transaction.atomic(), connection.cursor() as cursor:
                        changes = self._persist_course(
                            cursor, source_id, ingestion_id, dto
                        )
                except (ValidationError, ValueError) as exc:
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                except requests.RequestException as exc:
                    counts["http_errors"] += 1
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                    logger.warning("Fallo HTTP en detalle %s: %s", item.get("url"), exc)
                except Exception as exc:
                    counts["quarantined"] += 1
                    self._quarantine(source_id, ingestion_id, item, exc)
                    logger.exception("No se pudo procesar curso %s", item.get("url"))
                else:
                    counts["processed_ok"] += 1
                    counts["courses_changed"] += changes["course_changed"]
                    counts["offerings_changed"] += changes["offerings_changed"]
                finally:
                    saved_page_count = self._save_raw_pages(
                        source_id, saved_page_count
                    )

            minimum_pct = self._minimum_courses_pct()
            minimum_count = (active_count_before * minimum_pct + 99) // 100
            if counts["processed_ok"] == 0:
                raise IngestionFailure(
                    "No se pudo validar ningún curso de la fuente.",
                    "DOM_STRUCTURE_CHANGED",
                )
            if counts["processed_ok"] < minimum_count:
                raise IngestionFailure(
                    f"Se procesaron {counts['processed_ok']} cursos, menos del "
                    f"{minimum_pct}% requerido de los {active_count_before} "
                    "cursos activos al iniciar la corrida.",
                    "DOM_STRUCTURE_CHANGED",
                )

            with transaction.atomic(), connection.cursor() as cursor:
                self._mark_missing(cursor, source_id, ingestion_id, run_start)
                self._finish_ingestion(
                    cursor,
                    ingestion_id,
                    "PARTIAL" if counts["quarantined"] else "OK",
                    counts,
                )
                self.resilience.succeeded(cursor, source_id)
                self._purge_raw_pages(cursor)
            counts["status"] = "PARTIAL" if counts["quarantined"] else "OK"
            return counts
        except Exception as exc:
            error_message = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            failure_type = (
                exc.failure_type if isinstance(exc, IngestionFailure) else "HTTP_ERROR"
            )
            with transaction.atomic(), connection.cursor() as cursor:
                self._finish_ingestion(
                    cursor, ingestion_id, "FAILED", counts, error_message
                )
                self.resilience.failed(
                    cursor, source_id, ingestion_id, failure_type, str(exc)
                )
                self._purge_raw_pages(cursor)
            logger.error("Ingesta %s fallida: %s", self.source_code, exc)
            counts["status"] = "FAILED"
            return counts
        finally:
            close = getattr(self.extractor, "close", None)
            if close:
                close()

    @classmethod
    def ensure_sources(cls, source_codes: list[str]) -> None:
        IngestionRepository().ensure_sources(cls.SOURCE_METADATA, source_codes)

    def _start_ingestion(self, started_at) -> tuple[int, int]:
        return self.ingestion_repository.start(self.source_code, started_at)

    def _extract_one(self, item: dict[str, Any]) -> ScrapedCourseDTO:
        if self.source_code == "CIBERTEC":
            dto, _, _ = self.extractor.fetch_course(item)
            return dto
        html, _, _ = self.extractor.fetch_page(item["url"])
        return pucp_course_to_dto(
            self.extractor.parse_course_detail(
                html, item["url"], item.get("categoria")
            )
        )

    def _save_raw_pages(self, source_id: int, start_index: int) -> int:
        pages = getattr(self.extractor, "raw_pages", [])
        if start_index >= len(pages):
            return start_index
        with transaction.atomic(), connection.cursor() as cursor:
            for url, html_bytes in pages[start_index:]:
                self._insert_raw_page(cursor, source_id, url, html_bytes)
        return len(pages)

    @staticmethod
    def _insert_raw_page(cursor, source_id: int, url: str, html_bytes: bytes) -> None:
        IngestionRepository.insert_raw_page(cursor, source_id, url, html_bytes)

    def _quarantine(self, source_id, ingestion_id, payload, error) -> None:
        self.ingestion_repository.quarantine(
            source_id, ingestion_id, payload, error
        )

    def _minimum_courses_pct(self) -> int:
        with connection.cursor() as cursor:
            return setting_int(read_settings(cursor), "min_courses_threshold_pct", 20)

    @staticmethod
    def _active_course_count(source_id: int) -> int:
        with connection.cursor() as cursor:
            return IngestionRepository.active_course_count(cursor, source_id)

    def _persist_course(self, cursor, source_id, ingestion_id, dto: ScrapedCourseDTO):
        return self.course_repository.persist(cursor, source_id, ingestion_id, dto)

    def _mark_missing(self, cursor, source_id, ingestion_id, run_start):
        self.ingestion_repository.mark_missing(
            cursor, source_id, ingestion_id, run_start
        )

    @staticmethod
    def _purge_raw_pages(cursor):
        IngestionRepository.purge_raw_pages(cursor, read_settings(cursor))

    @staticmethod
    def _finish_ingestion(cursor, ingestion_id, status, counts, error_msg=None):
        IngestionRepository.finish(
            cursor, ingestion_id, status, counts, error_msg
        )

    @staticmethod
    def _hash(value: dict) -> str:
        encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
