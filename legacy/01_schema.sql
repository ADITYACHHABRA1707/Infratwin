CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------- relational core (3NF, with constraints) ----------
CREATE TABLE assets (
  asset_id     SERIAL PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  asset_type   TEXT NOT NULL CHECK (asset_type IN ('Plant','Line','Pump','Motor')),
  location     TEXT,
  install_date DATE DEFAULT CURRENT_DATE
);

-- asset_id -> dependent_id means: dependent_id depends on asset_id
CREATE TABLE asset_dependencies (
  asset_id        INT NOT NULL REFERENCES assets(asset_id),
  dependent_id    INT NOT NULL REFERENCES assets(asset_id),
  dependency_type TEXT NOT NULL CHECK (dependency_type IN ('feeds','powers','cools')),
  PRIMARY KEY (asset_id, dependent_id),
  CHECK (asset_id <> dependent_id)
);
CREATE INDEX idx_dep_asset ON asset_dependencies(asset_id);

-- ---------- time-series (hypertable, partitioned by time) ----------
CREATE TABLE sensor_readings (
  time         TIMESTAMPTZ NOT NULL,
  asset_id     INT NOT NULL REFERENCES assets(asset_id),
  air_temp     REAL,
  process_temp REAL,
  rpm          INT,
  torque       REAL,
  tool_wear    INT
);
SELECT create_hypertable('sensor_readings', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_readings_asset_time ON sensor_readings (asset_id, time DESC);

-- ---------- failures + vector search ----------
CREATE TABLE failure_events (
  event_id     SERIAL PRIMARY KEY,
  asset_id     INT NOT NULL REFERENCES assets(asset_id),
  event_time   TIMESTAMPTZ NOT NULL,
  failure_type TEXT NOT NULL CHECK (failure_type IN ('TWF','HDF','PWF','OSF','RNF'))
);

CREATE TABLE maintenance_reports (
  report_id    SERIAL PRIMARY KEY,
  event_id     INT NOT NULL REFERENCES failure_events(event_id),
  summary      TEXT NOT NULL,
  action_taken TEXT NOT NULL,
  embedding    vector(384)
);

-- benchmark-only table: random vectors, so HNSW vs exact scan is measurable
CREATE TABLE report_bench (
  id        SERIAL PRIMARY KEY,
  embedding vector(384)
);

-- ---------- audit trigger ----------
CREATE TABLE audit_logs (
  log_id   SERIAL PRIMARY KEY,
  entity   TEXT NOT NULL,
  action   TEXT NOT NULL,
  log_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  details  TEXT
);

CREATE FUNCTION log_report_change() RETURNS trigger AS $$
BEGIN
  INSERT INTO audit_logs(entity, action, details)
  VALUES ('maintenance_reports', TG_OP, 'report_id=' || COALESCE(NEW.report_id, OLD.report_id));
  RETURN COALESCE(NEW, OLD);
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_report_audit
AFTER INSERT OR UPDATE OR DELETE ON maintenance_reports
FOR EACH ROW EXECUTE FUNCTION log_report_change();
