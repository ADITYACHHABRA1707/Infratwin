"""
InfraTwin self-check.

Runs every claim the demo makes and prints pass/fail. Use it before presenting:
if this is green, the demo will work.

    python verify.py
"""
from __future__ import annotations

import os
import sys

import psycopg
from pgvector.psycopg import register_vector

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5433/postgres")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok ", FAIL: "FAIL ", WARN: "warn "}[status]
    print(f"{mark} {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    try:
        conn = psycopg.connect(DB, autocommit=True)
        register_vector(conn)
    except Exception as exc:
        print(f"FAIL  cannot connect to {DB.rsplit('@', 1)[-1]}: {exc}")
        print("      start the database (docker compose up -d) and re-run")
        return 1
    cur = conn.cursor()

    print(f"checking {DB.rsplit('@', 1)[-1]}\n")

    # -- schema objects ----------------------------------------------------
    for rel in ["assets", "asset_dependencies", "sensor_readings",
                "failure_events", "maintenance_reports", "report_bench",
                "audit_logs", "readings_hourly", "sensor_readings_plain"]:
        cur.execute("SELECT to_regclass(%s)", (rel,))
        exists = cur.fetchone()[0] is not None
        check(f"relation {rel}", PASS if exists else FAIL,
              "" if exists else "missing -- run the sql files and seed.py")

    cur.execute("SELECT count(*) FROM pg_extension WHERE extname='vector'")
    check("extension pgvector", PASS if cur.fetchone()[0] else FAIL)

    cur.execute("SELECT count(*) FROM pg_extension WHERE extname='timescaledb'")
    has_ts = bool(cur.fetchone()[0])
    check("time-series engine", PASS,
          "TimescaleDB hypertable" if has_ts else "PostgreSQL native partitioning")

    # -- row counts --------------------------------------------------------
    counts = {}
    for rel in ["assets", "asset_dependencies", "sensor_readings",
                "failure_events", "maintenance_reports", "report_bench"]:
        cur.execute(f"SELECT count(*) FROM {rel}")
        counts[rel] = cur.fetchone()[0]

    check("assets loaded", PASS if counts["assets"] == 101 else WARN,
          f"{counts['assets']} rows (expected 101)")
    check("dependency edges", PASS if counts["asset_dependencies"] == 100 else WARN,
          f"{counts['asset_dependencies']} rows (expected 100)")
    check("sensor readings", PASS if counts["sensor_readings"] > 0 else FAIL,
          f"{counts['sensor_readings']:,} rows")
    check("failure events", PASS if counts["failure_events"] > 0 else FAIL,
          f"{counts['failure_events']:,} rows")
    check("reports embedded", PASS if counts["maintenance_reports"] > 0 else FAIL,
          f"{counts['maintenance_reports']:,} rows")

    # -- referential integrity --------------------------------------------
    cur.execute("""
        SELECT count(*) FROM sensor_readings s
        LEFT JOIN assets a USING (asset_id) WHERE a.asset_id IS NULL""")
    orphans = cur.fetchone()[0]
    check("no orphan readings", PASS if orphans == 0 else FAIL, f"{orphans} orphans")

    cur.execute("SELECT count(*) FROM maintenance_reports WHERE embedding IS NULL")
    nulls = cur.fetchone()[0]
    check("every report has an embedding", PASS if nulls == 0 else FAIL,
          f"{nulls} missing")

    # -- constraints actually bite ----------------------------------------
    cur.execute("SAVEPOINT sp") if not conn.autocommit else None
    try:
        cur.execute("INSERT INTO assets(name, asset_type) VALUES ('__bad__', 'Spaceship')")
        check("CHECK on asset_type", FAIL, "invalid value was accepted")
        cur.execute("DELETE FROM assets WHERE name='__bad__'")
    except psycopg.errors.CheckViolation:
        check("CHECK on asset_type", PASS, "invalid value rejected")
    except Exception as exc:
        check("CHECK on asset_type", WARN, type(exc).__name__)

    try:
        cur.execute("INSERT INTO asset_dependencies VALUES (1, 1, 'feeds')")
        check("CHECK no self-dependency", FAIL, "self-edge was accepted")
        cur.execute("DELETE FROM asset_dependencies WHERE asset_id=1 AND dependent_id=1")
    except psycopg.errors.CheckViolation:
        check("CHECK no self-dependency", PASS, "self-edge rejected")
    except Exception as exc:
        check("CHECK no self-dependency", WARN, type(exc).__name__)

    # -- audit trigger -----------------------------------------------------
    cur.execute("SELECT count(*) FROM audit_logs")
    before = cur.fetchone()[0]
    cur.execute("SELECT event_id FROM failure_events ORDER BY event_id LIMIT 1")
    ev = cur.fetchone()
    if ev:
        cur.execute("""UPDATE maintenance_reports SET summary = summary
                       WHERE event_id = %s""", (ev[0],))
        cur.execute("SELECT count(*) FROM audit_logs")
        after = cur.fetchone()[0]
        check("audit trigger fires on UPDATE", PASS if after > before else FAIL,
              f"{after - before} new audit row(s)")

    # -- the three showcase queries ---------------------------------------
    cur.execute("""
        WITH base AS (
          SELECT asset_id, avg(avg_temp) m, GREATEST(stddev(avg_temp),0.05) s
          FROM readings_hourly WHERE bucket < now() - interval '3 hours' GROUP BY asset_id),
        recent AS (
          SELECT asset_id, avg(process_temp) r FROM sensor_readings
          WHERE time > now() - interval '1 hour' GROUP BY asset_id)
        SELECT count(*) FROM recent r JOIN base b USING (asset_id)
        WHERE (r.r - b.m) / b.s > 3 AND (r.r - b.m) > 1.0""")
    n_anom = cur.fetchone()[0]
    check("query A: anomaly detection", PASS if n_anom == 3 else WARN,
          f"{n_anom} anomalies found (3 injected)"
          + ("" if n_anom else " -- run: python scripts/refresh_demo.py"))

    cur.execute("SELECT asset_id FROM assets WHERE asset_type='Motor' ORDER BY asset_id LIMIT 1")
    motor = cur.fetchone()[0]
    cur.execute("""
        WITH RECURSIVE impacted AS (
          SELECT d.dependent_id AS asset_id, 1 AS depth FROM asset_dependencies d
          WHERE d.asset_id = %s
          UNION ALL
          SELECT d.dependent_id, i.depth+1 FROM asset_dependencies d
          JOIN impacted i ON d.asset_id = i.asset_id WHERE i.depth < 5)
        SELECT count(*), max(depth) FROM impacted""", (motor,))
    n_imp, max_depth = cur.fetchone()
    check("query B: failure cascade", PASS if n_imp == 3 else WARN,
          f"{n_imp} impacted, depth {max_depth} (motor -> pump -> line -> plant)")

    cur.execute("SELECT embedding FROM maintenance_reports WHERE embedding IS NOT NULL LIMIT 1")
    probe = cur.fetchone()
    if probe:
        cur.execute("""SELECT count(*) FROM (
                         SELECT report_id FROM maintenance_reports
                         ORDER BY embedding <=> %s LIMIT 3) x""", (probe[0],))
        check("query C: vector search", PASS if cur.fetchone()[0] == 3 else FAIL)

    # -- indexes present ---------------------------------------------------
    for idx in ["idx_readings_asset_time", "idx_reports_hnsw"]:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname = %s", (idx,))
        ok = cur.fetchone()[0] > 0
        check(f"index {idx}", PASS if ok else WARN,
              "" if ok else "run sql/03_optimise_*.sql")

    # -- HNSW is actually used, and where it is not -------------------------
    # Checked on report_bench (20k vectors), not maintenance_reports (339 rows):
    # on a table that small a sequential scan is genuinely cheaper and the
    # planner correctly ignores the index. Reporting it on the small table would
    # claim something untrue.
    cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname='idx_bench_hnsw'")
    if cur.fetchone()[0] and probe:
        cur.execute("SELECT embedding FROM report_bench LIMIT 1")
        bench_probe = cur.fetchone()
        if bench_probe:
            cur.execute("""EXPLAIN SELECT id FROM report_bench
                           ORDER BY embedding <=> %s LIMIT 3""", (bench_probe[0],))
            plan = " ".join(r[0] for r in cur.fetchall())
            uses_index = "idx_bench_hnsw" in plan or "Index Scan" in plan
            check(f"HNSW used on report_bench ({counts['report_bench']:,} vectors)",
                  PASS if uses_index else WARN,
                  "" if uses_index else "planner chose a sequential scan")
    else:
        check("HNSW used on report_bench", WARN, "run benchmark.py to build the index")

    if probe:
        cur.execute("""EXPLAIN SELECT report_id FROM maintenance_reports
                       ORDER BY embedding <=> %s LIMIT 3""", (probe[0],))
        plan = " ".join(r[0] for r in cur.fetchall())
        scan = "sequential scan" if "Seq Scan" in plan else "index scan"
        check(f"maintenance_reports ({counts['maintenance_reports']:,} rows) plan",
              PASS, f"{scan} -- expected at this size, HNSW pays off at volume")

    # -- summary -----------------------------------------------------------
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_warn = sum(1 for _, s, _ in results if s == WARN)
    print(f"\n{len(results)} checks: {len(results) - n_fail - n_warn} passed, "
          f"{n_warn} warnings, {n_fail} failures")
    if n_fail:
        print("Fix the failures above before presenting.")
    else:
        print("Ready to present.")

    conn.close()
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
