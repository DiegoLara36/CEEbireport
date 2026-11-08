# etl/extractors/pucp.py
import re
import time as time_module
import hashlib
from datetime import date, time
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.utils import timezone as django_timezone
from django.utils.text import slugify

from etl.extractors.base import BaseExtractor
from etl.schemas import (
    CourseExtractedSchema,
    ObservationStatus,
    ScheduleItemSchema,
    ProfessorSchema,
    ContactSchema,
)
from etl.normalization import normalize_amount, normalize_currency


MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}

DIAS_SEMANA_ES = {
    "lunes": 1, "martes": 2, "miercoles": 3, "miércoles": 3,
    "jueves": 4, "viernes": 5, "sabado": 6, "sábado": 6, "domingo": 7
}


class PucpExtractor(BaseExtractor):
    source_code = "PUCP"
    source_name = "Educación Continua PUCP"
    base_url = "https://educacioncontinua.pucp.edu.pe/tipo-de-actividad/cursos/"

    # =========================================================================
    # 1. EXTRACCIÓN DEL LISTADO PAGINADO
    # =========================================================================
    def fetch_catalog_index(self, sleep_seconds: float = 1.0) -> List[Dict[str, Any]]:
        self.catalog_complete = False
        html_text, _, _ = self.fetch_page(self.base_url)
        soup = BeautifulSoup(html_text, "html.parser")

        grid = soup.select_one(".jet-listing-grid__items[data-pages]")
        total_paginas = int(grid.get("data-pages", 1)) if grid else 1
        print(f"[PUCP] Total de páginas detectadas: {total_paginas}")

        resultados = []
        urls_vistas = set()
        catalog_complete = True

        for pagina in range(1, total_paginas + 1):
            if pagina == 1:
                url_pagina = self.base_url
                pagina_soup = soup
            else:
                url_pagina = f"{self.base_url}jsf/jet-engine/pagenum/{pagina}/"
                try:
                    page_html, _, _ = self.fetch_page(url_pagina)
                    pagina_soup = BeautifulSoup(page_html, "html.parser")
                except requests.RequestException as e:
                    print(f"[PUCP] Error en la página {pagina}: {e}")
                    catalog_complete = False
                    continue

            print(f"[PUCP] Procesando página {pagina}/{total_paginas}")
            tarjetas = pagina_soup.select(".jet-listing-grid__item")
            cursos_pagina = 0

            for tarjeta in tarjetas:
                overlay = tarjeta.select_one(".jet-engine-listing-overlay-wrap[data-url]")
                if not overlay:
                    continue

                raw_url = overlay.get("data-url", "").strip()
                if "/cursos/" not in raw_url or "/tipo-de-actividad/" in raw_url:
                    continue

                full_url = urljoin(url_pagina, raw_url)
                if full_url in urls_vistas:
                    continue

                campo_categoria = overlay.select_one(
                    ".elementor-element-0cb3372 .jet-listing-dynamic-field__content"
                )
                categoria = campo_categoria.get_text(" ", strip=True) if campo_categoria else None

                campo_curso = overlay.select_one(".titul .jet-listing-dynamic-field__content")
                curso = campo_curso.get_text(" ", strip=True) if campo_curso else None

                if curso and full_url:
                    urls_vistas.add(full_url)
                    resultados.append({
                        "categoria": categoria,
                        "curso": curso,
                        "url": full_url
                    })
                    cursos_pagina += 1

            print(f"[PUCP] Cursos nuevos encontrados en pág {pagina}: {cursos_pagina}")
            if pagina < total_paginas and sleep_seconds > 0:
                time_module.sleep(sleep_seconds)

        self.catalog_complete = catalog_complete
        return resultados

    # =========================================================================
    # 2. HELPERS DE TRANSFORMACIÓN (Fechas, Temario, Horarios, Precios)
    # =========================================================================
    @staticmethod
    def _clean_course_name(raw_name: str) -> str:
        """Quita el prefijo 'Curso de ' (case-insensitive) del Elemento 2."""
        return re.sub(r"^curso\s+de\s+", "", raw_name.strip(), flags=re.IGNORECASE).strip()

    @staticmethod
    def _parse_dates(
        el3_start: str,
        el4_end: str,
        reference_date: Optional[date] = None
    ) -> Tuple[Optional[date], Optional[date], Optional[str], Optional[str]]:
        """
        Reglas de negocio para Elemento 3 y Elemento 4:
        - Si ambos meses son menores al mes actual de referencia (ej. estamos en dic y
          el curso empieza en ene y termina en ene), ambos pasan al siguiente año.
        - Si el mes final es menor que el mes inicial (ej. empieza en dic y termina en ene),
          la fecha final pasa al siguiente año respecto al año de inicio.
        - Retorna objetos `date` (para PostgreSQL) y strings `DD/MM/YYYY`.
        """
        ref = reference_date or django_timezone.localdate()

        start_match = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de\s+(\d{4}))?", el3_start.lower())
        end_match = re.search(r"(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de\s+(\d{4}))?", el4_end.lower())

        if not start_match or not end_match:
            return None, None, None, None

        d_ini, m_ini_str, explicit_start_year = start_match.groups()
        d_fin, m_fin_str, explicit_end_year = end_match.groups()

        m_ini = MESES_ES.get(m_ini_str)
        m_fin = MESES_ES.get(m_fin_str)
        if not m_ini or not m_fin:
            return None, None, None, None

        # Explicit source years take precedence; infer only missing years.
        y_ini = int(explicit_start_year) if explicit_start_year else ref.year
        y_fin = int(explicit_end_year) if explicit_end_year else y_ini
        if not explicit_start_year and not explicit_end_year and m_ini < ref.month and m_fin < ref.month:
            y_ini += 1
            y_fin = y_ini
        elif not explicit_start_year and m_ini < ref.month:
            y_ini += 1
            if not explicit_end_year:
                y_fin = y_ini

        # Regla 2: Si el mes final es menor al mes de inicio (cruce dic -> ene), el fin es el año siguiente.
        if m_fin < m_ini and not explicit_end_year:
            y_fin = y_ini + 1

        start_dt = date(y_ini, m_ini, int(d_ini))
        end_dt = date(y_fin, m_fin, int(d_fin))

        return (
            start_dt,
            end_dt,
            start_dt.strftime("%d/%m/%Y"),
            end_dt.strftime("%d/%m/%Y"),
        )

    @staticmethod
    def _parse_duration(el5: str) -> Optional[Decimal]:
        """Extrae solo el número de 'Duración: 30 horas' -> 30."""
        match = re.search(r"(\d+(?:\.\d+)?)", el5)
        return Decimal(match.group(1)) if match else None

    @staticmethod
    def _parse_price(el6: str) -> Tuple[Optional[str], Decimal]:
        """Parse amount and only return a currency when the source identifies it."""
        currency = normalize_currency(el6)
        amount = normalize_amount(el6)
        if amount is None:
            raise ValueError(f"No se pudo extraer monto numérico de precio: '{el6}'")
        return currency, amount

    @staticmethod
    def _itemize_syllabus(el10_elem) -> List[str]:
        """
        Convierte el Elemento 10 en una lista de ítems para `course_content_item`.
        Intenta primero separar por etiquetas HTML (<li>, <p>, <br>) y si viene plano,
        separa por saltos o bloques de mayúsculas.
        """
        if not el10_elem:
            return []

        li_items = [li.get_text(" ", strip=True) for li in el10_elem.find_all("li")]
        if li_items:
            return [i for i in li_items if i]

        # Si está separado por <br> o <p>, usar un separador explícito
        raw_lines = [
            line.strip()
            for line in el10_elem.get_text("\n", strip=True).splitlines()
            if line.strip()
        ]
        if len(raw_lines) > 1:
            return raw_lines

        # Fallback si todo el texto vino colapsado en una sola línea:
        single_text = raw_lines[0] if raw_lines else ""
        if not single_text:
            return []

        # Divide cuando hay un cierre de paréntesis o palabra en minúscula seguida de Mayúscula inicial de frase
        parts = re.split(r"(?<=[a-z0-9\)])\s+(?=[A-ZÁÉÍÓÚ][a-záéíóú])", single_text)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _parse_brochure(el11: str) -> Tuple[Optional[str], Optional[str]]:
        """De '27320 https://...pdf' extrae el código numérico (si existe) y el link del PDF."""
        code_match = re.match(r"^(\d+)\s+", el11.strip())
        url_match = re.search(r"(https?://\S+\.pdf)", el11, flags=re.IGNORECASE)
        return (
            code_match.group(1) if code_match else None,
            url_match.group(1) if url_match else None
        )

    @staticmethod
    def _parse_schedule(el13: str) -> List[ScheduleItemSchema]:
        """
        Convierte 'Martes y jueves de 7:00 p.m. a 10:00 p.m.' en registros para `offering_schedule`.
        """
        text = el13.lower()
        days_found = []
        for day_name, day_num in DIAS_SEMANA_ES.items():
            if re.search(rf"\b{day_name}\b", text):
                if day_num not in days_found:
                    days_found.append(day_num)

        time_pattern = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)"
        times = re.findall(time_pattern, text)

        if len(times) < 2 or not days_found:
            return []

        def to_time(h_str: str, m_str: str, meridian: str) -> time:
            h = int(h_str)
            m = int(m_str) if m_str else 0
            is_pm = "p" in meridian
            if is_pm and h != 12:
                h += 12
            elif not is_pm and h == 12:
                h = 0
            return time(h, m)

        start_t = to_time(*times[0])
        end_t = to_time(*times[1])

        return [
            ScheduleItemSchema(day_of_week=d, start_time=start_t, end_time=end_t)
            for d in sorted(days_found)
        ]

    # =========================================================================
    # 3. PARSEO DE LA PÁGINA DE DETALLE DEL CURSO
    # =========================================================================
    def parse_course_detail(
        self,
        html: str,
        url: str,
        fallback_category: Optional[str] = None
    ) -> CourseExtractedSchema:
        soup = BeautifulSoup(html, "html.parser")
        raw_nodes = soup.select(".jet-listing-dynamic-field__content")
        elements = [el.get_text(" ", strip=True) for el in raw_nodes]

        def get_el(idx: int) -> str:
            return elements[idx].strip() if idx < len(elements) and elements[idx] else ""

        # Slug de la URL como external_id del curso
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        slug = path_parts[-1] if path_parts else hashlib.md5(url.encode()).hexdigest()[:12]

        # Elemento 2: Nombre limpio sin "Curso de "
        raw_title = get_el(2) or get_el(0) or (soup.title.get_text(strip=True) if soup.title else slug)
        clean_name = self._clean_course_name(raw_title)

        # Elemento 1: Categoría
        category_name = get_el(1) or fallback_category

        # Elementos 3 y 4: Fechas con reglas de año
        start_dt, end_dt, start_str, end_str = self._parse_dates(get_el(3), get_el(4))

        # Elemento 5: Duración
        duration_hours = self._parse_duration(get_el(5))

        # Elemento 6: Inversión (moneda y monto)
        currency, price_amount = self._parse_price(get_el(6))

        # Elemento 8: Descripción (¿Por qué estudiar este curso?)
        description = get_el(8) or None

        # Elemento 9: Público objetivo (se captura para informar/guardar en JSONB)
        target_audience = get_el(9) or None

        # Elemento 10: Temario itemizado
        content_items = self._itemize_syllabus(raw_nodes[10]) if len(raw_nodes) > 10 else []

        # Elemento 11: Brochure PDF + posible ID externo de cohorte
        brochure_code, brochure_url = self._parse_brochure(get_el(11))

        # Modalidad inferida del link del brochure o texto
        modality_code = "VIRTUAL_SINC"
        if brochure_url and "asincrono" in brochure_url.lower():
            modality_code = "VIRTUAL_ASINC"
        elif brochure_url and "presencial" in brochure_url.lower():
            modality_code = "PRESENCIAL"
        modality_raw = next(
            (
                term
                for term in ("híbrido", "hibrido", "presencial", "virtual", "asincrónico", "asincronico")
                if term in " ".join(elements).lower()
            ),
            None,
        )
        if modality_raw is None and brochure_url:
            modality_raw = (
                "presencial"
                if "presencial" in brochure_url.lower()
                else "virtual asincrónico"
                if "asincrono" in brochure_url.lower()
                else None
            )

        # Elemento 13: Horario
        schedules = self._parse_schedule(get_el(13))

        # Elementos 14 y 15: Profesor y especialidad
        prof_name = get_el(14)
        prof_spec = get_el(15) or None
        professor = ProfessorSchema(full_name=prof_name, specialty=prof_spec) if prof_name else None

        # Elemento 16: Certificado y Constancia
        cert_text = get_el(16).lower()
        has_cert = "certificado" in cert_text or "certificación" in cert_text
        has_part_cert = "constancia" in cert_text
        price_details = get_el(17) or None

        # Elemento 18: Contacto
        contact_name = get_el(18)
        contact = ContactSchema(name=contact_name) if contact_name else None

        # Código único de la cohorte (`course_offering.external_code`)
        offering_code = brochure_code or (
            f"{start_dt.isoformat()}_{slugify(get_el(13))[:30]}"
            if start_dt and get_el(13)
            else start_dt.isoformat() if start_dt
            else "PERMANENT"
        )

        return CourseExtractedSchema(
            source_code=self.source_code,
            external_id=slug,
            course_url=url,
            category_name=category_name,
            modality_code=modality_code,
            modality_raw=modality_raw,
            name=clean_name,
            description=description,
            duration_hours=duration_hours,
            has_certificate=has_cert,
            has_participation_certificate=has_part_cert,
            content_items=content_items,
            content_observation=(
                ObservationStatus.OBSERVED if content_items else ObservationStatus.NOT_FOUND
            ),
            contact=contact,
            contact_observation=(
                ObservationStatus.OBSERVED if contact else ObservationStatus.NOT_FOUND
            ),
            professor=professor,
            professor_observation=(
                ObservationStatus.OBSERVED if professor else ObservationStatus.NOT_FOUND
            ),
            certificate_observation=(
                ObservationStatus.OBSERVED if get_el(16) else ObservationStatus.NOT_COLLECTED
            ),
            participation_certificate_observation=(
                ObservationStatus.OBSERVED if get_el(16) else ObservationStatus.NOT_COLLECTED
            ),
            external_offering_code=offering_code,
            start_date=start_dt,
            end_date=end_dt,
            schedules=schedules,
            currency=currency,
            currency_raw=get_el(6),
            price_observation=(
                ObservationStatus.OBSERVED if get_el(6) else ObservationStatus.NOT_FOUND
            ),
            price_amount=price_amount,
            price_raw=get_el(6),
            duration_original=get_el(5) or None,
            target_audience_raw=target_audience,
            brochure_url_raw=brochure_url,
            start_date_str=start_str,
            end_date_str=end_str,
            price_details_raw=price_details,
        )
