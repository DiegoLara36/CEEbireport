# etl/extractors/pucp.py
import re
import json
import time as time_module
import hashlib
from datetime import date, time
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import ValidationError
from django.db import connection, transaction
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
    # 1. EXTRACCIÓN DEL LISTADO PAGINADO (Tu código integrado)
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


# =============================================================================
# 4. SERVICIO ORQUESTADOR Y LOADER POSTGRESQL (Respetando tu DDL exacto)
# =============================================================================
class PucpETLService:
    """
    Ejecuta la extracción, guarda páginas crudas, valida con Pydantic,
    inserta en las tablas del modelo relacional y registra auditoría/alertas.
    """

    def __init__(self):
        self.extractor = PucpExtractor()
        self.omitted_fields_report: List[Dict[str, Any]] = []

    def _get_or_create_source(self, cur) -> int:
        cur.execute(
            """
            INSERT INTO source (code, name, base_url, active)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id;
            """,
            (self.extractor.source_code, self.extractor.source_name, self.extractor.base_url),
        )
        return cur.fetchone()[0]

    def _start_ingestion_log(self, cur, source_id: int) -> int:
        cur.execute(
            """
            INSERT INTO ingestion_log (source_id, status, started_at, counts)
            VALUES (%s, 'PARTIAL', now(), '{}'::jsonb)
            RETURNING id;
            """,
            (source_id,),
        )
        return cur.fetchone()[0]

    def _finish_ingestion_log(self, cur, ingestion_id: int, status: str, counts: dict, error_msg: str = None):
        cur.execute(
            """
            UPDATE ingestion_log
               SET status = %s,
                   finished_at = now(),
                   counts = %s::jsonb,
                   error_msg = %s
             WHERE id = %s;
            """,
            (status, json.dumps(counts), error_msg, ingestion_id),
        )

    def _log_alert(self, cur, ingestion_id: int, severity: str, alert_type: str, message: str, entity_ref: str = None):
        cur.execute(
            """
            INSERT INTO alert (ingestion_id, severity, type, message, entity_ref, created_at)
            VALUES (%s, %s, %s, %s, %s, now());
            """,
            (ingestion_id, severity, alert_type, message, entity_ref),
        )

    def _log_quarantine(self, cur, source_id: int, ingestion_id: int, raw_payload: dict, errors: list):
        cur.execute(
            """
            INSERT INTO quarantine_record (source_id, ingestion_id, raw_payload, errors, reviewed, created_at)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, FALSE, now());
            """,
            (source_id, ingestion_id, json.dumps(raw_payload, default=str), json.dumps(errors, default=str)),
        )

    def _save_raw_page(self, cur, source_id: int, url: str, sha256_hex: str, html_bytes: bytes):
        cur.execute(
            """
            INSERT INTO raw_page (source_id, url, html_sha256, html, fetched_at)
            VALUES (%s, %s, %s, %s, now());
            """,
            (source_id, url, sha256_hex, html_bytes),
        )

    def _persist_course_data(self, cur, source_id: int, ingestion_id: int, data: CourseExtractedSchema):
        # 1. Categoría
        category_id = None
        if data.category_name:
            cur.execute(
                """
                INSERT INTO category (source_id, name)
                VALUES (%s, %s)
                ON CONFLICT (source_id, name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id;
                """,
                (source_id, data.category_name),
            )
            category_id = cur.fetchone()[0]

        # 2. Hash canónico del curso para detectar cambios
        course_hash = hashlib.sha256(
            f"{data.name}|{data.description}|{data.duration_hours}|{data.has_certificate}".encode("utf-8")
        ).hexdigest()

        # 3. Upsert en `course`
        cur.execute(
            """
            INSERT INTO course (
                source_id, external_id, name, category_id, description,
                course_url, duration_hours, has_certificate, has_participation_certificate,
                state, canonical_hash, first_seen_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, now(), now())
            ON CONFLICT (source_id, external_id) DO UPDATE SET
                name = EXCLUDED.name,
                category_id = COALESCE(EXCLUDED.category_id, course.category_id),
                description = EXCLUDED.description,
                course_url = EXCLUDED.course_url,
                duration_hours = EXCLUDED.duration_hours,
                has_certificate = EXCLUDED.has_certificate,
                has_participation_certificate = EXCLUDED.has_participation_certificate,
                canonical_hash = EXCLUDED.canonical_hash,
                last_seen_at = now()
            RETURNING id, (xmax = 0) AS inserted;
            """,
            (
                source_id, data.external_id, data.name, category_id, data.description,
                str(data.course_url), data.duration_hours, data.has_certificate,
                data.has_participation_certificate, course_hash
            ),
        )
        course_id, was_inserted = cur.fetchone()

        # Auditoría en `change_log`
        cur.execute(
            """
            INSERT INTO change_log (entity_type, entity_id, action, diff, ingestion_id)
            VALUES ('course', %s, %s, %s::jsonb, %s);
            """,
            (
                course_id,
                "CREATED" if was_inserted else "UPDATED",
                json.dumps({"name": data.name, "canonical_hash": course_hash}),
                ingestion_id,
            ),
        )

        # 4. Contacto (`course_contact` - Elemento 18)
        if data.contact:
            cur.execute(
                """
                INSERT INTO course_contact (course_id, name, phone, email)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (course_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    phone = COALESCE(EXCLUDED.phone, course_contact.phone),
                    email = COALESCE(EXCLUDED.email, course_contact.email);
                """,
                (course_id, data.contact.name, data.contact.phone, data.contact.email),
            )

        # 5. Temario itemizado (`course_content_item` - Elemento 10)
        if data.content_items:
            cur.execute("DELETE FROM course_content_item WHERE course_id = %s;", (course_id,))
            for idx, item_text in enumerate(data.content_items, start=1):
                cur.execute(
                    """
                    INSERT INTO course_content_item (course_id, position, text)
                    VALUES (%s, %s, %s);
                    """,
                    (course_id, idx, item_text),
                )

        # 6. Modalidad (`modality`)
        cur.execute("SELECT id FROM modality WHERE code = %s;", (data.modality_code,))
        mod_row = cur.fetchone()
        modality_id = mod_row[0] if mod_row else None

        # 7. Oferta / Cohorte (`course_offering` - Elementos 3, 4, 5)
        cur.execute(
            """
            INSERT INTO course_offering (
                course_id, external_code, modality_id, start_date, end_date,
                duration_hours, state, first_seen_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', now(), now())
            ON CONFLICT (course_id, external_code) WHERE external_code IS NOT NULL
            DO UPDATE SET
                modality_id = EXCLUDED.modality_id,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                duration_hours = EXCLUDED.duration_hours,
                last_seen_at = now()
            RETURNING id;
            """,
            (
                course_id, data.external_offering_code, modality_id,
                data.start_date, data.end_date, data.duration_hours
            ),
        )
        offering_id = cur.fetchone()[0]

        # 8. Horarios (`offering_schedule` - Elemento 13)
        if data.schedules:
            cur.execute("DELETE FROM offering_schedule WHERE offering_id = %s;", (offering_id,))
            for sched in data.schedules:
                cur.execute(
                    """
                    INSERT INTO offering_schedule (offering_id, day_of_week, start_time, end_time)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (offering_id, sched.day_of_week, sched.start_time, sched.end_time),
                )

        # 9. Docente (`professor`, `professor_source_link`, `offering_professor` - Elementos 14 y 15)
        if data.professor:
            cur.execute(
                "SELECT id FROM professor WHERE lower(full_name) = lower(%s) LIMIT 1;",
                (data.professor.full_name,),
            )
            prof_row = cur.fetchone()
            if prof_row:
                professor_id = prof_row[0]
                cur.execute(
                    "UPDATE professor SET specialty = COALESCE(%s, specialty) WHERE id = %s;",
                    (data.professor.specialty, professor_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO professor (full_name, specialty)
                    VALUES (%s, %s)
                    RETURNING id;
                    """,
                    (data.professor.full_name, data.professor.specialty),
                )
                professor_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO professor_source_link (professor_id, source_id)
                VALUES (%s, %s)
                ON CONFLICT (professor_id, source_id) DO NOTHING;
                """,
                (professor_id, source_id),
            )
            cur.execute(
                """
                INSERT INTO offering_professor (offering_id, professor_id, role)
                VALUES (%s, %s, 'TITULAR')
                ON CONFLICT (offering_id, professor_id, role) DO NOTHING;
                """,
                (offering_id, professor_id),
            )

        # 10. Certificados (`offering_certificate` - Elemento 16)
        if data.has_certificate:
            cur.execute("SELECT id FROM certificate_type WHERE code = 'CERTIFICADO';")
            cert_type = cur.fetchone()
            if cert_type:
                cur.execute(
                    """
                    INSERT INTO offering_certificate (offering_id, certificate_type_id, price, currency, requires_approval, optional)
                    VALUES (%s, %s, 0, %s, TRUE, FALSE)
                    ON CONFLICT (offering_id, certificate_type_id) DO NOTHING;
                    """,
                    (offering_id, cert_type[0], data.currency),
                )

        if data.has_participation_certificate:
            cur.execute("SELECT id FROM certificate_type WHERE code = 'CONSTANCIA_PARTICIPACION';")
            const_type = cur.fetchone()
            if const_type:
                cur.execute(
                    """
                    INSERT INTO offering_certificate (offering_id, certificate_type_id, price, currency, requires_approval, optional)
                    VALUES (%s, %s, 0, %s, FALSE, FALSE)
                    ON CONFLICT (offering_id, certificate_type_id) DO NOTHING;
                    """,
                    (offering_id, const_type[0], data.currency),
                )

        # 11. Precio (`price_snapshot` - Elemento 6)
        # Nota: Si el precio cambia, el trigger `tg_price_snapshot_close_prev` cierra el anterior automáticamente.
        cur.execute(
            """
            SELECT list_amount, currency
              FROM price_snapshot
             WHERE offering_id = %s AND is_current = TRUE
             LIMIT 1;
            """,
            (offering_id,),
        )
        current_price = cur.fetchone()
        raw_payload_extra = {
            "start_date_formatted": data.start_date_str,
            "end_date_formatted": data.end_date_str,
            "brochure_pdf_url": data.brochure_url_raw,
            "target_audience_el9": data.target_audience_raw,
        }

        if not current_price:
            cur.execute(
                """
                INSERT INTO price_snapshot (
                    offering_id, currency, list_amount, final_amount,
                    tax_included, valid_from, snapshot_reason, raw_payload
                )
                VALUES (%s, %s, %s, %s, TRUE, now(), 'INITIAL', %s::jsonb);
                """,
                (offering_id, data.currency, data.price_amount, data.price_amount, json.dumps(raw_payload_extra)),
            )
        elif current_price[0] != data.price_amount or current_price[1] != data.currency:
            cur.execute(
                """
                INSERT INTO price_snapshot (
                    offering_id, currency, list_amount, final_amount,
                    tax_included, valid_from, snapshot_reason, raw_payload
                )
                VALUES (%s, %s, %s, %s, TRUE, now(), 'CHANGE_DETECTED', %s::jsonb);
                """,
                (offering_id, data.currency, data.price_amount, data.price_amount, json.dumps(raw_payload_extra)),
            )

        # Registrar en el reporte en memoria qué datos no tienen columna propia en las tablas maestras
        self.omitted_fields_report.append({
            "curso": data.name,
            "fechas_formateadas": f"{data.start_date_str} -> {data.end_date_str}",
            "elemento_7_organiza": "Omitido por solicitud (Infopucp)",
            "elemento_9_publico_objetivo": (
                "Sin columna dedicada en tabla `course` -> resguardado en `price_snapshot.raw_payload`"
                if data.target_audience_raw else "Vacío"
            ),
            "elemento_11_brochure_pdf": (
                f"Sin columna `brochure_url` en tabla `course` -> resguardado en `price_snapshot.raw_payload` ({data.brochure_url_raw})"
                if data.brochure_url_raw else "Vacío"
            ),
            "elemento_17_descuentos_y_cuotas": "Omitido de momento por solicitud",
        })

    # =========================================================================
    # 5. EJECUCIÓN PRINCIPAL DEL PIPELINE
    # =========================================================================
    def run(self, max_courses: Optional[int] = None) -> Dict[str, Any]:
        counts = {"discovered": 0, "processed_ok": 0, "quarantined": 0, "http_errors": 0}

        with connection.cursor() as cur:
            source_id = self._get_or_create_source(cur)
            ingestion_id = self._start_ingestion_log(cur, source_id)

        try:
            catalog_items = self.extractor.fetch_catalog_index()
            if max_courses:
                catalog_items = catalog_items[:max_courses]
            counts["discovered"] = len(catalog_items)
        except Exception as e:
            with connection.cursor() as cur:
                self._log_alert(cur, ingestion_id, "CRITICAL", "HTTP_ERROR", f"Fallo al listar catálogo PUCP: {e}")
                self._finish_ingestion_log(cur, ingestion_id, "FAILED", counts, str(e))
            raise

        for idx, item in enumerate(catalog_items, start=1):
            course_url = item["url"]
            fallback_cat = item.get("categoria")
            print(f"[{idx}/{counts['discovered']}] Extrayendo detalle: {course_url}")

            try:
                html_text, html_bytes, sha256_hex = self.extractor.fetch_page(course_url)
                with connection.cursor() as cur:
                    self._save_raw_page(cur, source_id, course_url, sha256_hex, html_bytes)

                extracted_data = self.extractor.parse_course_detail(
                    html=html_text, url=course_url, fallback_category=fallback_cat
                )

                with transaction.atomic(), connection.cursor() as cur:
                    self._persist_course_data(cur, source_id, ingestion_id, extracted_data)

                counts["processed_ok"] += 1

            except requests.RequestException as http_err:
                counts["http_errors"] += 1
                with connection.cursor() as cur:
                    self._log_alert(
                        cur, ingestion_id, "WARN", "HTTP_ERROR",
                        f"Error HTTP en curso: {http_err}", entity_ref=course_url
                    )

            except (ValidationError, ValueError) as val_err:
                counts["quarantined"] += 1
                err_detail = val_err.errors() if isinstance(val_err, ValidationError) else [{"msg": str(val_err)}]
                with connection.cursor() as cur:
                    self._log_quarantine(
                        cur, source_id, ingestion_id,
                        raw_payload={"url": course_url, "curso_listado": item.get("curso")},
                        errors=err_detail,
                    )
                    self._log_alert(
                        cur, ingestion_id, "WARN", "VALIDATION",
                        f"Curso enviado a cuarentena por validación: {course_url}", entity_ref=course_url
                    )

            except Exception as unexpected_err:
                counts["quarantined"] += 1
                with connection.cursor() as cur:
                    self._log_alert(
                        cur, ingestion_id, "CRITICAL", "VALIDATION",
                        f"Error inesperado procesando {course_url}: {unexpected_err}", entity_ref=course_url
                    )

            time_module.sleep(0.8)

        # Estado final de la corrida
        final_status = "OK"
        if counts["processed_ok"] == 0 and counts["discovered"] > 0:
            final_status = "FAILED"
        elif counts["quarantined"] > 0 or counts["http_errors"] > 0:
            final_status = "PARTIAL"

        with connection.cursor() as cur:
            self._finish_ingestion_log(cur, ingestion_id, final_status, counts)

        self._print_omitted_report(counts, final_status)
        return counts

    def _print_omitted_report(self, counts: dict, status: str):
        print("\n" + "=" * 75)
        print(f"RESUMEN DE INGESTA PUCP | ESTADO: {status}")
        print(f"Descubiertos: {counts['discovered']} | OK: {counts['processed_ok']} | "
              f"Cuarentena: {counts['quarantined']} | Errores HTTP: {counts['http_errors']}")
        print("=" * 75)
        print("REPORTE DE DATOS OMITIDOS O SIN COLUMNA DEDICADA EN EL DDL DE POSTGRESQL:")
        print("- Elemento 0 (Título duplicado): Omitido. Se usa Elemento 2 sin el prefijo 'Curso de '.")
        print("- Elemento 7 (Organiza: Infopucp): Omitido explícitamente.")
        print("- Elemento 9 (Público objetivo / Dirigido a): La tabla `course` solo tiene `description` "
              "(usada para Elemento 8) y `course_prerequisite_item`. Para no contaminar ni perder el texto, "
              "se omitió de las columnas relacionales y se guardó dentro de `price_snapshot.raw_payload`.")
        print("- Elemento 11 (Link del PDF del Brochure): La tabla `course` tiene `course_url` e `image_url`, "
              "pero NO tiene una columna `brochure_url` o `syllabus_url`. El código numérico ('27320') se usó "
              "como `course_offering.external_code` y el link del PDF se resguardó en `price_snapshot.raw_payload`.")
        print("- Elemento 12 (Vacío): Omitido.")
        print("- Elemento 17 (Tarifas por segmento, descuentos y cuotas con tarjetas): Omitido de momento "
              "según lo indicado (las tablas `offering_price_tier`, `discount` y `payment_plan` quedan listas "
              "para la siguiente fase).")
        print("=" * 75 + "\n")