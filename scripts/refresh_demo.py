"""
Re-arm the live demo anomalies against the current clock.

    python scripts/refresh_demo.py        (or: python run.py demo)

Why this exists: the dashboard compares "the last hour" against "everything older
than three hours", both relative to now(). seed.py injects the anomalies into the
last 90 minutes, so as the clock advances those rows drift into the baseline they
are being measured against and the z-scores decay. This script:

  1. reverts the previous injection exactly (offsets are stored in demo_injection),
  2. tops the fixed units up with readings to the current minute,
  3. injects a fresh +/- offset into the last 90 minutes,
  4. refreshes the hourly roll-up.

Only fixed units (cold rooms, display cases, pasteuriser lines) are re-armed. A
reefer's readings exist only while its trip does, so the in-transit truck anomaly
seeded at load time cannot be re-created later.

Ledger safety: rows inside an already-anchored telemetry window are never touched,
because changing them would (correctly) fail ledger verification. If part of the old
injection has been anchored, it stays in history as a real, recorded event.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")
WINDOW = timedelta(minutes=90)
RAMP_SECONDS = 1800.0

# unit name -> offset in degrees C
PLAN = {"Plant RM Cold Store": 4.5, "Pasteuriser Line B": -3.0, "FreshMart T Nagar Display": 6.0}


def shift(cur, unit_id: int, t0: datetime, t1: datetime, delta: float) -> int:
    """Add delta (ramped in over 30 minutes from t0) to a unit's readings in [t0, t1]."""
    cur.execute(
        """UPDATE sensor_readings
              SET temp_c = round((temp_c + %s * least(1.0, extract(epoch FROM (time - %s)) / %s))::numeric, 2)::real
            WHERE unit_id = %s AND time >= %s AND time <= %s""",
        (delta, t0, RAMP_SECONDS, unit_id, t0, t1))
    return cur.rowcount


def inject_anomalies(cur, plan: dict[int, float], now: datetime) -> list[str]:
    """Inject {unit_id: offset} into the last 90 minutes and record it for later undo."""
    t0 = now - WINDOW
    cur.execute("SELECT unit_id, name FROM units")
    name_of = dict(cur.fetchall())
    for unit_id, delta in plan.items():
        shift(cur, unit_id, t0, now, delta)
        cur.execute("INSERT INTO demo_injection(unit_id, from_time, to_time, delta) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id) DO UPDATE "
                    "SET from_time = EXCLUDED.from_time, to_time = EXCLUDED.to_time, delta = EXCLUDED.delta",
                    (unit_id, t0, now, delta))
    return [f"{name_of[u]} ({d:+.1f} C)" for u, d in plan.items()]


def revert(cur) -> int:
    """Undo the previous injection on unanchored rows only. Returns rows reverted."""
    cur.execute("SELECT unit_id, from_time, to_time, delta FROM demo_injection")
    total = 0
    for unit_id, t0, t1, delta in cur.fetchall():
        cur.execute("SELECT max(window_end) FROM telemetry_windows WHERE unit_id = %s", (unit_id,))
        anchored_to = cur.fetchone()[0]
        start = t0 if anchored_to is None or anchored_to <= t0 else anchored_to
        if start > t1:
            continue
        # The ramp is a function of time since t0, so revert with the same t0.
        cur.execute(
            """UPDATE sensor_readings
                  SET temp_c = round((temp_c - %s * least(1.0, extract(epoch FROM (time - %s)) / %s))::numeric, 2)::real
                WHERE unit_id = %s AND time >= %s AND time <= %s""",
            (delta, t0, RAMP_SECONDS, unit_id, start, t1))
        total += cur.rowcount
    cur.execute("DELETE FROM demo_injection")
    return total


def top_up(cur, now: datetime) -> int:
    """Extend fixed units' readings to `now` using the same shape seed.py uses."""
    cur.execute("""
        INSERT INTO sensor_readings (time, unit_id, temp_c, humidity)
        SELECT t, u.unit_id,
               round((u.setpoint
                 + CASE u.unit_type WHEN 'DisplayCase' THEN 0.40 WHEN 'ColdRoom' THEN 0.20 ELSE 0 END
                   * sin(extract(epoch FROM t) / 86400 * 2 * pi())
                 + CASE u.unit_type WHEN 'ColdRoom' THEN 0.25 WHEN 'DisplayCase' THEN 0.30 ELSE 0.15 END
                   * (random() + random() + random() - 1.5) * 2)::numeric, 2)::real,
               round((62 + random() * 14)::numeric, 1)::real
        FROM units u
        CROSS JOIN LATERAL (SELECT max(time) AS last FROM sensor_readings r WHERE r.unit_id = u.unit_id) l
        CROSS JOIN LATERAL generate_series(l.last + interval '1 minute', %s::timestamptz, interval '1 minute') t
        WHERE u.unit_type <> 'Reefer'""", (now,))
    return cur.rowcount


def main() -> int:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        reverted = revert(cur)
        added = top_up(cur, now)
        cur.execute("SELECT unit_id, name FROM units WHERE name = ANY(%s)", (list(PLAN),))
        plan = {uid: PLAN[name] for uid, name in cur.fetchall()}
        injected = inject_anomalies(cur, plan, now)
        cur.execute("REFRESH MATERIALIZED VIEW readings_hourly")
        conn.commit()
    print(f"reverted {reverted} rows, added {added} readings up to {now:%H:%MZ}")
    print("armed: " + ", ".join(injected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
