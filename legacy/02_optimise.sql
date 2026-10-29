-- Run AFTER seed.py. Builds the optimised pieces and the un-optimised baseline.

-- 1) Continuous aggregate: hourly roll-up, kept up to date by TimescaleDB
CREATE MATERIALIZED VIEW readings_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       asset_id,
       avg(process_temp) AS avg_temp,
       avg(torque)       AS avg_torque,
       avg(rpm)          AS avg_rpm,
       max(tool_wear)    AS max_wear
FROM sensor_readings
GROUP BY bucket, asset_id
WITH NO DATA;

CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL);

-- 2) HNSW index for similar-failure search
CREATE INDEX idx_reports_hnsw ON maintenance_reports USING hnsw (embedding vector_cosine_ops);

-- 3) Baseline: same rows, plain table, no partitioning, no indexes (LIKE copies columns only)
DROP TABLE IF EXISTS sensor_readings_plain;
CREATE TABLE sensor_readings_plain (LIKE sensor_readings);
INSERT INTO sensor_readings_plain SELECT * FROM sensor_readings;

ANALYZE sensor_readings;
ANALYZE sensor_readings_plain;
