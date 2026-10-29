-- =============================================================================
-- ChainTwin  |  03_optimise.sql
-- Run AFTER seed.py. Builds the optimised structures and the un-optimised
-- baseline the benchmark measures against.
-- =============================================================================

-- 1) Hourly roll-up ----------------------------------------------------------
-- The anomaly baseline reads this instead of re-scanning every raw minute.
DROP MATERIALIZED VIEW IF EXISTS readings_hourly;

CREATE MATERIALIZED VIEW readings_hourly AS
SELECT date_trunc('hour', time) AS bucket,
       unit_id,
       avg(temp_c)   AS avg_temp,
       max(temp_c)   AS max_temp,
       avg(humidity) AS avg_humidity,
       count(*)      AS n
FROM sensor_readings
GROUP BY 1, 2
WITH NO DATA;

REFRESH MATERIALIZED VIEW readings_hourly;

-- The unique index serves lookups and permits REFRESH ... CONCURRENTLY, so the
-- dashboard never reads a half-built roll-up.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_bucket_unit ON readings_hourly (bucket, unit_id);
CREATE INDEX        IF NOT EXISTS idx_hourly_unit        ON readings_hourly (unit_id);

-- 2) Vector index ------------------------------------------------------------
-- HNSW over cosine distance, built after the rows are loaded (faster, and a
-- better graph than inserting into an existing index).
DROP INDEX IF EXISTS idx_reports_hnsw;
CREATE INDEX idx_reports_hnsw
  ON incident_reports USING hnsw (embedding vector_cosine_ops);

-- 3) Baseline ----------------------------------------------------------------
-- Same rows in one unpartitioned heap with no indexes: the "before" column.
-- LIKE copies column definitions only, so no index leaks in and flatters it.
DROP TABLE IF EXISTS sensor_readings_plain;
CREATE TABLE sensor_readings_plain (LIKE sensor_readings);
INSERT INTO sensor_readings_plain SELECT * FROM sensor_readings;

-- 4) Statistics --------------------------------------------------------------
-- Without this the planner works from stale estimates and the benchmark
-- measures bad plans rather than the structures under test.
ANALYZE sensor_readings;
ANALYZE sensor_readings_plain;
ANALYZE readings_hourly;
ANALYZE incident_reports;
ANALYZE report_bench;
ANALYZE lots;
ANALYZE lot_flows;
ANALYZE custody;
