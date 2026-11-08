-- CEEBI Phase 4: source-level execution reliability and retention.
-- Apply after 003_persistence_history.sql.
BEGIN;

ALTER TABLE ingestion_log
    DROP CONSTRAINT IF EXISTS ingestion_log_status_check,
    DROP CONSTRAINT IF EXISTS chk_ingestion_log_status;

UPDATE ingestion_log
   SET status = CASE
       WHEN status = 'PARTIAL' AND finished_at IS NULL THEN 'RUNNING'
       WHEN status IN ('OK', 'PARTIAL') THEN 'COMPLETED'
       ELSE status
   END;

ALTER TABLE ingestion_log
    ADD CONSTRAINT chk_ingestion_log_status
    CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED', 'ABANDONED'));

ALTER TABLE quarantine_record
    ADD COLUMN IF NOT EXISTS resolved BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

UPDATE quarantine_record
   SET resolved_at = COALESCE(resolved_at, now())
 WHERE resolved = TRUE;

UPDATE quarantine_record
   SET resolved_at = NULL
 WHERE resolved = FALSE AND resolved_at IS NOT NULL;

ALTER TABLE quarantine_record
    DROP CONSTRAINT IF EXISTS chk_quarantine_resolution_state;
ALTER TABLE quarantine_record
    ADD CONSTRAINT chk_quarantine_resolution_state
    CHECK ((resolved AND resolved_at IS NOT NULL) OR
           (NOT resolved AND resolved_at IS NULL));

CREATE OR REPLACE FUNCTION fn_set_quarantine_resolved_at()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.resolved THEN
        IF TG_OP = 'INSERT' OR OLD.resolved = FALSE OR NEW.resolved_at IS NULL THEN
            NEW.resolved_at = COALESCE(NEW.resolved_at, now());
        END IF;
    ELSE
        NEW.resolved_at = NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_quarantine_resolved_at ON quarantine_record;
CREATE TRIGGER tg_quarantine_resolved_at
BEFORE INSERT OR UPDATE ON quarantine_record
FOR EACH ROW EXECUTE FUNCTION fn_set_quarantine_resolved_at();

CREATE INDEX IF NOT EXISTS idx_quarantine_resolved_retention
    ON quarantine_record (resolved_at)
    WHERE resolved = TRUE AND resolved_at IS NOT NULL;

INSERT INTO system_setting (key, value, type, description) VALUES
    ('retention_raw_page_days', '90', 'int', 'Días antes de purgar páginas HTML crudas'),
    ('retention_resolved_quarantine_days', '90', 'int', 'Días desde la resolución antes de purgar cuarentenas'),
    ('retention_ingestion_diagnostic_months', '24', 'int', 'Meses antes de redactar diagnósticos de corridas finalizadas')
ON CONFLICT (key) DO NOTHING;

COMMENT ON COLUMN quarantine_record.resolved_at IS
    'Timestamp asignado al marcar resolved=TRUE; cuarentenas resueltas se retienen por su periodo configurado.';
COMMENT ON COLUMN ingestion_log.counts IS
    'Métricas de corrida; el payload puede limpiarse tras el periodo configurado sin eliminar la fila de proveniencia.';
COMMENT ON COLUMN ingestion_log.error_msg IS
    'Diagnóstico textual limpiable tras el periodo configurado; la corrida y sus referencias históricas permanecen.';
COMMENT ON CONSTRAINT chk_ingestion_log_status ON ingestion_log IS
    'Lifecycle state; terminal state is retained independently from the ingestion quality outcome in counts.';

COMMIT;
