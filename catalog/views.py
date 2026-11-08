from collections import Counter

from django.db import connection
from django.shortcuts import render


COURSE_REPORT_SQL = """
    SELECT
        course.id,
        course.name,
        COALESCE(category.name, 'Sin categoría') AS category_name,
        course.course_url,
        course.duration_hours,
        course.has_certificate,
        offering.start_date,
        offering.end_date,
        offering.modality_name,
        price.list_amount,
        price.final_amount,
        price.currency
    FROM course
    JOIN source ON source.id = course.source_id
    LEFT JOIN category ON category.id = course.category_id
    LEFT JOIN LATERAL (
        SELECT
            course_offering.id,
            course_offering.start_date,
            course_offering.end_date,
            modality.name AS modality_name
        FROM course_offering
        LEFT JOIN modality ON modality.id = course_offering.modality_id
        WHERE course_offering.course_id = course.id
          AND course_offering.state = 'ACTIVE'
        ORDER BY (course_offering.start_date < CURRENT_DATE) ASC NULLS LAST,
                 course_offering.start_date ASC NULLS LAST,
                 course_offering.last_seen_at DESC
        LIMIT 1
    ) AS offering ON TRUE
    LEFT JOIN LATERAL (
        SELECT price_snapshot.list_amount,
               price_snapshot.final_amount,
               price_snapshot.currency
        FROM price_snapshot
        WHERE price_snapshot.offering_id = offering.id
          AND price_snapshot.is_current = TRUE
        ORDER BY price_snapshot.valid_from DESC,
                 price_snapshot.created_at DESC
        LIMIT 1
    ) AS price ON TRUE
    WHERE source.code = %s
      AND source.active = TRUE
      AND course.state = 'ACTIVE'
      AND (
          %s = ''
          OR course.name ILIKE %s
          OR COALESCE(category.name, '') ILIKE %s
      )
      AND (%s = '' OR COALESCE(category.name, 'Sin categoría') = %s)
    ORDER BY course.name;
"""

CATEGORY_OPTIONS_SQL = """
    SELECT DISTINCT COALESCE(category.name, 'Sin categoría')
    FROM course
    JOIN source ON source.id = course.source_id
    LEFT JOIN category ON category.id = course.category_id
    WHERE source.code = %s
      AND source.active = TRUE
      AND course.state = 'ACTIVE'
    ORDER BY 1;
"""

SOURCE_SUMMARY_SQL = """
    SELECT COUNT(DISTINCT course.id),
           MAX(ingestion_log.finished_at)
               FILTER (WHERE ingestion_log.status = 'COMPLETED')
    FROM source
    LEFT JOIN course
      ON course.source_id = source.id
     AND course.state = 'ACTIVE'
    LEFT JOIN ingestion_log ON ingestion_log.source_id = source.id
    WHERE source.code = %s
      AND source.active = TRUE;
"""


def dashboard(request):
    query = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()
    search_pattern = f"%{query}%"

    with connection.cursor() as cursor:
        cursor.execute(SOURCE_SUMMARY_SQL, ["PUCP"])
        catalog_total, last_updated = cursor.fetchone()
        cursor.execute(CATEGORY_OPTIONS_SQL, ["PUCP"])
        categories = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            COURSE_REPORT_SQL,
            [
                "PUCP",
                query,
                search_pattern,
                search_pattern,
                selected_category,
                selected_category,
            ],
        )
        rows = cursor.fetchall()

    courses = []
    for row in rows:
        courses.append(
            {
                "id": row[0],
                "name": row[1],
                "category": row[2],
                "url": row[3],
                "duration_hours": row[4],
                "has_certificate": row[5],
                "start_date": row[6],
                "end_date": row[7],
                "modality": row[8],
                "list_amount": row[9],
                "final_amount": row[10],
                "display_price": row[10] if row[10] is not None else row[9],
                "has_price": row[9] is not None or row[10] is not None,
                "currency": row[11],
            }
        )

    category_counts = Counter(course["category"] for course in courses)
    highest_category = category_counts.most_common(1)
    highest_category_name, highest_category_count = (
        highest_category[0] if highest_category else ("", 0)
    )
    total_filtered = len(courses)
    priced_courses = sum(course["has_price"] for course in courses)

    category_breakdown = [
        {
            "name": category,
            "count": count,
            "percentage": (
                round(count * 100 / total_filtered) if total_filtered else 0
            ),
        }
        for category, count in sorted(
            category_counts.items(), key=lambda item: (-item[1], item[0].casefold())
        )
    ]

    return render(
        request,
        "catalog/dashboard.html",
        {
            "courses": courses,
            "query": query,
            "selected_category": selected_category,
            "categories": categories,
            "total_courses": total_filtered,
            "catalog_total": catalog_total or 0,
            "category_total": len(category_counts),
            "priced_courses": priced_courses,
            "highest_category_name": highest_category_name,
            "highest_category_count": highest_category_count,
            "highest_category_percentage": (
                round(highest_category_count * 100 / total_filtered)
                if total_filtered
                else 0
            ),
            "category_breakdown": category_breakdown,
            "last_updated": last_updated,
        },
    )


def etl_flow(request):
    return render(request, "catalog/etl_flow.html")
