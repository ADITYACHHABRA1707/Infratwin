-- =============================================================================
-- ChainTwin  |  02_timeseries.sql
-- sensor_readings on plain PostgreSQL 16 with native range partitioning by day.
-- No extension beyond pgvector is needed, so this runs on a laptop container
-- and on any hosted PostgreSQL (Supabase, Neon, RDS) that offers pgvector.
-- =============================================================================

DROP TABLE IF EXISTS sensor_readings CASCADE;

CREATE TABLE sensor_readings (
  time     TIMESTAMPTZ NOT NULL,
  unit_id  INT         NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
  temp_c   REAL        NOT NULL,
  humidity REAL
) PARTITION BY RANGE (time);

-- One partition per day, 45 days back to 2 days ahead.
DO $$
DECLARE
  d    DATE := (CURRENT_DATE - INTERVAL '45 days')::date;
  stop DATE := (CURRENT_DATE + INTERVAL '2 days')::date;
BEGIN
  WHILE d < stop LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS %I PARTITION OF sensor_readings
         FOR VALUES FROM (%L) TO (%L)',
      'sensor_readings_p' || to_char(d, 'YYYYMMDD'), d, d + 1);
    d := d + 1;
  END LOOP;
END $$;

-- Catch-all so an out-of-window insert lands somewhere visible instead of
-- raising mid-load. verify.py reports if anything ends up here.
CREATE TABLE IF NOT EXISTS sensor_readings_default
  PARTITION OF sensor_readings DEFAULT;

-- Every question is "one unit over a time window", so (unit_id, time DESC) is
-- the access path. Created on the parent, it propagates to every partition.
CREATE INDEX IF NOT EXISTS idx_readings_unit_time
  ON sensor_readings (unit_id, time DESC);
