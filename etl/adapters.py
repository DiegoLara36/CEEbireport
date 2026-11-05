import re
from decimal import Decimal

from etl.normalization import (
    normalize_category,
    normalize_currency,
    normalize_duration,
    normalize_modality,
    classify_price,
)
from etl.schemas import (
    CertificateSchema,
    CourseExtractedSchema,
    DurationSchema,
    ObservationStatus,
    ScrapedCourseDTO,
    ScrapedDiscountDTO,
    ScrapedOfferingDTO,
    ScrapedPaymentPlanDTO,
    ScrapedPriceTierDTO,
)


def adapt_pucp_course(data: CourseExtractedSchema) -> ScrapedCourseDTO:
    modality_text = data.modality_raw
    normalized_modality = normalize_modality(modality_text)
    category = normalize_category(data.category_name)
    raw_payload = {
        "brochure_url": data.brochure_url_raw,
        "target_audience": data.target_audience_raw,
        "start_date_text": data.start_date_str,
        "end_date_text": data.end_date_str,
        "price_details": data.price_details_raw,
    }
    details = data.price_details_raw or ""

    def amount(value: str) -> Decimal:
        normalized = value.replace(" ", "")
        if "," in normalized and "." in normalized:
            normalized = (
                normalized.replace(".", "").replace(",", ".")
                if normalized.rfind(",") > normalized.rfind(".")
                else normalized.replace(",", "")
            )
        elif "," in normalized:
            tail = normalized.rsplit(",", 1)[-1]
            normalized = (
                normalized.replace(",", ".")
                if len(tail) in (1, 2)
                else normalized.replace(",", "")
            )
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
        tiers.extend(
            ScrapedPriceTierDTO(
                segment_code=code,
                amount=community_amount,
                currency=data.currency,
            )
            for code in (
                "PUCP_PREGRADO",
                "PUCP_POSGRADO",
                "PUCP_EGRESADO",
                "PUCP_DOCENTE",
                "PUCP_COLABORADOR",
            )
        )
    other_university_match = re.search(
        r"Alumnos?\s+de\s+pregrado\s+otras\s+universidades[^:]*:\s*S/?\.?\s*([\d.,]+)",
        details,
        re.IGNORECASE,
    )
    if other_university_match:
        tiers.append(
            ScrapedPriceTierDTO(
                segment_code="OTRA_UNIV_PREGRADO",
                amount=amount(other_university_match.group(1)),
                currency=data.currency,
            )
        )

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
            discounts.append(
                ScrapedDiscountDTO(
                    kind="PERCENT",
                    value=amount(match.group(1)),
                    segment_code=code,
                    description=match.group(0).strip(),
                )
            )

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
            installments = [
                int(value) for value in re.findall(r"\d+", counts.group(1))
            ]
        payment_plans.extend(
            ScrapedPaymentPlanDTO(installments=count, cards=[card_name.strip()])
            for count in dict.fromkeys(installments)
        )
    return ScrapedCourseDTO(
        source_code=data.source_code,
        external_id=data.external_id,
        course_url=data.course_url,
        category_name=data.category_name,
        category=category,
        category_observation=(
            ObservationStatus.OBSERVED if category else ObservationStatus.NOT_FOUND
        ),
        name=data.name,
        description=data.description,
        duration_hours=data.duration_hours,
        duration_observation=(
            ObservationStatus.OBSERVED
            if data.duration_hours is not None
            else ObservationStatus.UNKNOWN
        ),
        durations=normalize_duration(
            data.duration_original or (
                f"{data.duration_hours} horas" if data.duration_hours is not None else None
            )
        ),
        has_certificate=data.has_certificate,
        has_participation_certificate=data.has_participation_certificate,
        certificate_observation=data.certificate_observation,
        participation_certificate_observation=data.participation_certificate_observation,
        certificates=(
            [CertificateSchema(kind="CERTIFICATE")]
            if data.has_certificate
            else []
        )
        + (
            [CertificateSchema(kind="PARTICIPATION")]
            if data.has_participation_certificate
            else []
        ),
        content_items=data.content_items,
        content_observation=data.content_observation,
        professors=[data.professor] if data.professor else [],
        professors_observation=data.professor_observation,
        contacts=[data.contact] if data.contact else [],
        contacts_observation=data.contact_observation,
        offerings=[
            ScrapedOfferingDTO(
                external_code=data.external_offering_code or None,
                modality_code=data.modality_code,
                normalized_modality=normalized_modality,
                modality_raw=modality_text,
                modality_observation=(
                    ObservationStatus.OBSERVED
                    if normalized_modality
                    else ObservationStatus.UNKNOWN
                ),
                start_date=data.start_date,
                start_date_observation=(
                    ObservationStatus.OBSERVED
                    if data.start_date
                    else ObservationStatus.UNKNOWN
                ),
                end_date=data.end_date,
                end_date_observation=(
                    ObservationStatus.OBSERVED
                    if data.end_date
                    else ObservationStatus.UNKNOWN
                ),
                duration_hours=data.duration_hours,
                duration_observation=(
                    ObservationStatus.OBSERVED
                    if data.duration_hours is not None
                    else ObservationStatus.UNKNOWN
                ),
                durations=normalize_duration(
                    data.duration_original or (
                        f"{data.duration_hours} horas"
                        if data.duration_hours is not None
                        else None
                    )
                ),
                schedules=data.schedules,
                schedules_observation=(
                    ObservationStatus.OBSERVED
                    if data.schedules
                    else ObservationStatus.UNKNOWN
                ),
                currency=data.currency,
                currency_raw=data.currency_raw,
                currency_observation=(
                    ObservationStatus.OBSERVED
                    if data.currency
                    else (
                        ObservationStatus.UNKNOWN
                        if data.price_observation == ObservationStatus.OBSERVED
                        else data.price_observation
                    )
                ),
                list_amount=data.price_amount,
                final_amount=data.price_amount,
                price_observation=data.price_observation,
                price_status=classify_price(
                    data.price_amount,
                    data.currency,
                    data.price_observation,
                ),
                price_raw=data.price_raw,
                commercial_conditions=(
                    [data.target_audience_raw] if data.target_audience_raw else []
                ),
                commercial_conditions_observation=(
                    ObservationStatus.OBSERVED
                    if data.target_audience_raw
                    else ObservationStatus.NOT_FOUND
                ),
                discounts=discounts,
                discounts_observation=(
                    ObservationStatus.OBSERVED
                    if data.price_details_raw
                    else ObservationStatus.NOT_COLLECTED
                ),
                price_tiers=tiers,
                price_tiers_observation=(
                    ObservationStatus.OBSERVED
                    if data.price_details_raw
                    else ObservationStatus.NOT_COLLECTED
                ),
                payment_plans=payment_plans,
                payment_plans_observation=(
                    ObservationStatus.OBSERVED
                    if data.price_details_raw
                    else ObservationStatus.NOT_COLLECTED
                ),
                raw_payload=raw_payload,
            )
        ],
        raw_payload={"slug": data.external_id},
    )


def adapt_cibertec_course(data: ScrapedCourseDTO) -> ScrapedCourseDTO:
    category = normalize_category(data.category_name)
    data.category = category
    data.category_observation = (
        ObservationStatus.OBSERVED if category else ObservationStatus.NOT_FOUND
    )
    for offering in data.offerings:
        raw_modality = offering.modality_raw
        offering.normalized_modality = normalize_modality(raw_modality)
        offering.modality_raw = raw_modality
        offering.modality_observation = (
            ObservationStatus.OBSERVED
            if offering.normalized_modality
            else ObservationStatus.UNKNOWN
        )
        if not offering.durations:
            offering.durations = normalize_duration(
                offering.raw_payload.get("duration_text")
            )
        if not offering.durations:
            if offering.duration_hours is not None:
                offering.durations.append(
                    DurationSchema(amount=offering.duration_hours, unit="HOUR")
                )
            if offering.duration_weeks is not None:
                offering.durations.append(
                    DurationSchema(amount=offering.duration_weeks, unit="WEEK")
                )
        offering.currency = normalize_currency(
            offering.currency_raw
            or offering.raw_payload.get("price_text")
            or offering.currency
        )
        offering.currency_observation = (
            ObservationStatus.OBSERVED
            if offering.currency
            else ObservationStatus.UNKNOWN
        )
        offering.price_status = classify_price(
            offering.final_amount,
            offering.currency,
            offering.price_observation,
        )
    return data
