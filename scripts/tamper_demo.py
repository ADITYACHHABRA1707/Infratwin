"""
Tamper with the data on purpose and show the ledger catching it.

    python scripts/tamper_demo.py

Each scenario is rolled back, so nothing is left altered.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tamper  # noqa: E402

import settings  # noqa: E402  (loads .env; no secrets in source)
DB = settings.database_url()


def main() -> int:
    conn = psycopg.connect(DB, autocommit=True)
    base = tamper.baseline(conn)
    print("untouched database:", "VERIFIED" if base["ok"] else "PROBLEMS FOUND")
    if not base["ok"]:
        print(tamper.summarise(base["problems"], 5))
        print("\nfix the baseline before running tamper scenarios")
        return 1
    caught = 0
    for key, (title, _) in tamper.SCENARIOS.items():
        r = tamper.run(conn, key)
        caught += r["detected"]
        print(f"\n--- {title}")
        print(f"    attacker: {r['what']}")
        print(f"    result  : {'CAUGHT' if r['detected'] else 'MISSED'}  "
              f"({len(r['problems'])} findings, {r['ms']:.0f} ms)")
        print(f"    first   : {tamper.summarise(r['problems'], 2)}")
    print(f"\n{caught}/{len(tamper.SCENARIOS)} tampering attempts detected; database unchanged.")
    return 0 if caught == len(tamper.SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
