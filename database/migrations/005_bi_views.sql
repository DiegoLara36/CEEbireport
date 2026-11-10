-- CEEBI Phase 5: read-only analytical views.
-- Set ceebi.bi_reader_role on the session before applying this migration.
BEGIN;

CREATE SCHEMA IF NOT EXISTS bi;

DROP VIEW IF EXISTS bi.v_courses_by_category;
DROP VIEW IF EXISTS bi.v_kpi_summary;
DROP VIEW IF EXISTS bi.v_price_history;
DROP VIEW IF EXISTS bi.v_current_catalog;

CREATE OR REPLACE VIEW bi.v_current_catalog AS
SELECT
    c.id AS course_id,
    o.id AS offering_id,
    s.code AS source_code,
    c.name AS course_name,
    COALESCE(
        NULLIF(
            INITCAP(REGEXP_REPLACE(BTRIM(cat.name), '\s+', ' ', 'g')),
            ''
        ),
        'Sin categoría'
    ) AS category_name,
    COALESCE(
        NULLIF(cv.attributes #>> '{category,normalized_label}', ''),
        NULLIF(
            LOWER(REGEXP_REPLACE(BTRIM(COALESCE(cat.name, '')), '\s+', '_', 'g')),
            ''
        ),
        'sin_categoria'
    ) AS normalized_category,
    CASE
        WHEN UPPER(COALESCE(m.code, m.name, '')) LIKE '%HIBRID%' THEN 'Híbrida'
        WHEN UPPER(COALESCE(m.code, m.name, '')) LIKE '%PRESEN%' THEN 'Presencial'
        WHEN UPPER(COALESCE(m.code, m.name, '')) LIKE '%VIRTUAL%' THEN 'Virtual'
        WHEN m.name IS NOT NULL THEN BTRIM(m.name)
        ELSE 'No especificada'
    END AS modality,
    COALESCE(o.duration_hours, c.duration_hours) AS duration_hours,
    COALESCE(o.duration_weeks, c.duration_weeks) AS duration_weeks,
    c.course_url,
    c.has_certificate,
    price.list_amount,
    price.final_amount,
    price.currency,
    price.final_amount / NULLIF(COALESCE(o.duration_hours, c.duration_hours), 0)
        AS cost_per_hour,
    COALESCE(commercial.has_discount, FALSE) AS has_discount,
    COALESCE(commercial.has_financing, FALSE) AS has_financing,
    instructors.names AS instructors,
    certificates.names AS certificates,
    o.start_date,
    o.end_date
FROM course c
JOIN source s ON s.id = c.source_id
LEFT JOIN category cat ON cat.id = c.category_id
LEFT JOIN LATERAL (
    SELECT version.attributes
    FROM course_version version
    WHERE version.course_id = c.id AND version.valid_to IS NULL
    ORDER BY version.valid_from DESC, version.id DESC
    LIMIT 1
) cv ON TRUE
LEFT JOIN LATERAL (
    SELECT co.*
    FROM course_offering co
    WHERE co.course_id = c.id AND co.state = 'ACTIVE'
    ORDER BY
        (co.start_date < CURRENT_DATE) ASC NULLS LAST,
        co.start_date ASC NULLS LAST,
        co.last_seen_at DESC,
        co.id DESC
    LIMIT 1
) o ON TRUE
LEFT JOIN modality m ON m.id = o.modality_id
LEFT JOIN LATERAL (
    SELECT ps.list_amount, ps.final_amount, ps.currency
    FROM price_snapshot ps
    WHERE ps.offering_id = o.id AND ps.is_current = TRUE
    ORDER BY ps.valid_from DESC, ps.id DESC
    LIMIT 1
) price ON TRUE
LEFT JOIN LATERAL (
    SELECT
        EXISTS (
            SELECT 1
            FROM price_snapshot_discount psd
            JOIN discount d ON d.id = psd.discount_id
            JOIN price_snapshot ps ON ps.id = psd.price_snapshot_id
            WHERE ps.offering_id = o.id AND ps.is_current = TRUE
              AND psd.applied = TRUE
              AND (d.course_id = c.id OR d.offering_id = o.id)
              AND (d.valid_from IS NULL OR d.valid_from <= CURRENT_DATE)
              AND (d.valid_to IS NULL OR d.valid_to >= CURRENT_DATE)
        ) AS has_discount,
        EXISTS (
            SELECT 1
            FROM payment_plan pp
            WHERE pp.offering_id = o.id AND pp.is_current = TRUE
              AND pp.installments > 1
        ) AS has_financing
) commercial ON TRUE
LEFT JOIN LATERAL (
    SELECT ARRAY_AGG(DISTINCT p.full_name ORDER BY p.full_name) AS names
    FROM offering_professor op
    JOIN professor p ON p.id = op.professor_id
    WHERE op.offering_id = o.id
) instructors ON TRUE
LEFT JOIN LATERAL (
    SELECT ARRAY_AGG(DISTINCT ct.name ORDER BY ct.name) AS names
    FROM offering_certificate oc
    JOIN certificate_type ct ON ct.id = oc.certificate_type_id
    WHERE oc.offering_id = o.id
) certificates ON TRUE
WHERE s.active = TRUE
  AND s.code IN ('PUCP', 'CIBERTEC')
  AND c.state = 'ACTIVE';

COMMENT ON VIEW bi.v_current_catalog IS
    'Granularity: one row per active course from PUCP or Cibertec, with one representative active offering. Child collections are aggregated independently.';

CREATE OR REPLACE VIEW bi.v_kpi_summary AS
WITH active_offerings AS (
    SELECT
        s.code AS source_code,
        c.id AS course_id,
        o.id AS offering_id,
        p.currency,
        p.final_amount,
        COALESCE(o.duration_hours, c.duration_hours) AS duration_hours,
        (
            EXISTS (
                SELECT 1
                FROM price_snapshot_discount psd
                JOIN discount d ON d.id = psd.discount_id
                JOIN price_snapshot ps ON ps.id = psd.price_snapshot_id
                WHERE ps.offering_id = o.id AND ps.is_current = TRUE
                  AND psd.applied = TRUE
                  AND (d.course_id = c.id OR d.offering_id = o.id)
                  AND (d.valid_from IS NULL OR d.valid_from <= CURRENT_DATE)
                  AND (d.valid_to IS NULL OR d.valid_to >= CURRENT_DATE)
            )
        ) AS has_discount,
        EXISTS (
            SELECT 1
            FROM payment_plan pp
            WHERE pp.offering_id = o.id AND pp.is_current = TRUE
              AND pp.installments > 1
        ) AS has_financing
    FROM course c
    JOIN source s ON s.id = c.source_id AND s.active = TRUE
    LEFT JOIN course_offering o
        ON o.course_id = c.id AND o.state = 'ACTIVE'
    LEFT JOIN LATERAL (
        SELECT ps.currency, ps.final_amount
        FROM price_snapshot ps
        WHERE ps.offering_id = o.id AND ps.is_current = TRUE
        ORDER BY ps.valid_from DESC, ps.id DESC
        LIMIT 1
    ) p ON TRUE
    WHERE c.state = 'ACTIVE'
      AND s.code IN ('PUCP', 'CIBERTEC')
)
SELECT
    source_code,
    currency,
    COUNT(DISTINCT course_id) AS active_courses,
    COUNT(DISTINCT offering_id) AS current_offers,
    AVG(final_amount) AS average_price,
    AVG(final_amount / NULLIF(duration_hours, 0)) AS average_cost_per_hour,
    AVG(duration_hours) AS average_duration_hours,
    ROUND(
        100.0 * COUNT(DISTINCT offering_id)
            FILTER (WHERE has_discount OR has_financing)
            / NULLIF(COUNT(DISTINCT offering_id), 0),
        2
    ) AS discount_or_installment_pct
FROM active_offerings
GROUP BY source_code, currency;

COMMENT ON VIEW bi.v_kpi_summary IS
    'Granularity: institution and currency. Active course counts are distinct within each currency; no monetary values are combined across currencies.';

CREATE OR REPLACE VIEW bi.v_courses_by_category AS
SELECT
    source_code,
    currency,
    normalized_category,
    category_name,
    modality,
    COUNT(*) AS course_count,
    AVG(cost_per_hour) AS average_cost_per_hour
FROM bi.v_current_catalog
GROUP BY source_code, currency, normalized_category, category_name, modality;

COMMENT ON VIEW bi.v_courses_by_category IS
    'Granularity: institution, currency, normalized category, and normalized modality; one contribution per active course.';

CREATE OR REPLACE VIEW bi.v_price_history AS
SELECT
    ps.id AS price_snapshot_id,
    s.code AS source_code,
    c.id AS course_id,
    o.id AS offering_id,
    c.name AS course_name,
    ps.currency,
    ps.list_amount,
    ps.final_amount,
    ps.final_amount / NULLIF(COALESCE(o.duration_hours, c.duration_hours), 0)
        AS cost_per_hour,
    ps.valid_from,
    ps.valid_to,
    ps.is_current
FROM price_snapshot ps
JOIN course_offering o ON o.id = ps.offering_id
JOIN course c ON c.id = o.course_id
JOIN source s ON s.id = c.source_id
WHERE s.code IN ('PUCP', 'CIBERTEC');

COMMENT ON VIEW bi.v_price_history IS
    'Granularity: one row per price snapshot, offering, and currency, ordered by valid_from for temporal price analysis.';

DO $permissions$
DECLARE
    reader_role TEXT := current_setting('ceebi.bi_reader_role', TRUE);
BEGIN
    IF reader_role IS NULL OR BTRIM(reader_role) = '' THEN
        RAISE EXCEPTION
            'Set ceebi.bi_reader_role to the configured read-only role before applying migration 005';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        RAISE EXCEPTION 'BI read-only role "%" does not exist', reader_role;
    END IF;

    EXECUTE FORMAT('GRANT USAGE ON SCHEMA bi TO %I', reader_role);
    EXECUTE FORMAT(
        'GRANT SELECT ON bi.v_current_catalog, bi.v_kpi_summary, '
        'bi.v_courses_by_category, bi.v_price_history TO %I',
        reader_role
    );
END;
$permissions$;

COMMIT;
