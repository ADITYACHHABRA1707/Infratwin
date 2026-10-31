"""
Backup and restore with PostgreSQL's own tools (pg_dump / pg_restore), run inside the
database container so no client install is needed.

    python scripts/backup.py backup                 -> backups/chaintwin_<timestamp>.dump
    python scripts/backup.py restore <file.dump>    -> restores into a SEPARATE database
                                                       `chaintwin_restored` and compares counts
    python scripts/backup.py list

Restoring into a separate database means a drill never overwrites live data. The ledger
checkpoint (.ledger_checkpoint.json) lives outside the database on purpose; back it up with
the dump.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backups"
CONTAINER = "chaintwin-db"
CHECK_TABLES = ["parties", "products", "units", "lots", "lot_flows", "shipments", "custody",
                "quality_events", "incident_reports", "blocks", "ledger_tx", "users", "sensor_readings"]


def _db() -> tuple[str, str]:
    u = urlparse(settings.database_url())
    return u.username or "postgres", (u.path or "/postgres").lstrip("/")


def _docker(*args: str, stdin=None, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", *( ["-i"] if stdin else []), CONTAINER, *args],
                          stdin=stdin, stdout=stdout, check=True, stderr=subprocess.PIPE)


def backup() -> Path:
    OUT.mkdir(exist_ok=True)
    user, db = _db()
    target = OUT / f"chaintwin_{datetime.now():%Y%m%d_%H%M%S}.dump"
    print(f"$ docker exec {CONTAINER} pg_dump -U {user} -Fc {db}  >  {target.name}")
    with open(target, "wb") as f:
        _docker("pg_dump", "-U", user, "-Fc", db, stdout=f)
    print(f"  wrote {target.name}  ({target.stat().st_size / 1e6:.1f} MB, custom format, compressed)")
    return target


def _count(db: str, table: str) -> int:
    user, _ = _db()
    r = _docker("psql", "-U", user, "-d", db, "-tAc", f"SELECT count(*) FROM {table}", stdout=subprocess.PIPE)
    return int(r.stdout.decode().strip())


def restore(dump: Path) -> int:
    user, db = _db()
    target = "chaintwin_restored"
    print(f"$ createdb {target}")
    _docker("psql", "-U", user, "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {target}", stdout=subprocess.PIPE)
    _docker("psql", "-U", user, "-d", "postgres", "-c", f"CREATE DATABASE {target}", stdout=subprocess.PIPE)
    _docker("psql", "-U", user, "-d", target, "-c", "CREATE EXTENSION IF NOT EXISTS vector", stdout=subprocess.PIPE)
    print(f"$ docker exec -i {CONTAINER} pg_restore -U {user} -d {target} --no-owner  <  {dump.name}")
    with open(dump, "rb") as f:
        p = subprocess.run(["docker", "exec", "-i", CONTAINER, "pg_restore", "-U", user, "-d", target,
                            "--no-owner", "--if-exists", "--clean"], stdin=f,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode not in (0, 1):      # 1 = warnings (e.g. pre-existing extension), still restored
        print(p.stderr.decode()[-500:])
        return 1
    print(f"\n  {'table':18}{'live':>12}{'restored':>12}  match")
    ok = True
    for t in CHECK_TABLES:
        a, b = _count(db, t), _count(target, t)
        ok &= a == b
        print(f"  {t:18}{a:>12,}{b:>12,}  {'yes' if a == b else 'NO'}")
    print("\n  restore verified: every table matches" if ok else "\n  MISMATCH")
    return 0 if ok else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backup":
        backup()
        return 0
    if cmd == "restore":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else max(OUT.glob("*.dump"), default=None)
        if path is None or not path.exists():
            print("no dump file; run `backup` first")
            return 2
        return restore(path)
    if cmd == "list":
        for f in sorted(OUT.glob("*.dump")):
            print(f"{f.name}  {f.stat().st_size / 1e6:.1f} MB")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
