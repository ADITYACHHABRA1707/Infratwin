"""
Re-arm the demo without a full re-seed.

Why this exists: seed.py injects the anomaly into "the last 3 hours", but the
dashboard query compares "the last hour" against "everything older than 3
hours", both relative to now(). As the clock advances, the injected rows drift
out of the detection window and into the baseline they are measured against, so
the z-scores decay and eventually the anomaly disappears.

Run this right before presenting. It takes a couple of seconds:

    python scripts/refresh_demo.py

It does three things:
  1. clears any previous injection so anomalies do not stack
  2. re-injects the anomaly relative to the current time
  3. refreshes the hourly roll-up so the baseline is current
"""
from __future__ import annotations

import os
import random
import sys

import psycopg

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5433/postgres")
N_DEMO = int(os.getenv("DEMO_ASSETS", "3"))

TEMP_RISE = 9.0
TORQUE_RISE = 18.0
RPM_DROP = 150


def main() -> int:
    conn = psycopg.connect(DB, autocommit=False)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM pg_extension WHERE extname='timescaledb'")
    has_ts = bool(cur.fetchone()[0])

    cur.execute("SELECT to_regclass('sensor_readings')")
    if cur.fetchone()[0] is None:
        print("sensor_readings does not exist. Run the sql files and seed.py first.")
        return 1

    # A tiny bookkeeping table recording what was raised and from when, so this
    # script can undo its own previous run exactly.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS demo_injection (
          asset_id  INT         NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
          from_time TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (asset_id)
        )""")

    # 1) Undo any earlier injection. Without this, re-running stacks another
    #    +9 K on assets that were already raised, and the contaminated rows stay
    #    in the baseline window.
    print("1/3 clearing previous injection ...")
    cur.execute(
        """UPDATE sensor_readings s
              SET process_temp = process_temp - %s,
                  torque       = torque       - %s,
                  rpm          = rpm          + %s
             FROM demo_injection d
            WHERE s.asset_id = d.asset_id
              AND s.time >= d.from_time""",
        (TEMP_RISE, TORQUE_RISE, RPM_DROP),
    )
    print(f"    reverted {max(cur.rowcount, 0):,} rows")
    cur.execute("DELETE FROM demo_injection")

    # 2) Inject relative to the current clock.
    print("2/3 injecting anomaly at the current time ...")
    cur.execute("SELECT asset_id FROM assets WHERE asset_type = 'Motor' ORDER BY asset_id")
    motors = [r[0] for r in cur.fetchall()]
    if len(motors) < N_DEMO:
        print(f"only {len(motors)} motors available")
        return 1

    demo = random.Random().sample(motors, N_DEMO)
    cur.execute(
        """UPDATE sensor_readings
              SET process_temp = process_temp + %s,
                  torque       = torque       + %s,
                  rpm          = rpm          - %s
            WHERE asset_id = ANY(%s)
              AND time > now() - interval '90 minutes'""",
        (TEMP_RISE, TORQUE_RISE, RPM_DROP, demo),
    )
    n = cur.rowcount
    cur.executemany(
        "INSERT INTO demo_injection(asset_id, from_time) "
        "VALUES (%s, now() - interval '90 minutes')",
        [(a,) for a in demo],
    )
    cur.execute("SELECT name FROM assets WHERE asset_id = ANY(%s) ORDER BY name", (demo,))
    names = [r[0] for r in cur.fetchall()]
    print(f"    raised {n:,} rows on {', '.join(names)}")
    conn.commit()

    # 3) Refresh the roll-up so the baseline reflects reality.
    print("3/3 refreshing the hourly roll-up ...")
    conn.autocommit = True
    if has_ts:
        cur.execute("CALL refresh_continuous_aggregate('readings_hourly', NULL, NULL)")
        print("    continuous aggregate refreshed")
    else:
        try:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY readings_hourly")
        except psycopg.Error:
            cur.execute("REFRESH MATERIALIZED VIEW readings_hourly")
        print("    materialized view refreshed")

    cur.execute("ANALYZE sensor_readings")

    # Confirm the demo will actually show something.
    cur.execute("""
        WITH base AS (
          SELECT asset_id, avg(avg_temp) m, GREATEST(stddev(avg_temp),0.05) s
          FROM readings_hourly WHERE bucket < now() - interval '3 hours' GROUP BY asset_id),
        recent AS (
          SELECT asset_id, avg(process_temp) r FROM sensor_readings
          WHERE time > now() - interval '1 hour' GROUP BY asset_id)
        SELECT a.name, round(((r.r-b.m)/b.s)::numeric,1)
        FROM recent r JOIN base b USING (asset_id) JOIN assets a USING (asset_id)
        WHERE (r.r-b.m)/b.s > 3 AND (r.r-b.m) > 1.0
        ORDER BY 2 DESC""")
    found = cur.fetchall()

    print(f"\nthe dashboard will now show {len(found)} anomalies:")
    for name, z in found:
        print(f"    {name}  z={z}")
    if len(found) != N_DEMO:
        print(f"\n  note: expected {N_DEMO}. If this is 0, the sensor stream may not")
        print("  reach the present -- re-run seed.py.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
