import re
from datetime import date, time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.utils import timezone

from etl.schemas import (
    ScheduleItemSchema,
    ScrapedCourseDTO,
    ScrapedDiscountDTO,
    ScrapedOfferingDTO,
    ScrapedPaymentPlanDTO,
    ObservationStatus,
)
from etl.adapters import adapt_cibertec_course
from etl.normalization import normalize_amount, normalize_currency


MONTHS_ES = {
    "ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4, "may": 5, "mayo": 5, "jun": 6, "junio": 6,
    "jul": 7, "julio": 7, "ago": 8, "agosto": 8, "sep": 9, "sept": 9,
    "set": 9, "septiembre": 9, "setiembre": 9, "oct": 10, "octubre": 10,
    "nov": 11, "noviembre": 11, "dic": 12, "diciembre": 12,
}
WEEKDAYS_ES = {
    "lunes": 1, "martes": 2, "miércoles": 3, "miercoles": 3,
    "jueves": 4, "viernes": 5, "sábado": 6, "sabado": 6, "domingo": 7,
}
WEEKDAY_PATTERN = "|".join(sorted(WEEKDAYS_ES, key=len, reverse=True))
TIME_TOKEN = r"\d{1,2}(?::\d{2})?\s*(?:(?:a|p)\.?\s*m\.?)?"
RANGE_PATTERN = re.compile(
    rf"(?P<start>{TIME_TOKEN})\s*(?:a|hasta|[-–])\s*(?P<end>{TIME_TOKEN})",
    re.IGNORECASE,
)


def parse_cibertec_start_date(value: Optional[str], today: Optional[date] = None) -> Optional[date]:
    if not value:
        return None
    match = re.search(r"(\d{1,2})\s+([a-záéíóú]+)\.?(?:\s+(\d{4}))?", value.lower())
    if not match:
        return None
    day, month_name, explicit_year = match.groups()
    month = MONTHS_ES.get(month_name.rstrip("."))
    if month is None:
        return None
    local_today = today or timezone.localdate()
    year = int(explicit_year) if explicit_year else local_today.year + (month < local_today.month)
    return date(year, month, int(day))


def _parse_time(value: str, companion: str = "") -> time:
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s*m\.?)?", value.lower())
    if not match:
        raise ValueError(f"Horario inválido: {value!r}")
    hour, minute, meridian = match.groups()
    if not meridian:
        companion_match = re.search(r"([ap])\.?\s*m\.?", companion.lower())
        meridian = companion_match.group(1) if companion_match else None
    hour = int(hour)
    if meridian == "p" and hour != 12:
        hour += 12
    elif meridian == "a" and hour == 12:
        hour = 0
    return time(hour, int(minute or 0))


def parse_cibertec_schedule(value: Optional[str]) -> list[ScheduleItemSchema]:
    if not value:
        return []
    schedules: list[ScheduleItemSchema] = []
    previous_end = 0
    for match in RANGE_PATTERN.finditer(value):
        prefix = value[previous_end:match.start()].lower()
        days = {
            day_number
            for name, day_number in WEEKDAYS_ES.items()
            if re.search(rf"\b{re.escape(name)}s?\b", prefix)
        }
        if not days:
            previous_end = match.end()
            continue
        start_value, end_value = match.group("start", "end")
        start = _parse_time(start_value, end_value)
        end = _parse_time(end_value, start_value)
        schedules.extend(
            ScheduleItemSchema(day_of_week=day, start_time=start, end_time=end)
            for day in sorted(days)
        )
        previous_end = match.end()
    return schedules


def _money(text: Optional[str]) -> Optional[Decimal]:
    return normalize_amount(text)


def _duration(text: Optional[str]) -> tuple[Optional[Decimal], Optional[Decimal]]:
    if not text:
        return None, None
    hours = re.search(r"([\d.,]+)\s*(?:horas?|hrs?\.?)", text, re.IGNORECASE)
    weeks = re.search(r"([\d.,]+)\s*semanas?", text, re.IGNORECASE)
    return _money(hours.group(1)) if hours else None, _money(weeks.group(1)) if weeks else None


class CibertecExtractor:
    source_code = "CIBERTEC"
    source_name = "Educación Continua Cibertec"
    base_url = "https://educacioncontinua.cibertec.edu.pe/buscador/"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.raw_pages: list[tuple[str, bytes]] = []
        self._playwright = None
        self._browser = None
        self._page = None
        self._playwright_timeout_error = TimeoutError

    def start(self) -> None:
        if self._page:
            return
        try:
            from playwright.sync_api import (
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Cibertec requiere Playwright. Instale las dependencias y ejecute "
                "'playwright install chromium'."
            ) from exc
        self._playwright_timeout_error = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        self._page = context.new_page()
        self._page.set_default_timeout(self.timeout * 1000)

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._page = self._browser = self._playwright = None

    def fetch_catalog_index(self) -> list[dict[str, Any]]:
        self.start()
        response = self._page.goto(
            self.base_url, wait_until="networkidle", timeout=self.timeout * 1000
        )
        if response and response.status >= 400:
            raise requests.HTTPError(f"HTTP {response.status} al cargar el catálogo Cibertec")
        try:
            self._page.wait_for_selector(
                "#catalog-cursos article", timeout=self.timeout * 1000
            )
        except self._playwright_timeout_error:
            self.raw_pages.append((self._page.url, self._page.content().encode("utf-8")))
            return []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            html = self._page.content()
            self.raw_pages.append((self._page.url, html.encode("utf-8")))
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("#catalog-cursos article") or soup.select("article")
            for card in cards:
                title_node = card.select_one("h3")
                more_label = card.find(string=re.compile(r"Saber\s+m[aá]s", re.I))
                anchor = (
                    more_label.find_parent("a")
                    if more_label and more_label.find_parent("a")
                    else title_node.find_parent("a") if title_node else card.select_one("a[href]")
                )
                if not anchor:
                    continue
                href = urljoin(self.base_url, anchor.get("href", "").strip())
                if urlparse(href).netloc != urlparse(self.base_url).netloc or href in seen:
                    continue
                title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
                if not title:
                    continue
                seen.add(href)
                type_node = card.find(
                    "span",
                    class_=lambda classes: classes
                    and "uppercase" in classes
                    and "font-bold" in classes,
                )
                discount_node = card.find("p", string=re.compile(r"Dscto", re.I))
                description_node = card.find(
                    "p",
                    class_=lambda classes: classes and "line-clamp-3" in classes,
                )
                start_node = card.find("p", string=re.compile(r"Inicio", re.I))
                duration_node = card.find(
                    "span", string=re.compile(r"semanas|hrs|horas", re.I)
                )
                current_price_node = card.find(
                    "p",
                    class_=lambda classes: classes
                    and ("text-[24px]" in classes or "leading-[22px]" in classes),
                )
                list_price_node = card.find("span", class_="line-through")
                results.append({
                    "url": href,
                    "curso": title,
                    "categoria": type_node.get_text(" ", strip=True) if type_node else None,
                    "discount": discount_node.get_text(" ", strip=True) if discount_node else None,
                    "description": description_node.get_text(" ", strip=True) if description_node else None,
                    "start_text": start_node.get_text(" ", strip=True) if start_node else None,
                    "duration_text": duration_node.get_text(" ", strip=True) if duration_node else None,
                    "current_price": current_price_node.get_text(" ", strip=True) if current_price_node else None,
                    "list_price": list_price_node.get_text(" ", strip=True) if list_price_node else None,
                })

            next_button = self._page.locator(
                "button[aria-label='Ver cursos posteriores'], "
                "button[aria-label*='Siguiente'], button:has-text('Siguiente'), "
                "a[aria-label*='Siguiente']"
            ).first
            if next_button.count() == 0 or next_button.is_disabled():
                break
            previous_title = (
                self._page.locator("#catalog-cursos article h3").first.inner_text().strip()
            )
            next_button.click()
            try:
                self._page.wait_for_function(
                    """previous => {
                        const title = document.querySelector('#catalog-cursos article h3');
                        return title && title.innerText.trim() !== previous;
                    }""",
                    arg=previous_title,
                    timeout=min(self.timeout * 1000, 10000),
                )
            except self._playwright_timeout_error:
                break
        return results

    def fetch_course(self, item: dict[str, Any]) -> tuple[ScrapedCourseDTO, str, bytes]:
        self.start()
        url = item["url"]
        response = self._page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
        if response and response.status >= 400:
            raise requests.HTTPError(f"HTTP {response.status} al cargar {url}")
        self._page.wait_for_selector("nav[aria-label='Breadcrumb'], h1", timeout=self.timeout * 1000)
        modules: list[dict[str, Any]] = []
        buttons = self._page.locator("button:has-text('Módulo')")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            module_name = button.inner_text().strip()
            button.click()
            self._page.wait_for_timeout(200)
            soup_module = BeautifulSoup(
                self._page.locator("div.border-base-color-secondary-60:has(h3)").first.inner_html(),
                "html.parser",
            )
            header = soup_module.find("h3")
            modules.append({
                "name": header.get_text(" ", strip=True) if header else module_name,
                "items": [li.get_text(" ", strip=True) for li in soup_module.find_all("li")],
            })

        cohorts: list[dict[str, Any]] = []
        date_labels = self._page.locator("div.grid label:has(span)")
        for index in range(date_labels.count()):
            label = date_labels.nth(index)
            start_text = label.locator("span").inner_text().strip()
            label.click()
            self._page.wait_for_timeout(250)
            soup = BeautifulSoup(self._page.content(), "html.parser")
            duration_node = soup.find(string=re.compile(r"\bsemanas?\b|\bhrs?\.?\b", re.IGNORECASE))
            schedule_node = soup.find(string=re.compile(rf"(?:{WEEKDAY_PATTERN}).*{RANGE_PATTERN.pattern}", re.I))
            duration_text = duration_node.parent.get_text(" ", strip=True) if duration_node and duration_node.parent else None
            schedule_text = schedule_node.parent.get_text(" ", strip=True) if schedule_node and schedule_node.parent else None
            cohorts.append({
                "start_text": start_text,
                "duration_text": duration_text,
                "schedule_text": schedule_text,
            })

        html = self._page.content()
        html_bytes = html.encode("utf-8")
        self.raw_pages.append((url, html_bytes))
        data = self.parse_course_detail(
            html, url, item.get("categoria"), modules, cohorts, item
        )
        return data, url, html_bytes

    def parse_course_detail(
        self,
        html: str,
        url: str,
        fallback_category: Optional[str] = None,
        modules: Optional[list[dict[str, Any]]] = None,
        cohorts: Optional[list[dict[str, Any]]] = None,
        listing: Optional[dict[str, Any]] = None,
    ) -> ScrapedCourseDTO:
        listing = listing or {}
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        title = soup.find("h1")
        name = title.get_text(" ", strip=True) if title else ""
        breadcrumb = soup.select("nav[aria-label='Breadcrumb'] a")
        category = breadcrumb[-1].get_text(" ", strip=True) if len(breadcrumb) > 2 else fallback_category
        modality_text = page_text.lower()
        badges = [li.get_text(" ", strip=True) for li in soup.select("ul li")]
        modality_raw = next(
            (
                badge
                for badge in badges
                if re.search(r"virtual|presencial|h[ií]brid[oa]|semipresencial", badge, re.I)
            ),
            None,
        )
        modality = (
            "PRESENCIAL" if "presencial" in modality_text and "virtual" not in modality_text
            else "HIBRIDO" if "híbrido" in modality_text or "hibrido" in modality_text
            else "VIRTUAL_SINC"
        )
        benefits = next(
            (text for text in badges if re.search(r"cuotas|tarjeta|sin intereses", text, re.I)),
            "",
        )
        list_node = soup.select_one("span.line-through")
        list_amount = (
            _money(list_node.get_text(" ", strip=True))
            if list_node
            else _money(listing.get("list_price"))
        )
        current_candidates = [
            _money(node.get_text(" ", strip=True))
            for node in soup.select("p, span, strong")
            if not node.find_parent(class_="line-through")
            and "line-through" not in node.get("class", [])
            and re.search(
                r"(?:S\s*/\.?|[$€£]|\b[A-Z]{3}\b)\s*\d[\d.,]*",
                node.get_text(" ", strip=True),
                re.I,
            )
            and _money(node.get_text(" ", strip=True)) is not None
        ]
        final_amount = (
            current_candidates[-1]
            if current_candidates
            else _money(listing.get("current_price")) or list_amount
        )
        if list_amount is None:
            list_amount = final_amount
        if final_amount is None:
            final_amount = list_amount
        current_price_text = next(
            (
                node.get_text(" ", strip=True)
                for node in reversed(soup.select("p, span, strong"))
                if not node.find_parent(class_="line-through")
                and "line-through" not in node.get("class", [])
                and re.search(
                    r"(?:S\s*/\.?|US\s*\$|[$€£]|\b[A-Z]{3}\b)\s*\d[\d.,]*",
                    node.get_text(" ", strip=True),
                    re.I,
                )
            ),
            listing.get("current_price"),
        )
        list_price_text = (
            list_node.get_text(" ", strip=True)
            if list_node
            else listing.get("list_price")
        )
        currency_raw = current_price_text or list_price_text

        discount_text_node = soup.find(
            string=re.compile(
                r"\d+(?:[.,]\d+)?\s*%\s*(?:de\s*)?(?:dscto|descuento)", re.I
            )
        )
        discount_text = (
            str(discount_text_node).strip()
            if discount_text_node
            else listing.get("discount")
        )
        discounts = []
        if discount_text:
            value_match = re.search(r"\d+(?:[.,]\d+)?", discount_text)
            value = _money(value_match.group()) if value_match else None
            if value is not None:
                discounts.append(ScrapedDiscountDTO(
                    kind="PERCENT", value=value, description=discount_text
                ))

        parsed_cohorts = cohorts or []
        if not parsed_cohorts:
            parsed_cohorts = [{
                "start_text": listing.get("start_text"),
                "duration_text": listing.get("duration_text"),
                "schedule_text": None,
            }]
        offerings = []
        for cohort in parsed_cohorts:
            start_date = parse_cibertec_start_date(cohort.get("start_text"))
            duration_hours, duration_weeks = _duration(
                cohort.get("duration_text") or listing.get("duration_text")
            )
            schedule_text = cohort.get("schedule_text")
            schedules = parse_cibertec_schedule(schedule_text)
            external_code = "PERMANENT"
            if start_date:
                from django.utils.text import slugify
                schedule_slug = slugify(schedule_text or "")
                external_code = (
                    f"{start_date.isoformat()}_{schedule_slug[:30]}"
                    if schedule_slug else start_date.isoformat()
                )
            offerings.append(ScrapedOfferingDTO(
                external_code=external_code,
                modality_code=modality,
                modality_raw=modality_raw,
                modality_observation=(
                    ObservationStatus.OBSERVED
                    if modality_raw
                    else ObservationStatus.UNKNOWN
                ),
                start_date=start_date,
                start_date_observation=(
                    ObservationStatus.OBSERVED
                    if start_date
                    else ObservationStatus.UNKNOWN
                    if cohort.get("start_text")
                    else ObservationStatus.NOT_COLLECTED
                ),
                end_date_observation=ObservationStatus.NOT_COLLECTED,
                duration_hours=duration_hours,
                duration_weeks=duration_weeks,
                duration_observation=(
                    ObservationStatus.OBSERVED
                    if duration_hours is not None or duration_weeks is not None
                    else ObservationStatus.UNKNOWN
                    if cohort.get("duration_text") or listing.get("duration_text")
                    else ObservationStatus.NOT_COLLECTED
                ),
                schedules=schedules,
                schedules_observation=(
                    ObservationStatus.OBSERVED
                    if schedules
                    else ObservationStatus.UNKNOWN
                    if schedule_text
                    else ObservationStatus.NOT_COLLECTED
                ),
                schedule_text=schedule_text,
                currency=normalize_currency(currency_raw),
                currency_raw=currency_raw,
                currency_observation=(
                    ObservationStatus.OBSERVED
                    if normalize_currency(currency_raw)
                    else ObservationStatus.UNKNOWN
                ),
                list_amount=list_amount,
                final_amount=final_amount,
                price_observation=(
                    ObservationStatus.OBSERVED
                    if list_amount is not None or final_amount is not None
                    else ObservationStatus.NOT_FOUND
                ),
                discounts=discounts,
                discounts_observation=ObservationStatus.OBSERVED,
                payment_plans=self._payment_plans(benefits),
                payment_plans_observation=(
                    ObservationStatus.OBSERVED
                    if benefits
                    else ObservationStatus.NOT_FOUND
                ),
                commercial_conditions=([benefits] if benefits else []),
                commercial_conditions_observation=ObservationStatus.OBSERVED,
                price_raw=current_price_text,
                raw_payload={
                    "cohort": cohort,
                    "benefits": benefits,
                    "duration_text": cohort.get("duration_text") or listing.get("duration_text"),
                    "price_text": currency_raw,
                },
            ))

        content_items = []
        for index, module in enumerate(modules or [], start=1):
            module_title = module.get("name") or f"Módulo {index}"
            for item in module.get("items", []):
                content_items.append(f"{module_title} - {item}")
        prerequisites = self._list_after_heading(soup, r"Conocimientos\s+previos")
        hardware = self._list_after_heading(soup, r"Hardware")
        software = self._list_after_heading(soup, r"Software")
        description_nodes = [
            node.get_text(" ", strip=True)
            for node in soup.select("p")
            if len(node.get_text(" ", strip=True)) > 40
        ]
        description = (
            description_nodes[0]
            if description_nodes
            else listing.get("description")
        )
        certificate = "certificado" in page_text.lower()
        participation_certificate = "constancia" in page_text.lower()
        parsed_url = urlparse(url)
        external_id = parsed_url.path.rstrip("/").split("/")[-1]
        course = ScrapedCourseDTO(
            source_code=self.source_code,
            external_id=external_id,
            course_url=url,
            category_name=category,
            name=name,
            description=description,
            has_certificate=certificate,
            has_participation_certificate=participation_certificate,
            certificate_observation=(
                ObservationStatus.OBSERVED
                if certificate
                else ObservationStatus.NOT_FOUND
            ),
            participation_certificate_observation=(
                ObservationStatus.OBSERVED
                if participation_certificate
                else ObservationStatus.NOT_FOUND
            ),
            content_items=content_items,
            content_observation=(
                ObservationStatus.OBSERVED
                if content_items
                else ObservationStatus.NOT_COLLECTED
            ),
            prerequisite_items=prerequisites,
            prerequisites_observation=(
                ObservationStatus.OBSERVED
                if prerequisites
                else ObservationStatus.NOT_COLLECTED
            ),
            hardware_requirements=hardware,
            hardware_observation=(
                ObservationStatus.OBSERVED
                if hardware
                else ObservationStatus.NOT_COLLECTED
            ),
            software_requirements=software,
            software_observation=(
                ObservationStatus.OBSERVED
                if software
                else ObservationStatus.NOT_COLLECTED
            ),
            duration_hours=offerings[0].duration_hours,
            duration_observation=offerings[0].duration_observation,
            offerings=offerings,
            raw_payload={"benefits": benefits, "modules": modules or [], "cohorts": parsed_cohorts},
        )
        return adapt_cibertec_course(course)

    @staticmethod
    def _list_after_heading(soup: BeautifulSoup, heading_pattern: str) -> list[str]:
        heading = soup.find(string=re.compile(heading_pattern, re.IGNORECASE))
        if not heading:
            return []
        parent = heading.find_parent("div")
        listing = parent.find_next("ul") if parent else None
        return [item.get_text(" ", strip=True) for item in listing.find_all("li")] if listing else []

    @staticmethod
    def _payment_plans(text: str) -> list[ScrapedPaymentPlanDTO]:
        match = re.search(r"(\d+)\s+cuotas?.*?(?:sin\s+intereses)?\s*(?:con\s+tarjetas?\s+de\s+)?(.+)?$", text, re.I)
        if not match:
            return []
        count = int(match.group(1))
        card_text = match.group(2) or text
        cards = [
            name for name in ("Diners", "BCP", "BBVA", "Interbank", "Visa", "Mastercard")
            if re.search(rf"\b{re.escape(name)}\b", card_text, re.I)
        ]
        return [ScrapedPaymentPlanDTO(installments=count, cards=cards)] if cards else []
