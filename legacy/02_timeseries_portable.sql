-- =============================================================================
-- InfraTwin  |  02_timeseries_portable.sql
-- sensor_readings on PLAIN PostgreSQL 16, using native declarative
-- partitioning by time (RANGE). No extensions beyond pgvector required.
--
-- Use this path when TimescaleDB is unavailable: locked-down lab PC, no Docker,
-- or a hosted Postgres (Supabase / Neon / RDS) that offers pgvector but not
-- TimescaleDB.
--
-- Partitions are created by a DO block covering a window around today, so the
-- seed can write without a default partition swallowing every row.
-- =============================================================================

DROP TABLE IF EXISTS sensor_readings CASCADE;

CREATE TABLE sensor_readings (
  time         TIMESTAMPTZ NOT NULL,
  asset_id     INT         NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  air_temp     REAL,
  process_temp REAL,
  rpm          INT,
  torque       REAL,
  tool_wear    INT
) PARTITION BY RANGE (time);

-- One partition per day, from 45 days ago to 2 days ahead. Matches the
-- chunk_time_interval => 1 day used on the TimescaleDB path so benchmark
-- numbers are comparable between the two.
DO $$
DECLARE
  d     DATE := (CURRENT_DATE - INTERVAL '45 days')::date;
  stop  DATE := (CURRENT_DATE + INTERVAL '2 days')::date;
  part  TEXT;
BEGIN
  WHILE d < stop LOOP
    part := format('sensor_readings_p%s', to_char(d, 'YYYYMMDD'));
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS %I PARTITION OF sensor_readings
         FOR VALUES FROM (%L) TO (%L)',
      part, d, d + 1);
    d := d + 1;
  END LOOP;
END $$;

-- Catch-all so an out-of-window insert errors loudly in dev rather than
-- silently vanishing. Comment this out in production if you prefer the error.
CREATE TABLE IF NOT EXISTS sensor_readings_default
  PARTITION OF sensor_readings DEFAULT;

-- The workhorse index: every dashboard query filters one asset over a time
-- window, so (asset_id, time DESC) is the access path. Created on the parent,
-- which propagates to every partition.
CREATE INDEX IF NOT EXISTS idx_readings_asset_time
  ON sensor_readings (asset_id, time DESC);
