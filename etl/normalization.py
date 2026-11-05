import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.utils.text import slugify

from etl.schemas import (
    CategorySchema,
    DurationSchema,
    NormalizedModality,
    ObservationStatus,
)


SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def normalize_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = re.fullmatch(
        r"(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+de\s+(\d{4}))?",
        text.lower(),
    )
    if not match or not match.group(3):
        return None
    day, month_name, year = match.groups()
    month = SPANISH_MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def normalize_currency(value: Optional[str]) -> Optional[str]:
    """Return a currency only when the source provides unambiguous evidence."""
    if not value:
        return None
    text = value.strip().upper()
    if re.search(r"\bPEN\b|S\s*/\.?", text):
        return "PEN"
    if re.search(r"\bUSD\b|US\s*\$", text):
        return "USD"
    if "€" in text:
        return "EUR"
    if "£" in text:
        return "GBP"
    code = re.search(r"\b([A-Z]{3})\b(?=\s*[\d.,])", text)
    if code:
        return code.group(1)
    if re.fullmatch(r"[A-Z]{3}", text):
        return text
    return None


def normalize_amount(value: Optional[str]) -> Optional[Decimal]:
    if not value:
        return None
    match = re.search(r"-?[\d][\d.,]*", value.replace(" ", ""))
    if not match:
        return None
    normalized = match.group(0)
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        tail = normalized.rsplit(",", 1)[-1]
        normalized = (
            normalized.replace(",", ".")
            if len(tail) in (1, 2)
            else normalized.replace(",", "")
        )
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def normalize_duration(value: Optional[str]) -> list[DurationSchema]:
    if not value:
        return []
    results = []
    for pattern, unit in (
        (r"([\d.,]+)\s*(?:horas?|hrs?\.?)", "HOUR"),
        (r"([\d.,]+)\s*semanas?", "WEEK"),
        (r"([\d.,]+)\s*(?:sesiones?|clases?)", "SESSION"),
    ):
        for match in re.finditer(pattern, value, re.IGNORECASE):
            amount = normalize_amount(match.group(1))
            if amount is not None:
                results.append(
                    DurationSchema(
                        amount=amount,
                        unit=unit,
                        original_text=match.group(0),
                    )
                )
    return results


def normalize_modality(value: Optional[str]) -> Optional[NormalizedModality]:
    if not value:
        return None
    text = value.casefold()
    if any(term in text for term in ("híbr", "hibr", "semipresencial", "mixta")):
        return NormalizedModality.HYBRID
    if any(term in text for term in ("virtual", "online", "remoto", "a distancia")):
        return NormalizedModality.VIRTUAL
    if "presencial" in text:
        return NormalizedModality.PRESENTIAL
    return None


def normalize_category(value: Optional[str]) -> Optional[CategorySchema]:
    if not value or not value.strip():
        return None
    label = value.strip()
    return CategorySchema(
        source_label=label,
        normalized_label=slugify(label).replace("-", "_") or None,
        observation=ObservationStatus.OBSERVED,
    )
