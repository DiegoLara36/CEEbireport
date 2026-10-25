# etl/schemas.py
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional

class CourseDetailSchema(BaseModel):
    title: str = Field(..., description="Título del curso")
    institution: str = Field(..., description="Institución que dicta el curso (PUCP, Cibertec)")
    category: Optional[str] = Field(None, description="Categoría o área (ej. Ingeniería y Tecnología)")
    duration_hours: Optional[int] = Field(None, description="Duración en horas")
    price_pen: Optional[float] = Field(None, description="Precio regular en Soles")
    start_date: Optional[str] = Field(None, description="Fecha de inicio (texto o formato fecha)")
    instructor: Optional[str] = Field(None, description="Nombre del docente")
    contact: Optional[str] = Field(None, description="Contacto comercial")
    syllabus_url: Optional[HttpUrl] = Field(None, description="URL del brochure o sílabo")
    description: Optional[str] = Field(None, description="Resumen o justificación del curso")