-- =============================================================================
-- InfraTwin  |  02_timeseries_timescale.sql
-- sensor_readings as a TimescaleDB hypertable.
-- Use this path when the timescaledb extension is available
-- (docker compose up -d gives you timescale/timescaledb-ha:pg16).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

DROP TABLE IF EXISTS sensor_readings CASCADE;

CREATE TABLE sensor_readings (
  time         TIMESTAMPTZ NOT NULL,
  asset_id     INT         NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  air_temp     REAL,
  process_temp REAL,
  rpm          INT,
  torque       REAL,
  tool_wear    INT
);

-- create_hypertable's positional signature is deprecated from TimescaleDB 2.13
-- and emits a warning. The by_range() form is the current API; the DO block
-- falls back to the old signature so this file works on 2.9 through 2.17+.
DO $$
BEGIN
  BEGIN
    PERFORM create_hypertable(
      'sensor_readings',
      by_range('time', INTERVAL '1 day')
    );
  EXCEPTION WHEN undefined_function THEN
    PERFORM create_hypertable(
      'sensor_readings', 'time',
      chunk_time_interval => INTERVAL '1 day'
    );
  END;
END $$;

CREATE INDEX IF NOT EXISTS idx_readings_asset_time
  ON sensor_readings (asset_id, time DESC);

-- Columnstore compression on chunks older than 7 days. Optional, but it is the
-- single most visible storage win and worth one line on the benchmark slide.
-- Wrapped because the syntax moved between 2.x versions and compression is not
-- available on every edition.
DO $$
BEGIN
  ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby   = 'time DESC'
  );
  PERFORM add_compression_policy('sensor_readings', INTERVAL '7 days');
EXCEPTION WHEN others THEN
  RAISE NOTICE 'Compression not enabled (%). Core demo is unaffected.', SQLERRM;
END $$;
