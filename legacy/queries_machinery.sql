-- =============================================================================
-- InfraTwin  |  queries.sql
-- The three queries the demo is built on. One database, three access patterns
-- that are normally three separate systems.
--
-- Run with psql, passing :asset where required, e.g.
--   psql -d infratwin -v asset=45 -f sql/queries.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- A) TIME-SERIES LAYER  |  Anomaly detection
--
-- Each machine is judged against its OWN history, not a global threshold: a
-- pump that always runs warm should not alarm, and a motor that is normally
-- cool should alarm at a lower absolute temperature. That is a z-score over a
-- per-asset baseline.
--
-- The baseline reads from readings_hourly (the pre-aggregated roll-up) rather
-- than the raw table, which is what makes this cheap. The "recent" side reads
-- raw rows, because the last hour matters at full resolution.
--
-- The 3-hour gap between the two windows keeps the injected anomaly out of the
-- baseline it is being measured against.
--
-- GREATEST(stddev, 0.05) guards the divide: a sensor that has never moved has
-- stddev 0, and without the floor every such asset divides by zero.
--
-- TWO conditions, not one. A z-score on its own is hypersensitive when the
-- baseline is nearly flat: a machine whose hourly average varies by 0.1 K will
-- cross z > 3 on a 0.3 K drift that no engineer would call a fault. Requiring a
-- minimum absolute rise as well (1.0 K here) removes that whole class of false
-- positive while keeping the per-asset adaptiveness that makes the z-score
-- worth having.
-- -----------------------------------------------------------------------------
WITH base AS (
  SELECT asset_id,
         avg(avg_temp)                    AS mean_temp,
         GREATEST(stddev(avg_temp), 0.05) AS sd_temp
  FROM readings_hourly
  WHERE bucket < now() - interval '3 hours'
  GROUP BY asset_id
),
recent AS (
  SELECT asset_id, avg(process_temp) AS recent_temp
  FROM sensor_readings
  WHERE time > now() - interval '1 hour'
  GROUP BY asset_id
)
SELECT a.asset_id,
       a.name,
       a.asset_type,
       round(r.recent_temp::numeric, 2)                                AS recent_temp,
       round(b.mean_temp::numeric, 2)                                  AS baseline_temp,
       round((r.recent_temp - b.mean_temp)::numeric, 2)                AS rise_k,
       round(((r.recent_temp - b.mean_temp) / b.sd_temp)::numeric, 1)  AS z_score
FROM recent r
JOIN base   b USING (asset_id)
JOIN assets a USING (asset_id)
WHERE (r.recent_temp - b.mean_temp) / b.sd_temp > 3     -- unusual for THIS machine
  AND (r.recent_temp - b.mean_temp)            > 1.0    -- and big enough to matter
ORDER BY z_score DESC;


-- -----------------------------------------------------------------------------
-- B) GRAPH LAYER  |  Failure cascade
--
-- "Machine :asset just failed. What else is affected?"
--
-- A recursive CTE walks asset_dependencies upward. depth is how many hops away
-- the impact is, so the operator sees the blast radius ordered by closeness.
--
-- The depth < 5 guard is not cosmetic: a mis-entered dependency pair can form a
-- cycle, and without the bound the query never terminates. The visited-set
-- approach (tracking a path array) is the fuller fix; the depth cap is the one
-- that fits on a slide.
-- -----------------------------------------------------------------------------
WITH RECURSIVE impacted AS (
  SELECT d.dependent_id AS asset_id,
         1              AS depth,
         d.dependency_type
  FROM asset_dependencies d
  WHERE d.asset_id = :asset

  UNION ALL

  SELECT d.dependent_id,
         i.depth + 1,
         d.dependency_type
  FROM asset_dependencies d
  JOIN impacted i ON d.asset_id = i.asset_id
  WHERE i.depth < 5
)
SELECT a.name,
       a.asset_type,
       i.depth,
       i.dependency_type
FROM impacted i
JOIN assets a USING (asset_id)
ORDER BY i.depth, a.name;


-- -----------------------------------------------------------------------------
-- C) VECTOR LAYER  |  Similar past failures
--
-- "Here is what the operator is seeing. When did this happen before, and what
--  fixed it?"
--
-- <=> is pgvector's cosine distance. Because the HNSW index is built on
-- vector_cosine_ops, ORDER BY embedding <=> :qvec LIMIT 3 is an index scan, not
-- a scan of every report.
--
-- Cosine distance is in [0,2]; 1 - distance is reported as similarity because
-- that is the number an operator can read.
--
-- :qvec is the embedding of the symptom text, produced by the same model that
-- embedded the stored reports. Mixing models here silently returns nonsense:
-- the vectors are valid, the neighbourhood is meaningless.
-- -----------------------------------------------------------------------------
SELECT f.failure_type,
       a.name AS failed_asset,
       f.event_time,
       r.summary,
       r.action_taken,
       round((1 - (r.embedding <=> :qvec))::numeric, 3) AS similarity
FROM maintenance_reports r
JOIN failure_events f USING (event_id)
JOIN assets a         ON a.asset_id = f.asset_id
ORDER BY r.embedding <=> :qvec
LIMIT 3;
