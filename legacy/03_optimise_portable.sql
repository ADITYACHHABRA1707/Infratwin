-- =============================================================================
-- InfraTwin  |  03_optimise_portable.sql
-- Run AFTER seed.py. Builds the optimised structures and the un-optimised
-- baseline that the benchmark measures against, on plain PostgreSQL.
--
-- Produces a relation named readings_hourly with columns
--   (bucket, asset_id, avg_temp, avg_torque, avg_rpm, max_wear)
-- identical to the TimescaleDB path, so every downstream query, the app and
-- the benchmark are byte-for-byte the same on both engines.
-- =============================================================================

-- 1) Hourly roll-up ----------------------------------------------------------
-- A plain materialized view. date_trunc is standard PostgreSQL, so no
-- extension is needed. Refreshed explicitly (see REFRESH below); on the
-- TimescaleDB path the equivalent object keeps itself current.
DROP MATERIALIZED VIEW IF EXISTS readings_hourly;

CREATE MATERIALIZED VIEW readings_hourly AS
SELECT date_trunc('hour', time) AS bucket,
       asset_id,
       avg(process_temp) AS avg_temp,
       avg(torque)       AS avg_torque,
       avg(rpm)          AS avg_rpm,
       max(tool_wear)    AS max_wear
FROM sensor_readings
GROUP BY 1, 2
WITH NO DATA;

REFRESH MATERIALIZED VIEW readings_hourly;

-- A unique index both serves the lookups and allows REFRESH CONCURRENTLY,
-- so the dashboard never reads a half-built roll-up.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_bucket_asset
  ON readings_hourly (bucket, asset_id);
CREATE INDEX IF NOT EXISTS idx_hourly_asset
  ON readings_hourly (asset_id);

-- 2) Vector index ------------------------------------------------------------
-- HNSW over cosine distance. Built after the rows are loaded, which is both
-- faster and gives a better graph than inserting into an existing index.
DROP INDEX IF EXISTS idx_reports_hnsw;
CREATE INDEX idx_reports_hnsw
  ON maintenance_reports USING hnsw (embedding vector_cosine_ops);

-- 3) Baseline ----------------------------------------------------------------
-- Same rows, one unpartitioned heap, no indexes. This is the "before" column
-- on the evidence slide. LIKE copies column definitions only, so no index or
-- constraint leaks into the baseline and flatters it.
DROP TABLE IF EXISTS sensor_readings_plain;
CREATE TABLE sensor_readings_plain (LIKE sensor_readings);
INSERT INTO sensor_readings_plain SELECT * FROM sensor_readings;

-- 4) Statistics --------------------------------------------------------------
-- Without this the planner works from stale estimates and the benchmark
-- measures bad plans rather than the structures under test.
ANALYZE sensor_readings;
ANALYZE sensor_readings_plain;
ANALYZE readings_hourly;
ANALYZE maintenance_reports;
ANALYZE report_bench;
