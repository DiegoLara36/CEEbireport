from django.db import connection
from django.shortcuts import render


KPI_SQL = """
    SELECT source_code, currency, active_courses, current_offers,
           average_price, average_cost_per_hour, average_duration_hours,
           discount_or_installment_pct
    FROM bi.v_kpi_summary
    WHERE (%s = '' OR source_code = %s)
      AND (%s = '' OR currency = %s)
    ORDER BY source_code, currency;
"""

FILTER_OPTIONS_SQL = """
    SELECT DISTINCT category_name, modality
    FROM bi.v_current_catalog
    ORDER BY category_name, modality;
"""

COURSE_TOTALS_SQL = """
    SELECT source_code, COUNT(*)
    FROM bi.v_current_catalog
    GROUP BY source_code
    ORDER BY source_code;
"""

COURSES_SQL = """
    SELECT course_id, offering_id, source_code, course_name, category_name,
           modality, duration_hours, duration_weeks, course_url,
           has_certificate, list_amount, final_amount, currency,
           cost_per_hour, has_discount, has_financing, instructors,
           certificates, start_date, end_date
    FROM bi.v_current_catalog
    WHERE (%s = '' OR source_code = %s)
      AND (%s = '' OR modality = %s)
      AND (%s = '' OR category_name = %s)
      AND (%s = '' OR currency = %s)
      AND (%s = '' OR course_name ILIKE %s OR category_name ILIKE %s)
    ORDER BY source_code, course_name;
"""

CATEGORY_CHART_SQL = """
    SELECT source_code, currency, normalized_category, category_name, modality,
           course_count, average_cost_per_hour
    FROM bi.v_courses_by_category
    WHERE (%s = '' OR source_code = %s)
      AND (%s = '' OR modality = %s)
      AND (%s = '' OR category_name = %s)
      AND (%s = '' OR currency = %s)
    ORDER BY category_name, source_code;
"""


def _read_rows(cursor, sql, params=()):
    cursor.execute(sql, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def dashboard(request):
    query = request.GET.get("q", "").strip()
    selected_source = request.GET.get("source", "").strip().upper()
    if selected_source not in {"", "PUCP", "CIBERTEC"}:
        selected_source = ""
    selected_modality = request.GET.get("modality", "").strip()
    selected_category = request.GET.get("category", "").strip()
    selected_currency = request.GET.get("currency", "").strip().upper()
    if selected_currency not in {"", "PEN", "USD"}:
        selected_currency = ""
    search_pattern = f"%{query}%"
    common_filters = (
        selected_source,
        selected_source,
        selected_modality,
        selected_modality,
        selected_category,
        selected_category,
        selected_currency,
        selected_currency,
    )

    with connection.cursor() as cursor:
        kpis = _read_rows(
            cursor,
            KPI_SQL,
            (selected_source, selected_source, selected_currency, selected_currency),
        )
        options = _read_rows(cursor, FILTER_OPTIONS_SQL)
        cursor.execute(COURSE_TOTALS_SQL)
        course_totals = dict(cursor.fetchall())
        courses = _read_rows(
            cursor,
            COURSES_SQL,
            common_filters
            + (query, search_pattern, search_pattern),
        )
        category_rows = _read_rows(cursor, CATEGORY_CHART_SQL, common_filters)

    course_counts = {
        "PUCP": course_totals.get("PUCP", 0),
        "CIBERTEC": course_totals.get("CIBERTEC", 0),
    }
    if selected_source:
        course_counts = {
            source: total if source == selected_source else 0
            for source, total in course_counts.items()
        }

    currency_kpis = {}
    for row in kpis:
        currency = row["currency"] or "Sin moneda"
        currency_kpis.setdefault(currency, []).append(row)

    chart_categories = sorted(
        {
            (row["currency"], row["category_name"], row["modality"])
            for row in category_rows
            if row["currency"] is not None
        },
        key=lambda item: (item[0], item[1].casefold(), item[2].casefold()),
    )
    chart_sources = ("PUCP", "CIBERTEC")
    category_cost_datasets = []
    currencies = sorted({currency for currency, _, _ in chart_categories})
    for currency in currencies:
        for source, color in zip(
            chart_sources,
            (
                "#830c0f" if currency == "PEN" else "#4f0709",
                "#686868" if currency == "PEN" else "#9a9a9a",
            ),
        ):
            values = []
            for category_currency, category, modality in chart_categories:
                match = next(
                    (
                        row
                        for row in category_rows
                        if row["source_code"] == source
                        and row["currency"] == category_currency
                        and row["category_name"] == category
                        and row["modality"] == modality
                    ),
                    None,
                )
                values.append(
                    float(match["average_cost_per_hour"])
                    if match and match["average_cost_per_hour"] is not None
                    else None
                )
            category_cost_datasets.append(
                {
                    "label": f"{source} · {currency}",
                    "data": values,
                    "backgroundColor": color,
                }
            )

    modality_counts = {}
    for course in courses:
        key = (course["source_code"], course["modality"])
        modality_counts[key] = modality_counts.get(key, 0) + 1
    modalities = sorted({modality for _, modality in modality_counts})
    modality_datasets = [
        {
            "label": source,
            "data": [modality_counts.get((source, modality), 0) for modality in modalities],
            "backgroundColor": color,
        }
        for source, color in zip(chart_sources, ("#830c0f", "#686868"))
    ]

    category_volumes = {}
    for row in category_rows:
        key = (row["source_code"], row["currency"], row["category_name"])
        category_volumes[key] = category_volumes.get(key, 0) + row["course_count"]
    top_categories = sorted(
        category_volumes.items(),
        key=lambda item: (-item[1], item[0][2].casefold()),
    )[:8]
    for course in courses:
        course["display_price"] = (
            course["final_amount"]
            if course["final_amount"] is not None
            else course["list_amount"]
        )

    chart_data = {
        "categoryCosts": {
            "labels": [
                f"{currency} · {category} · {modality}"
                for currency, category, modality in chart_categories
            ],
            "datasets": category_cost_datasets,
        },
        "modalities": {"labels": modalities, "datasets": modality_datasets},
        "topCategories": {
            "labels": [
                f"{source} · {category} · {currency or 'Sin moneda'}"
                for (source, currency, category), _ in top_categories
            ],
            "data": [count for _, count in top_categories],
        },
    }

    return render(
        request,
        "catalog/dashboard.html",
        {
            "title": "Reporte BI de Centro de Especialización Ejecutiva",
            "courses": courses,
            "query": query,
            "selected_source": selected_source,
            "selected_modality": selected_modality,
            "selected_category": selected_category,
            "selected_currency": selected_currency,
            "categories": sorted({row["category_name"] for row in options}),
            "modalities": sorted({row["modality"] for row in options}),
            "course_counts": course_counts,
            "currency_kpis": currency_kpis,
            "chart_data": chart_data,
        },
    )


def etl_flow(request):
    return render(request, "catalog/etl_flow.html")
