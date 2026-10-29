"""
ChainTwin self-check.

Runs every claim the demo makes and prints pass/fail. If this is green, the demo
will work.

    python verify.py
"""
from __future__ import annotations

import os
import sys

import psycopg
from pgvector.psycopg import register_vector

import chain
import tamper
from embedding import load_embedder
from queries import ANOMALY, EXPOSED_LOTS, FORWARD, NAIVE, SIMILAR, exposure_start

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")
PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok ", FAIL: "FAIL ", WARN: "warn "}[status]
    print(f"{mark} {name}" + (f"  -- {detail}" if detail else ""))


def rejects(conn, sql: str, params=()) -> bool:
    """True if the statement is rejected by the database. Always rolled back."""
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(sql, params)
        return False
    except psycopg.Error:
        return True


def main() -> int:
    try:
        conn = psycopg.connect(DB, autocommit=True)
        register_vector(conn)
    except Exception as exc:
        print(f"FAIL  cannot connect to {DB.rsplit('@', 1)[-1]}: {exc}")
        print("      start the database (python run.py up) and re-run")
        return 1
    cur = conn.cursor()
    print(f"checking {DB.rsplit('@', 1)[-1]}\n")

    def one(sql, params=None):
        cur.execute(sql, params)
        return cur.fetchone()[0]

    # -- schema --------------------------------------------------------------
    for rel in ["parties", "products", "units", "lots", "lot_flows", "shipments", "custody",
                "sensor_readings", "quality_events", "incidents", "incident_reports",
                "report_bench", "audit_logs", "blocks", "ledger_tx", "telemetry_windows",
                "readings_hourly", "sensor_readings_plain"]:
        ok = one("SELECT to_regclass(%s) IS NOT NULL", (rel,))
        check(f"relation {rel}", PASS if ok else FAIL, "" if ok else "missing -- run schema, seed, optimise")
    check("extension pgvector", PASS if one("SELECT count(*) FROM pg_extension WHERE extname='vector'") else FAIL)

    # -- volumes -------------------------------------------------------------
    n = {t: one(f"SELECT count(*) FROM {t}") for t in
         ["lots", "lot_flows", "shipments", "custody", "sensor_readings", "incident_reports",
          "blocks", "ledger_tx", "telemetry_windows"]}
    for t, c in n.items():
        check(f"{t} populated", PASS if c > 0 else FAIL, f"{c:,} rows")
    check("sensor_readings == baseline copy", PASS
          if n["sensor_readings"] == one("SELECT count(*) FROM sensor_readings_plain") else FAIL)
    stray = one("SELECT count(*) FROM sensor_readings_default")
    check("no readings in the default partition", PASS if stray == 0 else FAIL, f"{stray} stray rows")

    # -- constraints reject bad data ------------------------------------------
    pid = one("SELECT product_id FROM products LIMIT 1")
    party = one("SELECT party_id FROM parties LIMIT 1")
    lot_a = one("SELECT lot_id FROM lots ORDER BY lot_id LIMIT 1")
    unit = one("SELECT unit_id FROM units LIMIT 1")
    bad = {
        "lot with quantity <= 0": ("INSERT INTO lots(lot_code,product_id,party_id,lot_type,quantity,created_at)"
                                   " VALUES ('X',%s,%s,'RAW',0,now())", (pid, party)),
        "lot with unknown type": ("INSERT INTO lots(lot_code,product_id,party_id,lot_type,quantity,created_at)"
                                  " VALUES ('X',%s,%s,'BOGUS',1,now())", (pid, party)),
        "duplicate lot code": ("INSERT INTO lots(lot_code,product_id,party_id,lot_type,quantity,created_at)"
                               " SELECT lot_code,product_id,party_id,lot_type,quantity,created_at FROM lots LIMIT 1", ()),
        "self-referencing flow": ("INSERT INTO lot_flows(from_lot,to_lot,flow_type,quantity) VALUES (%s,%s,'packed_as',1)",
                                  (lot_a, lot_a)),
        "custody ending before it starts": ("INSERT INTO custody(lot_id,unit_id,from_time,to_time)"
                                            " VALUES (%s,%s,now(),now() - interval '1 hour')", (lot_a, unit)),
        "inverted temperature window": ("UPDATE units SET temp_min = 9, temp_max = 1 WHERE unit_id = %s", (unit,)),
        "flow to a lot that does not exist": ("INSERT INTO lot_flows(from_lot,to_lot,flow_type,quantity)"
                                              " VALUES (%s,-5,'packed_as',1)", (lot_a,)),
        "ledger UPDATE": ("UPDATE ledger_tx SET subject = 'x' WHERE tx_id = (SELECT min(tx_id) FROM ledger_tx)", ()),
        "ledger DELETE": ("DELETE FROM blocks WHERE height = (SELECT max(height) FROM blocks)", ()),
    }
    for label, (sql, params) in bad.items():
        check(f"rejects {label}", PASS if rejects(conn, sql, params) else FAIL)

    # -- referential integrity ------------------------------------------------
    for label, sql in {
        "flows -> lots": "SELECT count(*) FROM lot_flows f LEFT JOIN lots a ON a.lot_id=f.from_lot "
                         "LEFT JOIN lots b ON b.lot_id=f.to_lot WHERE a.lot_id IS NULL OR b.lot_id IS NULL",
        "custody -> lots/units": "SELECT count(*) FROM custody c LEFT JOIN lots l USING(lot_id) "
                                 "LEFT JOIN units u USING(unit_id) WHERE l.lot_id IS NULL OR u.unit_id IS NULL",
        "readings -> units": "SELECT count(*) FROM sensor_readings r LEFT JOIN units u USING(unit_id) WHERE u.unit_id IS NULL",
    }.items():
        orphans = one(sql)
        check(f"no orphans: {label}", PASS if orphans == 0 else FAIL, f"{orphans} orphans")

    # -- supply-chain model sanity --------------------------------------------
    cur.execute("""SELECT count(*) FROM lots l WHERE l.lot_type <> 'RAW'
                   AND NOT EXISTS (SELECT 1 FROM lot_flows f WHERE f.to_lot = l.lot_id)""")
    check("every non-raw lot has a source", PASS if cur.fetchone()[0] == 0 else FAIL)
    cur.execute("""SELECT count(*) FROM lots b JOIN (SELECT to_lot, sum(quantity) q FROM lot_flows
                   WHERE flow_type='consumed_in' GROUP BY to_lot) s ON s.to_lot=b.lot_id
                   WHERE abs(s.q - b.quantity) > 0.02""")
    check("batch quantity == sum of consumed inputs", PASS if cur.fetchone()[0] == 0 else FAIL)
    cur.execute("""SELECT count(*) FROM (SELECT from_lot, sum(quantity) AS q FROM lot_flows
                   WHERE flow_type='consumed_in' GROUP BY from_lot) s JOIN lots r ON r.lot_id=s.from_lot
                   WHERE s.q > r.quantity + 0.02""")
    check("no raw lot over-consumed", PASS if cur.fetchone()[0] == 0 else FAIL)
    cur.execute("""WITH RECURSIVE up AS (
                     SELECT lot_id AS lot, lot_id AS anc, 0 AS d FROM lots WHERE lot_type = 'STOCK'
                     UNION ALL SELECT up.lot, f.from_lot, up.d + 1 FROM up
                     JOIN lot_flows f ON f.to_lot = up.anc WHERE up.d < 10)
                   SELECT count(DISTINCT lot) FROM up JOIN lots r ON r.lot_id = up.anc
                   WHERE r.lot_type = 'RAW'""")
    traced, total = cur.fetchone()[0], one("SELECT count(*) FROM lots WHERE lot_type='STOCK'")
    check("every stock lot traces back to a raw lot", PASS if traced == total else FAIL, f"{traced}/{total}")
    cur.execute("""SELECT count(*) FROM lots l WHERE l.lot_type <> 'RAW' AND NOT EXISTS (
                     SELECT 1 FROM custody c WHERE c.lot_id = l.lot_id)""")
    check("every lot has custody history", PASS if cur.fetchone()[0] == 0 else FAIL)
    cur.execute("""SELECT count(*) FROM sensor_readings r JOIN units u USING(unit_id)
                   WHERE u.unit_type='Reefer' AND NOT EXISTS (SELECT 1 FROM shipments s
                     WHERE s.unit_id=r.unit_id AND r.time >= s.depart_time AND r.time < s.arrive_time)""")
    check("reefer readings exist only during trips", PASS if cur.fetchone()[0] == 0 else FAIL)

    # -- audit trigger --------------------------------------------------------
    with conn.transaction(force_rollback=True):
        before = one("SELECT count(*) FROM audit_logs")
        conn.execute("UPDATE lots SET status = 'HOLD' WHERE lot_id = %s", (lot_a,))
        after = one("SELECT count(*) FROM audit_logs")
    check("audit trigger fires on a status change", PASS if after == before + 1 else FAIL)

    # -- showcase queries -----------------------------------------------------
    cur.execute(ANOMALY)
    anomalies = cur.fetchall()
    check("anomaly query finds the injected units", PASS if len(anomalies) >= 3 else WARN,
          f"{len(anomalies)} found" + ("" if len(anomalies) >= 3 else " -- run: python run.py demo"))
    cols = [d.name for d in cur.description]
    cur.execute("SELECT now()")
    now = cur.fetchone()[0]
    exposed = {}
    for a in anomalies:
        row = dict(zip(cols, a))
        since = exposure_start(cur, row["unit_id"])
        cur.execute(EXPOSED_LOTS, {"unit": row["unit_id"], "since": since, "until": now})
        exposed[row["name"]] = cur.rowcount
    check("exposure join returns lots for the flagged units", PASS if exposed and all(exposed.values()) else FAIL,
          ", ".join(f"{k}: {v} lots" for k, v in exposed.items()))

    cur.execute("SELECT lot_id, lot_code FROM quality_events q JOIN lots USING(lot_id) "
                "WHERE q.result='FAIL' AND lots.status='OK' ORDER BY tested_at DESC LIMIT 1")
    fail_lot = cur.fetchone()
    check("scripted failing lot present", PASS if fail_lot else FAIL)
    if fail_lot:
        cur.execute(FORWARD, {"seeds": [fail_lot[0]], "since": None})
        trace = cur.fetchall()
        types = {r[1] for r in trace}
        check("forward trace reaches batches, finished lots and retailers",
              PASS if {"BATCH", "FINISHED", "STOCK"} <= types else FAIL,
              f"{len(trace)} lots, types {sorted(types)}")
        shares = [r[4] for r in trace]
        check("exposure shares stay within (0, 1]", PASS if shares and all(0 < s <= 1.0000001 for s in shares) else FAIL,
              f"min {min(shares):.3f}, max {max(shares):.3f}")
        cur.execute(NAIVE, {"seeds": [fail_lot[0]]})
        naive = cur.fetchall()
        targeted = [r for r in trace if r[1] in ("FINISHED", "STOCK")]
        check("targeted recall is smaller than recalling everything since arrival", PASS if len(targeted) < len(naive) else FAIL,
              f"{len(targeted)} lots by genealogy vs {len(naive)} without genealogy")
    # cycle guard: insert a cycle in a rolled-back txn and make sure the traversal ends
    with conn.transaction(force_rollback=True):
        a, b = one("SELECT min(lot_id) FROM lots"), one("SELECT min(to_lot) FROM lot_flows")
        conn.execute("INSERT INTO lot_flows(from_lot,to_lot,flow_type,quantity) VALUES (%s,%s,'packed_as',1) "
                     "ON CONFLICT DO NOTHING", (b, a))
        cur.execute(FORWARD, {"seeds": [a], "since": None})
        check("forward trace terminates on a cycle", PASS)

    embedder = load_embedder(quiet=True)
    tests = {
        "Truck compressor failed and the trailer warmed up": "REEFER_FAULT",
        "Power cut in the cold room and temperature rising": "COLDROOM_FAULT",
        "Pasteuriser hold temperature dropped below legal limit": "PROCESS_DEVIATION",
        "Raw milk lot tested positive for antibiotic residue": "SUPPLIER_QUALITY",
        "Probe reading disagrees with the reference thermometer": "SENSOR_FAULT",
    }
    hits = 0
    for text, want in tests.items():
        vec = embedder.encode(text, normalize_embeddings=True)
        cur.execute(SIMILAR, (vec, vec))
        got = cur.fetchone()
        ok = got is not None and got[0] == want
        hits += ok
        check(f"similar incidents: '{text[:44]}...'", PASS if ok else WARN, f"top hit {got[0] if got else None}")
    cur.execute("EXPLAIN SELECT 1 FROM incident_reports ORDER BY embedding <=> (SELECT embedding FROM incident_reports LIMIT 1) LIMIT 3")
    plan = " ".join(r[0] for r in cur.fetchall())
    check("planner uses the HNSW index", PASS if "idx_reports_hnsw" in plan else WARN,
          "index scan" if "idx_reports_hnsw" in plan else "sequential scan (small table: acceptable)")

    # -- ledger ---------------------------------------------------------------
    c, c_ms = chain.timed(chain.verify_chain, cur)
    check("ledger: chain internally consistent", PASS if c["ok"] else FAIL,
          f"{c['blocks']} blocks, {c['txs']} txs, {c_ms:.0f} ms"
          + ("" if c["ok"] else f" -- {tamper.summarise(c['problems'])}"))
    check("ledger: checkpoint saved outside the database", PASS if c["checkpoint"] else WARN)
    o, o_ms = chain.timed(chain.verify_offchain, cur)
    check("ledger: database matches what was signed", PASS if o["ok"] else FAIL,
          f"{o['counts']['lots']} lots, {o['counts']['shipments']} hand-offs, "
          f"{o['counts']['sensor_windows']} sensor windows, "
          f"{o['readings_anchored']:,}/{o['readings_total']:,} readings anchored, {o_ms:.0f} ms"
          + ("" if o["ok"] else f" -- {tamper.summarise(o['problems'])}"))
    if c["ok"] and o["ok"]:
        for key, (title, _) in tamper.SCENARIOS.items():
            r = tamper.run(conn, key)
            check(f"tamper caught: {title}", PASS if r["detected"] else FAIL,
                  tamper.summarise(r["problems"], 1)[:150])
        again = tamper.baseline(conn)
        check("tamper scenarios left no trace (rolled back)", PASS if again["ok"] else FAIL)
    else:
        check("tamper scenarios", WARN, "skipped: ledger already reports problems")

    fails = [r for r in results if r[1] == FAIL]
    warns = [r for r in results if r[1] == WARN]
    print(f"\n{len(results) - len(fails) - len(warns)} passed, {len(warns)} warnings, {len(fails)} failed")
    if fails:
        print("not ready:", "; ".join(r[0] for r in fails))
        return 1
    print("Ready to present." if not warns else "Ready, with warnings above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
