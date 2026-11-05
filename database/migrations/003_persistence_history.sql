-- CEEBI Phase 3 additive persistence migration.
-- Back up the database before applying. Apply once with a role allowed to create
-- btree_gist. Do not run DDL.txt; it is a destructive schema reference.
BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE course
    ADD COLUMN IF NOT EXISTS canonical_url TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_course_source_canonical_url
    ON course (source_id, canonical_url)
    WHERE canonical_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS course_identity_alias (
    id BIGSERIAL PRIMARY KEY,
    source_id INT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
    course_id BIGINT NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_course_identity_alias_source_external
        UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_course_identity_alias_course
    ON course_identity_alias (course_id);

ALTER TABLE course_contact
    DROP CONSTRAINT IF EXISTS uq_course_contact;

CREATE TABLE IF NOT EXISTS course_version (
    id BIGSERIAL PRIMARY KEY,
    course_id BIGINT NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    ingestion_id BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    ingestion_id_original BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    fingerprint TEXT NOT NULL,
    attributes JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    CONSTRAINT chk_course_version_valid
        CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ex_course_version_no_overlap
        EXCLUDE USING gist (
            course_id WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_course_version_current
    ON course_version (course_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS course_offering_version (
    id BIGSERIAL PRIMARY KEY,
    offering_id BIGINT NOT NULL REFERENCES course_offering(id) ON DELETE CASCADE,
    ingestion_id BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    ingestion_id_original BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    fingerprint TEXT NOT NULL,
    attributes JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    CONSTRAINT chk_course_offering_version_valid
        CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ex_course_offering_version_no_overlap
        EXCLUDE USING gist (
            offering_id WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_course_offering_version_current
    ON course_offering_version (offering_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS offering_commercial_version (
    id BIGSERIAL PRIMARY KEY,
    offering_id BIGINT NOT NULL REFERENCES course_offering(id) ON DELETE CASCADE,
    ingestion_id BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    ingestion_id_original BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    price_snapshot_id BIGINT REFERENCES price_snapshot(id) ON DELETE SET NULL,
    fingerprint TEXT NOT NULL,
    attributes JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    CONSTRAINT chk_offering_commercial_version_valid
        CHECK (valid_to IS NULL OR valid_to > valid_from),
    CONSTRAINT ex_offering_commercial_version_no_overlap
        EXCLUDE USING gist (
            offering_id WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_offering_commercial_version_current
    ON offering_commercial_version (offering_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS offering_price_observation (
    id BIGSERIAL PRIMARY KEY,
    offering_id BIGINT NOT NULL REFERENCES course_offering(id) ON DELETE CASCADE,
    ingestion_id BIGINT REFERENCES ingestion_log(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PUBLISHED', 'NOT_PUBLISHED', 'UNKNOWN',
        'CURRENCY_UNIDENTIFIED', 'CURRENCY_UNSUPPORTED'
    )),
    list_amount NUMERIC(10,2) CHECK (list_amount IS NULL OR list_amount >= 0),
    final_amount NUMERIC(10,2) CHECK (final_amount IS NULL OR final_amount >= 0),
    currency TEXT,
    currency_raw TEXT,
    price_raw TEXT,
    fingerprint TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    CONSTRAINT chk_offering_price_observation_valid
        CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_offering_price_observation_current
    ON offering_price_observation (offering_id) WHERE valid_to IS NULL;

ALTER TABLE price_snapshot
    ADD COLUMN IF NOT EXISTS ingestion_id BIGINT
        REFERENCES ingestion_log(id) ON DELETE SET NULL;

ALTER TABLE offering_price_tier
    ADD COLUMN IF NOT EXISTS ingestion_id BIGINT
        REFERENCES ingestion_log(id) ON DELETE SET NULL;

ALTER TABLE payment_plan
    ADD COLUMN IF NOT EXISTS ingestion_id BIGINT
        REFERENCES ingestion_log(id) ON DELETE SET NULL;

ALTER TABLE offering_certificate
    ALTER COLUMN price DROP NOT NULL,
    ALTER COLUMN price DROP DEFAULT,
    ALTER COLUMN currency DROP NOT NULL,
    ALTER COLUMN currency DROP DEFAULT;

COMMENT ON TABLE course_version IS
    'Temporal snapshots of normalized course attributes; ingestion references may be nulled by retention.';
COMMENT ON TABLE course_offering_version IS
    'Temporal snapshots of normalized offering attributes; pricing remains in price_snapshot.';
COMMENT ON TABLE offering_commercial_version IS
    'Temporal commercial conditions; price values remain in price_snapshot and related price history.';
COMMENT ON TABLE offering_price_observation IS
    'Observed pricing state, including absent or unsupported currency without inventing a numeric price.';
COMMENT ON COLUMN offering_certificate.price IS
    'NULL means the certificate price was not provided; zero is reserved for an explicitly free certificate.';

COMMIT;
