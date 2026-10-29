-- =============================================================================
-- InfraTwin  |  03_optimise_timescale.sql
-- Run AFTER seed.py. TimescaleDB equivalent of 03_optimise_portable.sql.
--
-- Produces the same readings_hourly contract
--   (bucket, asset_id, avg_temp, avg_torque, avg_rpm, max_wear)
-- so queries.sql, app.py and benchmark.py are unchanged between engines.
-- =============================================================================

-- 1) Continuous aggregate ----------------------------------------------------
-- Unlike a plain materialized view this is incrementally maintained: the
-- refresh policy only recomputes buckets whose underlying rows changed, so the
-- roll-up stays current as sensor data streams in.
--
-- Continuous aggregates require time_bucket() (date_trunc is not accepted) and
-- must be created WITH NO DATA.
DROP MATERIALIZED VIEW IF EXISTS readings_hourly CASCADE;

CREATE MATERIALIZED VIEW readings_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       asset_id,
       avg(process_temp) AS avg_temp,
       avg(torque)       AS avg_torque,
       avg(rpm)          AS avg_rpm,
       max(tool_wear)    AS max_wear
FROM sensor_readings
GROUP BY 1, 2
WITH NO DATA;

-- First fill. CALL cannot run inside an explicit transaction block, which is
-- why this file must be run with psql -f (statement-per-transaction) and not
-- wrapped in BEGIN/COMMIT.
CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL);

-- Keep it current automatically from here on.
DO $$
BEGIN
  PERFORM add_continuous_aggregate_policy('readings_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');
EXCEPTION WHEN others THEN
  RAISE NOTICE 'Refresh policy not added (%). Roll-up still works; refresh manually.', SQLERRM;
END $$;

CREATE INDEX IF NOT EXISTS idx_hourly_asset ON readings_hourly (asset_id);

-- 2) Vector index ------------------------------------------------------------
DROP INDEX IF EXISTS idx_reports_hnsw;
CREATE INDEX idx_reports_hnsw
  ON maintenance_reports USING hnsw (embedding vector_cosine_ops);

-- 3) Baseline ----------------------------------------------------------------
-- Plain heap, no hypertable, no indexes: the "before" column.
DROP TABLE IF EXISTS sensor_readings_plain;
CREATE TABLE sensor_readings_plain (LIKE sensor_readings);
INSERT INTO sensor_readings_plain SELECT * FROM sensor_readings;

-- 4) Statistics --------------------------------------------------------------
ANALYZE sensor_readings;
ANALYZE sensor_readings_plain;
ANALYZE maintenance_reports;
ANALYZE report_bench;
