"""
The showcase queries, in one place so the dashboard, verify.py, the benchmark and
the tests all run exactly the same SQL.

    ANOMALY        which unit is behaving abnormally right now?     time-series
    EXPOSED_LOTS   which lots were inside it while it did?          custody join
    FORWARD        what else is affected downstream?                recursive CTE
    BACKWARD       where did this lot come from?                    recursive CTE
    NAIVE          what would a date-code recall have pulled?       baseline for FORWARD
    SIMILAR        have we seen this before, and what did we do?    pgvector

Named parameters (%(name)s) throughout, except SIMILAR which takes the query vector
twice, positionally.
"""

ANOMALY = """
WITH base AS (
  SELECT unit_id, avg(avg_temp) AS mean_temp, GREATEST(stddev(avg_temp), 0.05) AS sd_temp
  FROM readings_hourly
  WHERE bucket < now() - interval '3 hours'
  GROUP BY unit_id),
recent AS (
  SELECT unit_id, avg(temp_c) AS recent_temp
  FROM sensor_readings
  WHERE time > now() - interval '1 hour'
  GROUP BY unit_id)
SELECT u.unit_id, u.name, u.unit_type, p.name AS operator,
       round(r.recent_temp::numeric, 2)                          AS recent_c,
       round(b.mean_temp::numeric, 2)                            AS baseline_c,
       round((r.recent_temp - b.mean_temp)::numeric, 2)          AS deviation_c,
       round(((r.recent_temp - b.mean_temp) / b.sd_temp)::numeric, 1) AS z_score,
       u.temp_min, u.temp_max
FROM recent r JOIN base b USING (unit_id) JOIN units u USING (unit_id)
JOIN parties p ON p.party_id = u.party_id
WHERE abs(r.recent_temp - b.mean_temp) / b.sd_temp > 3     -- unusual for THIS unit
  AND abs(r.recent_temp - b.mean_temp)            > 1.0    -- and big enough to matter
ORDER BY abs((r.recent_temp - b.mean_temp) / b.sd_temp) DESC"""

TREND = """
SELECT date_trunc('minute', time) AS t, temp_c
FROM sensor_readings
WHERE unit_id = %(unit)s AND time BETWEEN %(start)s AND %(end)s
ORDER BY t"""

# First minute in the last three hours at which the unit was outside its limits.
EXCURSION_START = """
SELECT min(r.time)
FROM sensor_readings r JOIN units u USING (unit_id)
WHERE r.unit_id = %(unit)s AND r.time > now() - interval '3 hours'
  AND (r.temp_c > u.temp_max OR r.temp_c < u.temp_min)"""

# Out-of-spec episodes in the last 72 hours, live or finished.
EXCURSION_LOG = """
SELECT u.unit_id, u.name, u.unit_type, p.name AS operator,
       min(r.time) AS first_breach, max(r.time) AS last_breach,
       count(*)    AS minutes_out,
       round(max(r.temp_c)::numeric, 1) AS peak_c,
       round(min(r.temp_c)::numeric, 1) AS low_c,
       u.temp_min, u.temp_max
FROM sensor_readings r JOIN units u USING (unit_id) JOIN parties p ON p.party_id = u.party_id
WHERE r.time > now() - interval '72 hours'
  AND (r.temp_c > u.temp_max OR r.temp_c < u.temp_min)
GROUP BY u.unit_id, u.name, u.unit_type, p.name, u.temp_min, u.temp_max
HAVING count(*) >= 5
ORDER BY max(r.time) DESC"""

# Lots whose custody overlapped the excursion. This single join is what ties the
# sensor world to the lot world.
EXPOSED_LOTS = """
SELECT l.lot_id, l.lot_code, l.lot_type, pr.name AS product, l.quantity, l.status,
       greatest(c.from_time, %(since)s) AS exposed_from,
       round((extract(epoch FROM (least(coalesce(c.to_time, now()), %(until)s)
                                   - greatest(c.from_time, %(since)s))) / 60)::numeric) AS exposed_min
FROM custody c JOIN lots l USING (lot_id) JOIN products pr USING (product_id)
WHERE c.unit_id = %(unit)s
  AND c.from_time <= %(until)s
  AND coalesce(c.to_time, now()) >= %(since)s
ORDER BY l.lot_type, l.lot_code"""

# Walks the genealogy graph forward. share is the fraction of each downstream lot
# that traces to the seed lots: a batch that is 40% from a bad raw lot is 0.4.
# `since` (nullable) only follows hand-offs created at or after that moment, so a
# sensor excursion at 17:00 does not condemn a batch made at 08:00.
FORWARD = """
WITH RECURSIVE hit AS (
  SELECT l.lot_id, 0 AS depth, 1.0::float8 AS share, ARRAY[l.lot_id] AS path
  FROM lots l WHERE l.lot_id = ANY(%(seeds)s)
  UNION ALL
  SELECT f.to_lot, h.depth + 1, h.share * f.share, h.path || f.to_lot
  FROM hit h
  JOIN lot_flow_share f ON f.from_lot = h.lot_id
  JOIN lots t ON t.lot_id = f.to_lot
  WHERE h.depth < 8
    AND NOT f.to_lot = ANY(h.path)
    AND (%(since)s::timestamptz IS NULL OR t.created_at >= %(since)s))
SELECT l.lot_id, l.lot_type, l.lot_code, pr.name AS product,
       least(1.0, sum(h.share))                     AS share,
       min(h.depth)                                 AS depth,
       pa.name                                      AS holder,
       l.quantity, l.status, l.created_at,
       EXISTS (SELECT 1 FROM custody c WHERE c.lot_id = l.lot_id AND c.to_time IS NULL) AS in_chain,
       (SELECT u.name FROM custody c JOIN units u USING (unit_id)
         WHERE c.lot_id = l.lot_id AND c.to_time IS NULL
         ORDER BY c.from_time DESC LIMIT 1)         AS location
FROM hit h JOIN lots l USING (lot_id) JOIN products pr USING (product_id)
JOIN parties pa ON pa.party_id = l.party_id
GROUP BY l.lot_id, l.lot_type, l.lot_code, pr.name, pa.name, l.quantity, l.status, l.created_at
ORDER BY min(h.depth), l.lot_type, l.lot_code"""

# Provenance: walk backwards. share is the fraction of the starting lot that came
# from each ancestor.
BACKWARD = """
WITH RECURSIVE up AS (
  SELECT l.lot_id, 0 AS depth, 1.0::float8 AS share, ARRAY[l.lot_id] AS path
  FROM lots l WHERE l.lot_id = %(lot)s
  UNION ALL
  SELECT f.from_lot, u.depth + 1, u.share * f.share, u.path || f.from_lot
  FROM up u JOIN lot_flow_share f ON f.to_lot = u.lot_id
  WHERE u.depth < 8 AND NOT f.from_lot = ANY(u.path))
SELECT l.lot_id, l.lot_type, l.lot_code, pr.name AS product, pa.name AS holder,
       min(u.depth) AS hops_back, sum(u.share) AS share_of_lot, l.created_at
FROM up u JOIN lots l USING (lot_id) JOIN products pr USING (product_id)
JOIN parties pa ON pa.party_id = l.party_id
GROUP BY l.lot_id, l.lot_type, l.lot_code, pr.name, pa.name, l.created_at
ORDER BY min(u.depth), l.created_at"""

# The scope you are forced into when lot genealogy is not available: you cannot tell
# which batches consumed the bad lot, so the only safe recall is everything produced
# since it arrived. Used only as the comparison for FORWARD.
NAIVE = """
WITH RECURSIVE up AS (
  SELECT lot_id, lot_id AS anc, 0 AS d FROM lots WHERE lot_type IN ('FINISHED','STOCK')
  UNION ALL
  SELECT up.lot_id, f.from_lot, up.d + 1 FROM up JOIN lot_flows f ON f.to_lot = up.anc WHERE up.d < 6),
origin AS (
  SELECT up.lot_id, b.created_at AS batch_made
  FROM up JOIN lots b ON b.lot_id = up.anc WHERE b.lot_type = 'BATCH')
SELECT l.lot_id, l.lot_type, l.quantity,
       EXISTS (SELECT 1 FROM custody c WHERE c.lot_id = l.lot_id AND c.to_time IS NULL) AS in_chain
FROM origin o JOIN lots l USING (lot_id)
WHERE o.batch_made >= (SELECT min(created_at) FROM lots WHERE lot_id = ANY(%(seeds)s))"""

SIMILAR = """
SELECT i.kind, coalesce(u.name, l.lot_code) AS subject, i.occurred_at,
       r.summary, r.action_taken, r.outcome,
       round((1 - (r.embedding <=> %s))::numeric, 3) AS similarity
FROM incident_reports r
JOIN incidents i USING (incident_id)
LEFT JOIN units u ON u.unit_id = i.unit_id
LEFT JOIN lots  l ON l.lot_id  = i.lot_id
ORDER BY r.embedding <=> %s
LIMIT 3"""

# Where a lot (and everything it was made from) has been, and what the thermometer
# said while it was there.
JOURNEY = """
SELECT l.lot_code, u.name AS unit, pa.name AS operator, c.from_time, c.to_time,
       m.n AS readings, m.lo AS min_c, m.hi AS max_c,
       (m.hi > u.temp_max OR m.lo < u.temp_min) AS out_of_spec
FROM custody c JOIN lots l USING (lot_id) JOIN units u USING (unit_id)
JOIN parties pa ON pa.party_id = u.party_id
CROSS JOIN LATERAL (
  SELECT count(*) AS n, min(r.temp_c) AS lo, max(r.temp_c) AS hi
  FROM sensor_readings r
  WHERE r.unit_id = c.unit_id AND r.time >= c.from_time
    AND r.time < coalesce(c.to_time, now())) m
WHERE c.lot_id = ANY(%(lots)s)
ORDER BY c.from_time, l.lot_code"""

LOT_LEDGER = """
SELECT t.height, t.tx_type, t.subject, t.tx_hash, b.block_hash
FROM ledger_tx t JOIN blocks b USING (height)
WHERE t.subject = %(code)s
   OR t.subject IN (SELECT s.shipment_code FROM custody c JOIN shipments s USING (shipment_id)
                    JOIN lots l USING (lot_id) WHERE l.lot_code = %(code)s)
ORDER BY t.height, t.tx_index"""


def exposure_start(cur, unit_id: int):
    """When did this unit first leave its limits (last three hours)? Falls back to
    one hour ago if it never did (a deviation that stayed inside the spec window)."""
    cur.execute(EXCURSION_START, {"unit": unit_id})
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    cur.execute("SELECT now() - interval '1 hour'")
    return cur.fetchone()[0]
