"""
ChainTwin benchmark: un-optimised baseline vs the optimised structures.

Baseline   sensor_readings_plain -- one heap, no partitioning, no indexes
Optimised  sensor_readings       -- day partitions + (unit_id, time) index
                                    + readings_hourly roll-up
Vector     report_bench          -- exact scan vs HNSW

Writes benchmark_results.csv, which app.py reads for the Benchmark tab. Two further
numbers are printed but not charted because they have no baseline: the cost of a
forward recall trace and the cost of verifying the ledger.

    python benchmark.py

Environment: RUNS (default 7, median reported), WARMUP (default 2), DATABASE_URL.
"""
from __future__ import annotations

import os
import statistics
import sys
import time

import numpy as np
import pandas as pd
import psycopg
from pgvector.psycopg import register_vector

import chain
from queries import FORWARD

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")
RUNS = int(os.getenv("RUNS", "7"))
WARMUP = int(os.getenv("WARMUP", "2"))


def main() -> int:
    conn = psycopg.connect(DB, autocommit=True)
    register_vector(conn)
    cur = conn.cursor()

    for rel in ["sensor_readings", "sensor_readings_plain", "readings_hourly", "report_bench"]:
        cur.execute("SELECT to_regclass(%s)", (rel,))
        if cur.fetchone()[0] is None:
            print(f"missing relation: {rel}\nRun the seed and optimise steps first.")
            return 1

    cur.execute("SELECT count(*) FROM sensor_readings")
    n_rows = cur.fetchone()[0]
    print(f"sensor_readings: {n_rows:,} rows | {RUNS} timed runs after {WARMUP} warmup\n")

    def ms(sql: str, params=None) -> float:
        """Median wall time in ms. Warmup runs are discarded so the number reflects
        steady state rather than a cold buffer cache."""
        for _ in range(WARMUP):
            cur.execute(sql, params)
            cur.fetchall()
        times = []
        for _ in range(RUNS):
            t0 = time.perf_counter()
            cur.execute(sql, params)
            cur.fetchall()
            times.append((time.perf_counter() - t0) * 1000.0)
        return statistics.median(times)

    results: list[tuple[str, float, float, float]] = []

    def add(name: str, base: float, opt: float) -> None:
        # Guard the ratio: a sub-millisecond query would otherwise divide by ~0.
        speedup = base / opt if opt > 0.01 else float("nan")
        results.append((name, round(base, 1), round(opt, 2), round(speedup, 1)))
        print(f"  {name}\n      baseline {base:9.1f} ms   optimised {opt:8.2f} ms   {speedup:6.1f}x")

    cur.execute("SELECT unit_id FROM units WHERE name = 'Plant RM Cold Store'")
    unit = cur.fetchone()[0]

    print("1. one unit, last 24 hours")
    q1 = "SELECT avg(temp_c) FROM {t} WHERE unit_id = %s AND time > now() - interval '24 hours'"
    add("Avg temp, 1 unit, last 24h",
        ms(q1.format(t="sensor_readings_plain"), (unit,)),
        ms(q1.format(t="sensor_readings"), (unit,)))

    print("\n2. daily averages, all units, 30 days")
    add("Daily averages, all units, 30 days",
        ms("SELECT date_trunc('day', time) d, unit_id, avg(temp_c) FROM sensor_readings_plain "
           "WHERE time > now() - interval '30 days' GROUP BY 1, 2"),
        ms("SELECT date_trunc('day', bucket) d, unit_id, avg(avg_temp) FROM readings_hourly "
           "WHERE bucket > now() - interval '30 days' GROUP BY 1, 2"))

    print("\n3. out-of-spec scan, 1-hour window")
    q3 = ("SELECT count(*) FROM {t} WHERE time BETWEEN now() - interval '2 hours' AND now() - interval '1 hour' "
          "AND temp_c > 8 AND temp_c < 60")
    add("Readings above 8 C, 1-hour window",
        ms(q3.format(t="sensor_readings_plain")), ms(q3.format(t="sensor_readings")))

    print("\n4. nearest-neighbour search")
    cur.execute("SELECT count(*) FROM report_bench")
    n_vec = cur.fetchone()[0]
    qv = np.random.default_rng(7).standard_normal(384).astype(np.float32)
    qv /= np.linalg.norm(qv)
    vq = "SELECT id FROM report_bench ORDER BY embedding <=> %s LIMIT 3"
    try:
        cur.execute("DROP INDEX IF EXISTS idx_bench_hnsw")
        exact = ms(vq, (qv,))
        cur.execute("CREATE INDEX idx_bench_hnsw ON report_bench USING hnsw (embedding vector_cosine_ops)")
        cur.execute("ANALYZE report_bench")
        add(f"Similar-incident search, {n_vec:,} vectors (exact vs HNSW)", exact, ms(vq, (qv,)))
    except psycopg.Error as exc:
        print(f"  skipped: {type(exc).__name__} (pgvector HNSW not available here)")

    # A size, not a duration: reported separately. pg_total_relation_size on a
    # partitioned parent returns 0 (the partitions hold the rows), so sum the tree.
    def total_bytes(rel: str) -> int:
        try:
            cur.execute("SELECT coalesce(sum(pg_total_relation_size(relid)), 0) "
                        "FROM pg_partition_tree(%s::regclass)", (rel,))
            v = cur.fetchone()[0]
            if v:
                return int(v)
        except psycopg.Error:
            pass
        cur.execute("SELECT pg_total_relation_size(%s::regclass)", (rel,))
        return int(cur.fetchone()[0] or 0)

    def pretty(n: int) -> str:
        cur.execute("SELECT pg_size_pretty(%s::bigint)", (n,))
        return cur.fetchone()[0]

    b_opt, b_base, b_roll = (total_bytes("sensor_readings"), total_bytes("sensor_readings_plain"),
                             total_bytes("readings_hourly"))

    out = pd.DataFrame(results, columns=["query", "baseline_ms", "optimised_ms", "speedup_x"])
    out.to_csv("benchmark_results.csv", index=False)
    print("\n" + "=" * 78 + "\n" + out.to_string(index=False) + "\n" + "=" * 78)
    print(f"storage   partitioned+indexed {pretty(b_opt)}   plain heap {pretty(b_base)}   "
          f"roll-up {pretty(b_roll)}")

    # Informational, no baseline.
    cur.execute("SELECT lot_id FROM quality_events WHERE result = 'FAIL' ORDER BY tested_at DESC LIMIT 1")
    seed = cur.fetchone()[0]
    fwd = ms(FORWARD, {"seeds": [seed], "since": None})
    cur.execute("SELECT count(*) FROM lots")
    n_lots = cur.fetchone()[0]
    print(f"\nforward recall trace over {n_lots:,} lots: {fwd:.1f} ms (median)")
    c, c_ms = chain.timed(chain.verify_chain, cur)
    print(f"ledger chain verification: {c['blocks']} blocks / {c['txs']:,} txs in {c_ms:.0f} ms")
    o, o_ms = chain.timed(chain.verify_offchain, cur)
    rate = o["readings_anchored"] / (o_ms / 1000.0)
    print(f"database-vs-ledger verification: {o['readings_anchored']:,} readings re-hashed and "
          f"{o['counts']['lots']:,} lots re-derived in {o_ms / 1000:.1f} s ({rate:,.0f} readings/s)")
    print(f"rows      {n_rows:,} readings, {n_vec:,} benchmark vectors\n\nwritten: benchmark_results.csv")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
