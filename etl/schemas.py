import hashlib
import json
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Optional

from django.utils.text import slugify
from pydantic import BaseModel, Field, HttpUrl, model_validator


class ObservationStatus(StrEnum):
    OBSERVED = "OBSERVED"
    NOT_FOUND = "NOT_FOUND"
    NOT_COLLECTED = "NOT_COLLECTED"
    UNKNOWN = "UNKNOWN"


class NormalizedModality(StrEnum):
    VIRTUAL = "VIRTUAL"
    PRESENTIAL = "PRESENCIAL"
    HYBRID = "HIBRIDA"


class PublicationStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    CANCELLED = "CANCELLED"
    FINISHED = "FINISHED"


class ScheduleItemSchema(BaseModel):
    day_of_week: int = Field(..., ge=1, le=7, description="1=Lunes ... 7=Domingo")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def check_times(self) -> "ScheduleItemSchema":
        if self.end_time <= self.start_time:
            raise ValueError(
                f"end_time ({self.end_time}) debe ser mayor que start_time ({self.start_time})"
            )
        return self


class DurationSchema(BaseModel):
    amount: Decimal = Field(ge=0)
    unit: Literal["HOUR", "WEEK", "SESSION"]
    original_text: Optional[str] = None


class CategorySchema(BaseModel):
    source_label: Optional[str] = None
    normalized_label: Optional[str] = None
    homologation_code: Optional[str] = None
    observation: ObservationStatus = ObservationStatus.NOT_COLLECTED


class ProfessorSchema(BaseModel):
    full_name: str = Field(..., min_length=2)
    specialty: Optional[str] = None


class ContactSchema(BaseModel):
    name: str = Field(..., min_length=2)
    phone: Optional[str] = None
    email: Optional[str] = None


class CertificateSchema(BaseModel):
    kind: Literal["CERTIFICATE", "PARTICIPATION"]
    issuer: Optional[str] = None
    description: Optional[str] = None


class CourseExtractedSchema(BaseModel):
    """Source-specific PUCP extraction contract; not the shared ETL DTO."""

    source_code: str = "PUCP"
    external_id: str = Field(..., min_length=1)
    course_url: HttpUrl
    category_name: Optional[str] = None
    modality_code: Literal[
        "VIRTUAL_SINC", "VIRTUAL_ASINC", "PRESENCIAL", "HIBRIDO"
    ] = "VIRTUAL_SINC"
    modality_raw: Optional[str] = None

    name: str = Field(..., min_length=3)
    description: Optional[str] = None
    duration_hours: Optional[Decimal] = Field(None, ge=0)
    has_certificate: bool = False
    has_participation_certificate: bool = False
    content_items: list[str] = Field(default_factory=list)
    contact: Optional[ContactSchema] = None
    contact_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    professor: Optional[ProfessorSchema] = None
    professor_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    certificate_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    participation_certificate_observation: ObservationStatus = (
        ObservationStatus.NOT_COLLECTED
    )
    content_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED

    external_offering_code: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    schedules: list[ScheduleItemSchema] = Field(default_factory=list)

    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_raw: Optional[str] = None
    price_observation: ObservationStatus = ObservationStatus.UNKNOWN
    price_amount: Decimal = Field(..., ge=0)
    price_raw: Optional[str] = None
    duration_original: Optional[str] = None
    target_audience_raw: Optional[str] = None
    brochure_url_raw: Optional[str] = None
    start_date_str: Optional[str] = None
    end_date_str: Optional[str] = None
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
    cards: list[str] = Field(default_factory=list)
    interest_rate: Decimal = Field(default=Decimal("0"), ge=0)


class ScrapedPriceTierDTO(BaseModel):
    segment_code: str = Field(min_length=1)
    amount: Decimal = Field(ge=0)
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: Optional[str] = None


class ScrapedOfferingDTO(BaseModel):
    external_code: Optional[str] = None
    modality_code: Literal[
        "VIRTUAL_SINC", "VIRTUAL_ASINC", "PRESENCIAL", "HIBRIDO"
    ] = "VIRTUAL_SINC"
    normalized_modality: Optional[NormalizedModality] = None
    modality_raw: Optional[str] = None
    modality_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    start_date: Optional[date] = None
    start_date_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    end_date: Optional[date] = None
    end_date_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    duration_hours: Optional[Decimal] = Field(default=None, ge=0)
    duration_weeks: Optional[Decimal] = Field(default=None, ge=0)
    sessions_count: Optional[int] = Field(default=None, ge=0)
    duration_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    durations: list[DurationSchema] = Field(default_factory=list)
    schedules: list[ScheduleItemSchema] = Field(default_factory=list)
    schedules_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    schedule_text: Optional[str] = None
    publication_status: Optional[PublicationStatus] = None
    publication_status_observation: ObservationStatus = (
        ObservationStatus.NOT_COLLECTED
    )
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_raw: Optional[str] = None
    currency_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    list_amount: Optional[Decimal] = Field(..., ge=0)
    final_amount: Optional[Decimal] = Field(..., ge=0)
    price_observation: ObservationStatus = ObservationStatus.UNKNOWN
    discounts: list[ScrapedDiscountDTO] = Field(default_factory=list)
    price_tiers: list[ScrapedPriceTierDTO] = Field(default_factory=list)
    payment_plans: list[ScrapedPaymentPlanDTO] = Field(default_factory=list)
    commercial_conditions: list[str] = Field(default_factory=list)
    commercial_conditions_observation: ObservationStatus = (
        ObservationStatus.NOT_COLLECTED
    )
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
    """Shared, source-neutral contract consumed by ETLEngine."""

    source_code: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    course_url: HttpUrl
    category_name: Optional[str] = None
    category: Optional[CategorySchema] = None
    category_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    name: str = Field(min_length=3)
    description: Optional[str] = None
    duration_hours: Optional[Decimal] = Field(default=None, ge=0)
    duration_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    durations: list[DurationSchema] = Field(default_factory=list)

    # Legacy booleans remain for the existing ETLEngine/DDL contract. The
    # observation fields below distinguish unknown from a verified negative.
    has_certificate: bool = False
    has_participation_certificate: bool = False
    certificate_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    participation_certificate_observation: ObservationStatus = (
        ObservationStatus.NOT_COLLECTED
    )
    certificates: list[CertificateSchema] = Field(default_factory=list)

    content_items: list[str] = Field(default_factory=list)
    content_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    prerequisite_items: list[str] = Field(default_factory=list)
    prerequisites_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    hardware_requirements: list[str] = Field(default_factory=list)
    hardware_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    software_requirements: list[str] = Field(default_factory=list)
    software_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    professors: list[ProfessorSchema] = Field(default_factory=list)
    professors_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    contacts: list[ContactSchema] = Field(default_factory=list)
    contacts_observation: ObservationStatus = ObservationStatus.NOT_COLLECTED
    offerings: list[ScrapedOfferingDTO] = Field(default_factory=list)
    raw_payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_offering(self) -> "ScrapedCourseDTO":
        if not self.offerings:
            raise ValueError("El curso debe incluir al menos una cohorte o una oferta permanente")
        return self

    def canonical_content(self) -> dict:
        """Return deterministic normalized business content, excluding raw HTML/source payloads."""
        content = self.model_dump(mode="json", exclude={"raw_payload"})
        for offering in content["offerings"]:
            offering.pop("raw_payload", None)
            offering["schedules"] = sorted(
                offering["schedules"],
                key=lambda row: (
                    row["day_of_week"],
                    row["start_time"],
                    row["end_time"],
                ),
            )
        return content

    def canonical_fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_content(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def pucp_course_to_dto(data: CourseExtractedSchema) -> ScrapedCourseDTO:
    """Compatibility entry point; the institution adapter owns the mapping."""
    from etl.adapters import adapt_pucp_course

    return adapt_pucp_course(data)
