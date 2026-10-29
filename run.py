"""
ChainTwin task runner. Works the same on Windows, macOS and Linux, and needs
neither `make` nor `psql`: SQL files are applied through psycopg.

    python run.py up          start the database container and wait for it
    python run.py schema      create tables (core, time-series, ledger)
    python run.py seed        simulate the supply chain and write the ledger
    python run.py optimise    roll-up, HNSW index, un-optimised baseline
    python run.py anchor      anchor settled sensor windows + hand-offs on the ledger
    python run.py benchmark   baseline vs optimised -> benchmark_results.csv
    python run.py verify      every self-check, including ledger verification
    python run.py demo        re-arm the live anomalies against the current clock
    python run.py tamper      tamper with the data on purpose and show it caught
    python run.py test        pytest suite
    python run.py app         launch the dashboard
    python run.py all         up, schema, seed, optimise, benchmark, verify
    python run.py down        stop the container (data kept)
    python run.py clean       remove the container AND its data volume
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")
os.environ["DATABASE_URL"] = DB
PY = sys.executable


def sh(*cmd: str) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(list(cmd), cwd=ROOT)


def apply_sql(*files: str) -> int:
    with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
        for f in files:
            print(f"  applying sql/{f}")
            cur.execute((ROOT / "sql" / f).read_text(encoding="utf-8"))
    return 0


def up() -> int:
    if sh("docker", "compose", "up", "-d"):
        return 1
    print("waiting for postgres ", end="", flush=True)
    for _ in range(90):
        try:
            psycopg.connect(DB, connect_timeout=2).close()
            print(" ready")
            return 0
        except psycopg.OperationalError:
            print(".", end="", flush=True)
            time.sleep(1)
    print(" timed out")
    sh("docker", "compose", "logs", "db", "--tail", "30")
    return 1


def schema() -> int:
    return apply_sql("01_core.sql", "02_timeseries.sql", "04_ledger.sql")


def optimise() -> int:
    return apply_sql("03_optimise.sql")


COMMANDS = {
    "up": up,
    "down": lambda: sh("docker", "compose", "down"),
    "clean": lambda: sh("docker", "compose", "down", "-v"),
    "schema": schema,
    "seed": lambda: sh(PY, "seed.py"),
    "optimise": optimise,
    "anchor": lambda: sh(PY, "scripts/anchor.py"),
    "benchmark": lambda: sh(PY, "benchmark.py"),
    "verify": lambda: sh(PY, "verify.py"),
    "demo": lambda: sh(PY, "scripts/refresh_demo.py"),
    "tamper": lambda: sh(PY, "scripts/tamper_demo.py"),
    "test": lambda: sh(PY, "-m", "pytest", "tests/", "-q"),
    "app": lambda: sh(PY, "-m", "streamlit", "run", "app.py"),
}


def run_all() -> int:
    for name in ("up", "schema", "seed", "optimise", "benchmark", "verify"):
        print(f"\n=== {name} ===")
        if COMMANDS[name]():
            print(f"\n'{name}' failed -- stopping")
            return 1
    print("\npipeline complete. Launch the dashboard with:  python run.py app")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "all":
        return run_all()
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[cmd]() or 0


if __name__ == "__main__":
    sys.exit(main())
