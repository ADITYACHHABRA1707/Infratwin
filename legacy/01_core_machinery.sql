-- =============================================================================
-- InfraTwin  |  01_core.sql
-- Engine-agnostic relational core: entities, constraints, audit trigger.
-- Runs identically on plain PostgreSQL 16 and on TimescaleDB.
-- The sensor_readings time-series table is created by 02_timeseries_*.sql,
-- because the partitioning clause differs between the two engines.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- assets : every physical thing in the plant. One table, self-referencing
-- hierarchy expressed through asset_dependencies (not a parent_id column), so
-- an asset can have more than one dependent and the graph can be traversed
-- in either direction.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
  asset_id     SERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  asset_type   TEXT NOT NULL CHECK (asset_type IN ('Plant','Line','Pump','Motor')),
  location     TEXT,
  install_date DATE NOT NULL DEFAULT CURRENT_DATE,
  CONSTRAINT assets_name_not_blank CHECK (length(btrim(name)) > 0)
);

-- -----------------------------------------------------------------------------
-- asset_dependencies : the directed graph edge.
--
--   (asset_id) --dependency_type--> (dependent_id)
--
-- Read it as "dependent_id depends on asset_id", i.e. if asset_id fails,
-- dependent_id is affected. A Motor powers a Pump, so the row is
-- (motor, pump, 'powers') and a motor failure cascades up to the pump.
--
-- ON DELETE CASCADE keeps the graph consistent if an asset is retired.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_dependencies (
  asset_id        INT  NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  dependent_id    INT  NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  dependency_type TEXT NOT NULL CHECK (dependency_type IN ('feeds','powers','cools')),
  PRIMARY KEY (asset_id, dependent_id),
  CONSTRAINT no_self_dependency CHECK (asset_id <> dependent_id)
);

-- Forward traversal (who is affected by X) uses the PK's leading column.
-- This extra index serves the reverse direction (what does X depend on).
CREATE INDEX IF NOT EXISTS idx_dep_dependent ON asset_dependencies(dependent_id);

-- -----------------------------------------------------------------------------
-- failure_events : a confirmed failure on a specific asset at a specific time.
-- failure_type codes come from the AI4I 2020 dataset.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failure_events (
  event_id     SERIAL PRIMARY KEY,
  asset_id     INT         NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  event_time   TIMESTAMPTZ NOT NULL,
  failure_type TEXT        NOT NULL CHECK (failure_type IN ('TWF','HDF','PWF','OSF','RNF'))
);

CREATE INDEX IF NOT EXISTS idx_events_asset_time ON failure_events(asset_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_events_type       ON failure_events(failure_type);

-- -----------------------------------------------------------------------------
-- maintenance_reports : what the technician wrote, plus a 384-dim embedding of
-- that text. 1:1 with failure_events, enforced by the UNIQUE on event_id.
-- The embedding is what makes "find failures that look like this one" a single
-- SQL query instead of a separate search service.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_reports (
  report_id    SERIAL PRIMARY KEY,
  event_id     INT  NOT NULL UNIQUE REFERENCES failure_events(event_id) ON DELETE CASCADE,
  summary      TEXT NOT NULL,
  action_taken TEXT NOT NULL,
  embedding    vector(384)
);

-- -----------------------------------------------------------------------------
-- report_bench : benchmark scaffolding only. Random unit vectors at a volume
-- where exact scan and HNSW diverge measurably. Kept separate from
-- maintenance_reports so the demo data stays honest.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_bench (
  id        SERIAL PRIMARY KEY,
  embedding vector(384) NOT NULL
);

-- -----------------------------------------------------------------------------
-- audit_logs : append-only trail, written by trigger, never by the application.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
  log_id    BIGSERIAL PRIMARY KEY,
  entity    TEXT        NOT NULL,
  action    TEXT        NOT NULL CHECK (action IN ('INSERT','UPDATE','DELETE')),
  row_pk    INT,
  log_time  TIMESTAMPTZ NOT NULL DEFAULT now(),
  details   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_entity_time ON audit_logs(entity, log_time DESC);

-- -----------------------------------------------------------------------------
-- demo_injection : demo scaffolding, not part of the domain model.
--
-- seed.py and scripts/refresh_demo.py raise temperature and torque on a few
-- motors so the dashboard has something to detect. This records what was raised
-- and from when, so the next run can undo its predecessor exactly instead of
-- stacking a second +9 K on the same rows. Drop this table in a real deployment.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demo_injection (
  asset_id  INT         NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  from_time TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (asset_id)
);

-- -----------------------------------------------------------------------------
-- Audit trigger.
--
-- Note on TG_OP: in a DELETE trigger NEW is unassigned, and in an INSERT
-- trigger OLD is unassigned. Touching the wrong one raises
-- "record 'new' is not assigned yet", so the branch is explicit rather than
-- a COALESCE(NEW, OLD) that happens to work on some versions only.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION log_report_change() RETURNS trigger AS $$
DECLARE
  v_pk INT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_pk := OLD.report_id;
  ELSE
    v_pk := NEW.report_id;
  END IF;

  INSERT INTO audit_logs(entity, action, row_pk, details)
  VALUES ('maintenance_reports', TG_OP, v_pk, 'report_id=' || v_pk);

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_report_audit ON maintenance_reports;
CREATE TRIGGER trg_report_audit
AFTER INSERT OR UPDATE OR DELETE ON maintenance_reports
FOR EACH ROW EXECUTE FUNCTION log_report_change();
