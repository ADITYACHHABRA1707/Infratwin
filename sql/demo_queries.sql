-- =============================================================================
-- ChainTwin | demo_queries.sql
-- Joins, aggregates, views, transactions, the stored procedure, triggers,
-- constraints and index plans, each runnable on its own. The application itself
-- reaches the database through the ORM; this file exists to show the SQL features.
--
--   docker exec -i chaintwin-db psql -U postgres -a -f - < sql/demo_queries.sql
-- Everything that changes data runs inside a transaction that is rolled back.
-- =============================================================================
\pset pager off
\set VERBOSITY terse

\echo '=== 1. JOIN: lot -> product -> holder (3 tables) ==='
SELECT l.lot_code, pr.name AS product, pa.name AS holder, l.quantity, l.status
FROM lots l
JOIN products pr ON pr.product_id = l.product_id
JOIN parties  pa ON pa.party_id   = l.party_id
WHERE l.lot_type = 'STOCK'
ORDER BY l.created_at DESC LIMIT 5;

\echo '=== 2. JOIN: a shipment with origin, destination, carrier and vehicle (view over 5 tables) ==='
SELECT shipment_code, origin, destination, carrier, vehicle, hours, lots_carried
FROM v_shipment_overview ORDER BY depart_time DESC LIMIT 5;

\echo '=== 3. AGGREGATE: lots and quantity per organisation type (GROUP BY + HAVING) ==='
SELECT pa.party_type, count(*) AS lots, round(sum(l.quantity)) AS total_qty,
       count(*) FILTER (WHERE l.status <> 'OK') AS not_ok
FROM lots l JOIN parties pa ON pa.party_id = l.party_id
GROUP BY pa.party_type HAVING count(*) > 10
ORDER BY lots DESC;

\echo '=== 4. AGGREGATE: out-of-spec minutes per unit, worst five (time series, partition pruned) ==='
SELECT u.name, count(*) AS minutes_out, round(max(r.temp_c)::numeric, 1) AS peak_c, u.temp_max
FROM sensor_readings r JOIN units u USING (unit_id)
WHERE r.time > now() - interval '72 hours' AND (r.temp_c > u.temp_max OR r.temp_c < u.temp_min)
GROUP BY u.unit_id ORDER BY minutes_out DESC LIMIT 5;

\echo '=== 5. VIEWS ==='
SELECT * FROM v_lot_status_summary ORDER BY lot_type, status;
SELECT party, party_type, lots, on_hold, recalled FROM v_party_inventory ORDER BY lots DESC LIMIT 5;
SELECT from_lot, to_lot, flow_type, round(share::numeric, 3) AS share FROM lot_flow_share WHERE flow_type = 'consumed_in' LIMIT 5;

\echo '=== 6. RECURSIVE CTE: everything downstream of the failed raw-milk lot, with exposure share ==='
WITH RECURSIVE hit AS (
  SELECT lot_id, 0 AS depth, 1.0::float8 AS share, ARRAY[lot_id] AS path
  FROM lots WHERE lot_code = 'RM-261026-02'
  UNION ALL
  SELECT f.to_lot, h.depth + 1, h.share * f.share, h.path || f.to_lot
  FROM hit h JOIN lot_flow_share f ON f.from_lot = h.lot_id
  WHERE h.depth < 8 AND NOT f.to_lot = ANY(h.path))
SELECT l.lot_type, count(DISTINCT l.lot_id) AS lots, max(h.depth) AS max_depth
FROM hit h JOIN lots l USING (lot_id) GROUP BY l.lot_type ORDER BY max(h.depth);

\echo '=== 7. TRANSACTION: hold a lot, observe the audit trigger, roll back ==='
BEGIN;
UPDATE lots SET status = 'HOLD' WHERE lot_code = 'FG-261030-11';
SELECT entity, action, details FROM audit_logs ORDER BY log_id DESC LIMIT 1;
ROLLBACK;
SELECT lot_code, status AS status_after_rollback FROM lots WHERE lot_code = 'FG-261030-11';

\echo '=== 8. STORED PROCEDURE: sp_change_user_role (rolled back) ==='
BEGIN;
CALL sp_change_user_role(1, 3, 'MANAGER');        -- actor 1 is ADMIN: allowed
SELECT u.email, r.role_name FROM users u JOIN roles r USING (role_id) ORDER BY user_id;
SELECT entity, action, details FROM audit_logs WHERE entity = 'users' ORDER BY log_id DESC LIMIT 1;
ROLLBACK;
\echo '--- the same call made by a MANAGER (user 2) is refused inside the database:'
CALL sp_change_user_role(2, 3, 'ADMIN');
\echo '--- unknown role:'
CALL sp_change_user_role(1, 3, 'ROOT');

\echo '=== 9. TRIGGERS ==='
\echo '--- last active ADMIN cannot be disabled:'
UPDATE users SET is_active = FALSE WHERE user_id = 1;
\echo '--- last active ADMIN cannot be deleted:'
DELETE FROM users WHERE user_id = 1;
\echo '--- the ledger is append-only:'
UPDATE blocks SET nonce = 0 WHERE height = 0;
DELETE FROM ledger_tx WHERE tx_id = 1;

\echo '=== 10. CONSTRAINTS rejecting bad data ==='
INSERT INTO lots(lot_code, product_id, party_id, lot_type, quantity, created_at) VALUES ('X-1', 1, 1, 'RAW', -5, now());
INSERT INTO lots(lot_code, product_id, party_id, lot_type, quantity, created_at) VALUES ('X-2', 1, 1, 'MAGIC', 5, now());
INSERT INTO lots(lot_code, product_id, party_id, lot_type, quantity, created_at) VALUES ('X-3', 999999, 1, 'RAW', 5, now());
INSERT INTO lots(lot_code, product_id, party_id, lot_type, quantity, created_at) VALUES ('RM-261026-02', 1, 1, 'RAW', 5, now());
INSERT INTO lots(lot_code, product_id, party_id, lot_type, quantity) VALUES ('X-5', 1, 1, 'RAW', 5);
INSERT INTO users(email, full_name, password_hash, role_id) VALUES ('not-an-email', 'x', 'h', 1);
INSERT INTO shipments(shipment_code, origin_party, dest_party, carrier_party, unit_id, depart_time, arrive_time)
VALUES ('S-X', 1, 2, 3, 1, now(), now() - interval '1 hour');

\echo '=== 11. INDEXES: plans (EXPLAIN ANALYZE) ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT avg(temp_c) FROM sensor_readings
WHERE unit_id = 1 AND time > now() - interval '24 hours';
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT lot_id FROM custody WHERE unit_id = 1 AND from_time < now() AND (to_time IS NULL OR to_time > now() - interval '1 hour');
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
SELECT lot_code FROM lots WHERE lot_type = 'STOCK' ORDER BY created_at DESC LIMIT 20;

\echo '=== 12. Index inventory ==='
SELECT tablename, indexname FROM pg_indexes
WHERE schemaname = 'public' AND tablename !~ '^sensor_readings_p' AND indexname NOT LIKE '%pkey'
ORDER BY tablename, indexname;
