# etl/schemas.py
from pydantic import BaseModel, Field, HttpUrl, model_validator
from typing import List, Optional, Literal
from datetime import date, time
from decimal import Decimal


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

    @model_validator(mode="after")
    def check_offering_dates(self) -> "CourseExtractedSchema":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date}) no puede ser anterior a start_date ({self.start_date})"
            )
        return self