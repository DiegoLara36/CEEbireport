# etl/schemas.py
import re
from typing import List, Optional, Literal
from datetime import date, time
from decimal import Decimal
from urllib.parse import urlparse

from django.utils.text import slugify
from pydantic import BaseModel, Field, HttpUrl, model_validator


class ScheduleItemSchema(BaseModel):
    day_of_week: int = Field(..., ge=1, le=7, description="1=Lunes ... 7=Domingo")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def check_times(self) -> "ScheduleItemSchema":
        if self.end_time <= self.start_time:
            raise ValueError(f"end_time ({self.end_time}) debe ser mayor que start_time ({self.start_time})")
        return self


class ProfessorSchema(BaseModel):
    full_name: str = Field(..., min_length=2)
    specialty: Optional[str] = None


class ContactSchema(BaseModel):
    name: str = Field(..., min_length=2)
    phone: Optional[str] = None
    email: Optional[str] = None


class CourseExtractedSchema(BaseModel):
    # Identificadores y Maestros
    source_code: str = "PUCP"
    external_id: str = Field(..., min_length=1)
    course_url: HttpUrl
    category_name: Optional[str] = None
    modality_code: Literal["VIRTUAL_SINC", "VIRTUAL_ASINC", "PRESENCIAL", "HIBRIDO"] = "VIRTUAL_SINC"

    # Datos de la tabla `course`
    name: str = Field(..., min_length=3)
    description: Optional[str] = None
    duration_hours: Optional[Decimal] = Field(None, ge=0)
    has_certificate: bool = False
    has_participation_certificate: bool = False

    # Tablas hijas de `course`
    content_items: List[str] = Field(default_factory=list)
    contact: Optional[ContactSchema] = None

    # Datos de `course_offering` y relaciones
    external_offering_code: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    schedules: List[ScheduleItemSchema] = Field(default_factory=list)
    professor: Optional[ProfessorSchema] = None

    # Datos de `price_snapshot`
    currency: Literal["PEN", "USD"] = "PEN"
    price_amount: Decimal = Field(..., ge=0)

    # Datos extraídos que se guardan en el JSONB `raw_payload` (o se reportan como omitidos)
    target_audience_raw: Optional[str] = None
    brochure_url_raw: Optional[str] = None
    start_date_str: Optional[str] = None  # Formato DD/MM/YYYY solicitado
    end_date_str: Optional[str] = None    # Formato DD/MM/YYYY solicitado
    price_details_raw: Optional[str] = None

    @model_validator(mode="after")
    def check_offering_dates(self) -> "CourseExtractedSchema":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date}) no puede ser anterior a start_date ({self.start_date})"
            )
        return self


class ScrapedDiscountDTO(BaseModel):
    kind: Literal["PERCENT", "FIXED"]
    value: Decimal = Field(gt=0)
    segment_code: Optional[str] = None
    description: Optional[str] = None
    conditions: Optional[str] = None

    @model_validator(mode="after")
    def validate_percent(self) -> "ScrapedDiscountDTO":
        if self.kind == "PERCENT" and self.value > 100:
            raise ValueError("Un descuento porcentual no puede superar 100")
        return self


class ScrapedPaymentPlanDTO(BaseModel):
    installments: int = Field(ge=1)
    cards: List[str] = Field(default_factory=list)
    interest_rate: Decimal = Field(default=Decimal("0"), ge=0)


class ScrapedPriceTierDTO(BaseModel):
    segment_code: str = Field(min_length=1)
    amount: Decimal = Field(ge=0)
    currency: Literal["PEN", "USD"] = "PEN"
    notes: Optional[str] = None


class ScrapedOfferingDTO(BaseModel):
    external_code: Optional[str] = None
    modality_code: Literal[
        "VIRTUAL_SINC", "VIRTUAL_ASINC", "PRESENCIAL", "HIBRIDO"
    ] = "VIRTUAL_SINC"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    duration_hours: Optional[Decimal] = Field(default=None, ge=0)
    duration_weeks: Optional[Decimal] = Field(default=None, ge=0)
    sessions_count: Optional[int] = Field(default=None, ge=0)
    schedules: List[ScheduleItemSchema] = Field(default_factory=list)
    schedule_text: Optional[str] = None
    currency: Literal["PEN", "USD"] = "PEN"
    list_amount: Decimal = Field(ge=0)
    final_amount: Decimal = Field(ge=0)
    discounts: List[ScrapedDiscountDTO] = Field(default_factory=list)
    price_tiers: List[ScrapedPriceTierDTO] = Field(default_factory=list)
    payment_plans: List[ScrapedPaymentPlanDTO] = Field(default_factory=list)
    raw_payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_offering(self) -> "ScrapedOfferingDTO":
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValueError("end_date no puede ser anterior a start_date")
        if not self.external_code:
            if self.start_date is None:
                self.external_code = "PERMANENT"
            else:
                schedule_slug = slugify(self.schedule_text or "")
                self.external_code = (
                    f"{self.start_date.isoformat()}_{schedule_slug[:30]}"
                    if schedule_slug
                    else self.start_date.isoformat()
                )
        return self


class ScrapedCourseDTO(BaseModel):
    source_code: Literal["PUCP", "CIBERTEC"]
    external_id: str = Field(min_length=1)
    course_url: HttpUrl
    category_name: Optional[str] = None
    name: str = Field(min_length=3)
    description: Optional[str] = None
    duration_hours: Optional[Decimal] = Field(default=None, ge=0)
    has_certificate: bool = False
    has_participation_certificate: bool = False
    content_items: List[str] = Field(default_factory=list)
    prerequisite_items: List[str] = Field(default_factory=list)
    hardware_requirements: List[str] = Field(default_factory=list)
    software_requirements: List[str] = Field(default_factory=list)
    offerings: List[ScrapedOfferingDTO] = Field(default_factory=list)
    raw_payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_offering(self) -> "ScrapedCourseDTO":
        if not self.offerings:
            raise ValueError("El curso debe incluir al menos una cohorte o una oferta permanente")
        return self


def pucp_course_to_dto(data: CourseExtractedSchema) -> ScrapedCourseDTO:
    """Adapt the established PUCP extractor contract to the shared ETL DTO."""
    parsed_url = urlparse(str(data.course_url))
    slug = parsed_url.path.rstrip("/").split("/")[-1] or data.external_id
    details = data.price_details_raw or ""

    def amount(value: str) -> Decimal:
        normalized = value.replace(" ", "")
        if "," in normalized and "." in normalized:
            normalized = normalized.replace(",", "")
        elif "," in normalized:
            normalized = normalized.replace(",", ".")
        return Decimal(normalized)

    regular_match = re.search(
        r"Tarifa\s+general:\s*S/?\.?\s*([\d.,]+)", details, re.IGNORECASE
    )
    tiers: list[ScrapedPriceTierDTO] = []
    community_match = re.search(
        r"Comunidad\s+PUCP[^:]*:\s*S/?\.?\s*([\d.,]+)", details, re.IGNORECASE
    )
    if community_match:
        community_amount = amount(community_match.group(1))
        for code in (
            "PUCP_PREGRADO", "PUCP_POSGRADO", "PUCP_EGRESADO",
            "PUCP_DOCENTE", "PUCP_COLABORADOR",
        ):
            tiers.append(ScrapedPriceTierDTO(segment_code=code, amount=community_amount))
    other_university_match = re.search(
        r"Alumnos?\s+de\s+pregrado\s+otras\s+universidades[^:]*:\s*S/?\.?\s*([\d.,]+)",
        details,
        re.IGNORECASE,
    )
    if other_university_match:
        tiers.append(ScrapedPriceTierDTO(
            segment_code="OTRA_UNIV_PREGRADO",
            amount=amount(other_university_match.group(1)),
        ))

    discounts: list[ScrapedDiscountDTO] = []
    for label, code in (
        (r"ASOCEGRE|Asociado\s+Hábil", "ASOCEGRE"),
        (r"Instituciones?\s+Educativas?\s+en\s+convenio", "INSTITUCION_CONVENIO"),
        (r"empresas?\s+en\s+convenio", "EMPRESA_CONVENIO"),
    ):
        match = re.search(
            rf"(?:{label})[^:]*:\s*([\d.,]+)\s*%",
            details,
            re.IGNORECASE,
        )
        if match:
            discounts.append(ScrapedDiscountDTO(
                kind="PERCENT",
                value=amount(match.group(1)),
                segment_code=code,
                description=match.group(0).strip(),
            ))

    payment_plans: list[ScrapedPaymentPlanDTO] = []
    for match in re.finditer(
        r"Tarjetas?\s+de\s+crédito\s+([^:]+):\s*(.*?)(?=Tarjetas?\s+de\s+crédito|$)",
        details,
        re.IGNORECASE,
    ):
        card_name, terms = match.groups()
        counts = re.search(r"((?:\d+\s*(?:;|,|y)?\s*)+)\s*cuotas?", terms, re.I)
        if not counts:
            single_count = re.search(r"hasta\s+(\d+)\s+cuotas?", terms, re.I)
            installments = [int(single_count.group(1))] if single_count else []
        else:
            installments = [int(value) for value in re.findall(r"\d+", counts.group(1))]
        for count in dict.fromkeys(installments):
            payment_plans.append(ScrapedPaymentPlanDTO(
                installments=count, cards=[card_name.strip()]
            ))

    return ScrapedCourseDTO(
        source_code="PUCP",
        external_id=data.external_id,
        course_url=data.course_url,
        category_name=data.category_name,
        name=data.name,
        description=data.description,
        duration_hours=data.duration_hours,
        has_certificate=data.has_certificate,
        has_participation_certificate=data.has_participation_certificate,
        content_items=data.content_items,
        offerings=[
            ScrapedOfferingDTO(
                external_code=data.external_offering_code or None,
                modality_code=data.modality_code,
                start_date=data.start_date,
                end_date=data.end_date,
                duration_hours=data.duration_hours,
                schedules=data.schedules,
                currency=data.currency,
                list_amount=amount(regular_match.group(1)) if regular_match else data.price_amount,
                final_amount=data.price_amount,
                discounts=discounts,
                price_tiers=tiers,
                payment_plans=payment_plans,
                raw_payload={
                    "brochure_url": data.brochure_url_raw,
                    "target_audience": data.target_audience_raw,
                    "start_date_text": data.start_date_str,
                    "end_date_text": data.end_date_str,
                    "price_details": details,
                },
            )
        ],
        raw_payload={"slug": slug},
    )