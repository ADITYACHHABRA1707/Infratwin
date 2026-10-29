"""
InfraTwin benchmark: un-optimised baseline vs the optimised structures.

Baseline   sensor_readings_plain -- one heap, no partitioning, no indexes
Optimised  sensor_readings       -- partitioned (or hypertable) + composite index
                                    + readings_hourly roll-up
Vector     report_bench          -- exact scan vs HNSW index

Writes benchmark_results.csv, which app.py reads for the evidence tab.

Run after sql/03_optimise_*.sql:
    python benchmark.py

Environment:
    RUNS         timed repetitions per query (default 7, median reported)
    WARMUP       untimed repetitions first  (default 2)
    DATABASE_URL connection string
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

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5433/postgres")
RUNS = int(os.getenv("RUNS", "7"))
WARMUP = int(os.getenv("WARMUP", "2"))


def main() -> int:
    conn = psycopg.connect(DB, autocommit=True)
    register_vector(conn)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM pg_extension WHERE extname='timescaledb'")
    engine = "timescaledb" if cur.fetchone()[0] else "postgres (native partitioning)"

    for rel in ["sensor_readings", "sensor_readings_plain", "readings_hourly", "report_bench"]:
        cur.execute("SELECT to_regclass(%s)", (rel,))
        if cur.fetchone()[0] is None:
            print(f"missing relation: {rel}")
            print("Run seed.py and then sql/03_optimise_*.sql first.")
            return 1

    cur.execute("SELECT count(*) FROM sensor_readings")
    n_rows = cur.fetchone()[0]
    print(f"engine: {engine}")
    print(f"sensor_readings: {n_rows:,} rows | {RUNS} timed runs after {WARMUP} warmup\n")

    def ms(sql: str, params=None) -> float:
        """Median wall time in ms. Warmup runs are discarded so the number
        reflects steady state rather than a cold buffer cache."""
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
        # Guard the ratio: a sub-millisecond optimised query would otherwise
        # divide by ~0 and print a meaningless speedup.
        speedup = base / opt if opt > 0.01 else float("nan")
        results.append((name, round(base, 1), round(opt, 2), round(speedup, 1)))
        print(f"  {name}\n      baseline {base:9.1f} ms   optimised {opt:8.2f} ms   "
              f"{speedup:6.1f}x")

    # 1) Point query: one asset, recent window. The baseline has no index, so it
    #    scans every row; the optimised side prunes partitions and seeks.
    print("1. single asset, last 24 hours")
    q = ("SELECT avg(process_temp) FROM {t} "
         "WHERE asset_id = 50 AND time > now() - interval '24 hours'")
    add("Avg temp, 1 machine, last 24h",
        ms(q.format(t="sensor_readings_plain")),
        ms(q.format(t="sensor_readings")))

    # 2) Wide aggregate: the baseline recomputes from raw rows, the optimised
    #    side reads the pre-aggregated roll-up. This is the roll-up's whole point.
    print("\n2. daily averages, all machines, 30 days")
    add("Daily averages, all machines, 30 days",
        ms("SELECT date_trunc('day', time) d, asset_id, avg(process_temp) "
           "FROM sensor_readings_plain WHERE time > now() - interval '30 days' "
           "GROUP BY 1, 2"),
        ms("SELECT date_trunc('day', bucket) d, asset_id, avg(avg_temp) "
           "FROM readings_hourly WHERE bucket > now() - interval '30 days' "
           "GROUP BY 1, 2"))

    # 3) Narrow time window across all assets: pure partition pruning.
    print("\n3. threshold scan, 1-hour window")
    q3 = ("SELECT count(*) FROM {t} "
          "WHERE time BETWEEN now() - interval '2 hours' AND now() - interval '1 hour' "
          "AND process_temp > 312")
    add("Readings above threshold, 1-hour window",
        ms(q3.format(t="sensor_readings_plain")),
        ms(q3.format(t="sensor_readings")))

    # 4) Vector search: exact scan vs HNSW over the same rows.
    print("\n4. nearest-neighbour search")
    cur.execute("SELECT count(*) FROM report_bench")
    n_vec = cur.fetchone()[0]
    qv = np.random.default_rng(7).standard_normal(384).astype(np.float32)
    qv /= np.linalg.norm(qv)
    vq = "SELECT id FROM report_bench ORDER BY embedding <=> %s LIMIT 3"

    cur.execute("DROP INDEX IF EXISTS idx_bench_hnsw")
    exact = ms(vq, (qv,))
    cur.execute("CREATE INDEX idx_bench_hnsw ON report_bench "
                "USING hnsw (embedding vector_cosine_ops)")
    cur.execute("ANALYZE report_bench")
    add(f"Similar-report search, {n_vec:,} vectors (exact vs HNSW)",
        exact, ms(vq, (qv,)))

    # Storage, reported separately: it is a size, not a duration, so it does not
    # belong in a table of millisecond timings.
    #
    # pg_total_relation_size on a PARTITIONED PARENT returns 0 -- the parent
    # holds no rows, the partitions do. Timescale hypertables hide their chunks
    # the same way. Both need summing over the children.
    def total_bytes(rel: str) -> int:
        cur.execute("SELECT to_regclass(%s)", (rel,))
        if cur.fetchone()[0] is None:
            return 0
        # Timescale hypertable
        try:
            cur.execute("SELECT hypertable_size(%s::regclass)", (rel,))
            v = cur.fetchone()[0]
            if v:
                return int(v)
        except psycopg.Error:
            conn.rollback() if not conn.autocommit else None
        # Native partitioned table (sums parent + every partition)
        try:
            cur.execute(
                "SELECT coalesce(sum(pg_total_relation_size(relid)), 0) "
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

    b_opt, b_base, b_roll = (total_bytes("sensor_readings"),
                             total_bytes("sensor_readings_plain"),
                             total_bytes("readings_hourly"))
    size_opt, size_base, size_rollup = pretty(b_opt), pretty(b_base), pretty(b_roll)

    out = pd.DataFrame(results,
                       columns=["query", "baseline_ms", "optimised_ms", "speedup_x"])
    out.to_csv("benchmark_results.csv", index=False)

    print("\n" + "=" * 78)
    print(out.to_string(index=False))
    print("=" * 78)
    print(f"\nstorage   partitioned+indexed {size_opt}   plain heap {size_base}   "
          f"roll-up {size_rollup}")
    if b_roll and b_opt:
        print(f"          the roll-up answers the 30-day question from "
              f"{size_rollup} instead of {size_opt} "
              f"({b_opt / b_roll:.0f}x less data touched)")
    print(f"rows      {n_rows:,} readings, {n_vec:,} benchmark vectors")
    print("\nwritten: benchmark_results.csv")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
