"""
Anchor everything that has settled onto the ledger.

    python scripts/anchor.py

Sensor windows (a completed UTC day for each fixed unit, a completed trip for each
reefer) and shipment hand-offs are written to the chain once they have been closed
for SETTLE_HOURS. Safe to run repeatedly; already-anchored items are skipped.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chain  # noqa: E402

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")


def main() -> int:
    with psycopg.connect(DB) as conn, conn.cursor() as cur:
        info, ms = chain.timed(chain.anchor_pending, cur)
        conn.commit()
        cp = chain.save_checkpoint(cur)
    print(f"anchored {info['windows']} sensor windows and {info['shipments']} hand-offs "
          f"in {info['blocks']} new blocks ({ms:.0f} ms)")
    print(f"checkpoint saved: height {cp['height']}, {cp['block_hash'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
