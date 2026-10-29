"""
Tampering scenarios, for proving the ledger catches what it claims to catch.

Every scenario runs inside a transaction that is ROLLED BACK, so the database is
never left altered -- safe to run from the dashboard in front of an audience.

Each one plays an attacker with full SQL access (they can even switch off the
append-only trigger). The point is not that they are stopped from writing, it is
that what they wrote is detected afterwards:

    sensor     rewrite 30 minutes of a signed trip's temperature record
    quantity   shrink a finished lot's quantity
    genealogy  delete the link between a raw lot and the batch it went into
    unrecall   flip a recalled lot back to OK
    payload    edit a signed ledger transaction in place
    rewrite    edit a transaction AND recompute every hash and re-mine every
               later block, so the chain is internally consistent again
"""
from __future__ import annotations

import json
from datetime import timedelta

import chain


def _guards(cur, on: bool) -> None:
    verb = "ENABLE" if on else "DISABLE"
    cur.execute(f"ALTER TABLE ledger_tx {verb} TRIGGER trg_tx_immutable")
    cur.execute(f"ALTER TABLE blocks {verb} TRIGGER trg_blocks_immutable")


def sensor(cur) -> str:
    cur.execute("""SELECT w.unit_id, w.window_start, u.name FROM telemetry_windows w
                   JOIN units u USING (unit_id) WHERE u.unit_type = 'Reefer'
                   ORDER BY w.window_start LIMIT 1""")
    unit_id, start, name = cur.fetchone()
    cur.execute("UPDATE sensor_readings SET temp_c = temp_c + 6 "
                "WHERE unit_id = %s AND time >= %s AND time < %s",
                (unit_id, start + timedelta(minutes=20), start + timedelta(minutes=50)))
    return f"added 6 C to {cur.rowcount} readings of {name} on a trip that was already signed"


def quantity(cur) -> str:
    cur.execute("SELECT lot_id, lot_code, quantity FROM lots WHERE lot_type = 'FINISHED' "
                "ORDER BY lot_id LIMIT 1")
    lot_id, code, q = cur.fetchone()
    cur.execute("UPDATE lots SET quantity = quantity - 1000 WHERE lot_id = %s", (lot_id,))
    return f"reduced {code} from {q} to {float(q) - 1000:.2f} units"


def genealogy(cur) -> str:
    cur.execute("""SELECT f.from_lot, f.to_lot, a.lot_code, b.lot_code FROM lot_flows f
                   JOIN lots a ON a.lot_id = f.from_lot JOIN lots b ON b.lot_id = f.to_lot
                   WHERE f.flow_type = 'consumed_in' ORDER BY f.to_lot LIMIT 1""")
    src, dst, a, b = cur.fetchone()
    cur.execute("DELETE FROM lot_flows WHERE from_lot = %s AND to_lot = %s", (src, dst))
    return f"deleted the edge showing raw lot {a} went into batch {b} (recall evasion)"


def unrecall(cur) -> str:
    cur.execute("SELECT lot_id, lot_code FROM lots WHERE status = 'RECALLED' ORDER BY lot_id LIMIT 1")
    lot_id, code = cur.fetchone()
    cur.execute("UPDATE lots SET status = 'OK' WHERE lot_id = %s", (lot_id,))
    return f"set recalled lot {code} back to OK so it can be sold"


def payload(cur) -> str:
    _guards(cur, False)
    cur.execute("""SELECT tx_id, subject FROM ledger_tx WHERE tx_type = 'LOT_REGISTERED'
                   AND payload LIKE '%%"lot_type":"FINISHED"%%' ORDER BY tx_id LIMIT 1""")
    tx_id, subject = cur.fetchone()
    cur.execute("UPDATE ledger_tx SET payload = replace(payload, '\"lot_type\":\"FINISHED\"', "
                "'\"lot_type\":\"RAW\"') WHERE tx_id = %s", (tx_id,))
    return f"switched the append-only trigger off and rewrote the signed record of {subject}"


def rewrite(cur) -> str:
    """The strongest attack on a hash chain: change a record, then recompute every hash
    and re-mine every later block so the chain links up again."""
    _guards(cur, False)
    cur.execute("""SELECT tx_id, height, subject, payload FROM ledger_tx
                   WHERE tx_type = 'LOT_REGISTERED' ORDER BY tx_id LIMIT 1""")
    tx_id, height, subject, old = cur.fetchone()
    new = old.replace('"quantity":"', '"quantity":"1', 1)
    new_hash = chain.sha(new)
    cur.execute("SELECT tx_type, ts_ms FROM ledger_tx WHERE tx_id = %s", (tx_id,))
    ttype, ts = cur.fetchone()
    new_tx = chain.sha(chain.canon({"type": ttype, "subject": subject, "ts": ts, "payload_hash": new_hash}))
    cur.execute("UPDATE ledger_tx SET payload = %s, payload_hash = %s, tx_hash = %s WHERE tx_id = %s",
                (new, new_hash, new_tx, tx_id))
    cur.execute("SELECT max(height) FROM blocks")
    top = cur.fetchone()[0]
    prev = None
    for h in range(height, top + 1):
        cur.execute("SELECT tx_hash FROM ledger_tx WHERE height = %s ORDER BY tx_index", (h,))
        root = chain.merkle_root([r[0] for r in cur.fetchall()])
        cur.execute("SELECT prev_hash, ts_ms, difficulty FROM blocks WHERE height = %s", (h,))
        p, ts_ms, diff = cur.fetchone()
        p = prev if prev is not None else p
        nonce, bh = chain.mine(h, p, root, ts_ms, diff)
        cur.execute("UPDATE blocks SET prev_hash = %s, merkle_root = %s, nonce = %s, block_hash = %s "
                    "WHERE height = %s", (p, root, nonce, bh, h))
        prev = bh
    return (f"edited the record of {subject}, then recomputed its hashes and re-mined "
            f"{top - height + 1} blocks so the chain links up again")


SCENARIOS = {
    "sensor": ("Rewrite a trip's temperature record", sensor),
    "quantity": ("Shrink a lot's quantity", quantity),
    "genealogy": ("Delete a raw-to-batch link", genealogy),
    "unrecall": ("Un-recall a recalled lot", unrecall),
    "payload": ("Edit a signed ledger record", payload),
    "rewrite": ("Rewrite history and re-mine the chain", rewrite),
}


def run(conn, key: str) -> dict:
    """Apply one scenario inside a rolled-back transaction and report what the
    verifiers say. The connection must not already be inside a transaction."""
    title, fn = SCENARIOS[key]
    with conn.transaction(force_rollback=True):
        cur = conn.cursor()
        what = fn(cur)
        c, c_ms = chain.timed(chain.verify_chain, cur)
        o, o_ms = chain.timed(chain.verify_offchain, cur)
    problems = c["problems"] + o["problems"]
    return {"key": key, "title": title, "what": what, "detected": bool(problems),
            "problems": problems, "ms": c_ms + o_ms}


def baseline(conn) -> dict:
    cur = conn.cursor()
    c = chain.verify_chain(cur)
    o = chain.verify_offchain(cur)
    return {"ok": c["ok"] and o["ok"], "problems": c["problems"] + o["problems"]}


def summarise(problems: list[dict], limit: int = 3) -> str:
    return " | ".join(f"[{p['level']}] {p['where']}: {p['what']}" for p in problems[:limit]) \
        + (f" ... (+{len(problems) - limit} more)" if len(problems) > limit else "")


if __name__ == "__main__":      # pragma: no cover
    print(json.dumps(list(SCENARIOS)))
