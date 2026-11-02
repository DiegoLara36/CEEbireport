# etl/extractors/base.py
from abc import ABC, abstractmethod
from typing import Tuple, List, Dict, Any
import hashlib
import requests
from etl.schemas import CourseExtractedSchema


class BaseExtractor(ABC):
    source_code: str = ""
    source_name: str = ""
    base_url: str = ""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.raw_pages: List[Tuple[str, bytes]] = []
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def fetch_page(self, url: str) -> Tuple[str, bytes, str]:
        """Descarga la página y retorna (html_text, html_bytes, sha256_hex)."""
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        html_bytes = response.content
        sha256_hash = hashlib.sha256(html_bytes).hexdigest()
        self.raw_pages.append((url, html_bytes))
        return response.text, html_bytes, sha256_hash

    @abstractmethod
    def fetch_catalog_index(self) -> List[Dict[str, Any]]:
        """Obtiene la lista de cursos [{categoria, curso, url}, ...] de la paginación."""
        pass

    @abstractmethod
    def parse_course_detail(self, html: str, url: str, fallback_category: str = None) -> CourseExtractedSchema:
        """Extrae los campos de la página de detalle y los valida con Pydantic."""
        pass