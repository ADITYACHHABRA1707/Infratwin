"""
ChainTwin ledger.

A small permissioned blockchain that lives in PostgreSQL next to the business
data. It stores hashes and signatures, not business data: the relational tables
stay the working copy, and the ledger is what proves they were not edited.

    Block   height, prev_hash, merkle_root, nonce, ts  ->  block_hash (proof of work)
    Tx      type, subject, canonical payload, signatures by every required party

Verification is two-sided:

    verify_chain()    is the ledger internally consistent?
                      (links, hashes, Merkle roots, signatures, checkpoint)
    verify_offchain() does the relational data still match what was signed?
                      (lots, shipments, lab results, status, sensor windows)

This module is the only place the encoding is defined. Writer and verifier both
call it, so they cannot drift apart.

Demo-grade choices, called out so nobody mistakes them for production design:
  * Party keys are derived from the party name (party_key). A real deployment
    keeps private keys in each party's own HSM and verifies with public keys.
  * HMAC-SHA256 stands in for an asymmetric signature (Ed25519/ECDSA).
  * Proof of work with a low difficulty shows the mechanism. A consortium chain
    would use PoA/BFT consensus (Hyperledger Fabric, Besu/QBFT).
  * One node. Multi-party trust needs each party to hold a copy and compare tip
    hashes; the checkpoint file models that.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIFFICULTY = int(os.getenv("LEDGER_DIFFICULTY", "3"))
CHECKPOINT = Path(os.getenv("LEDGER_CHECKPOINT", ROOT / ".ledger_checkpoint.json"))
GENESIS_PREV = "0" * 64
PLANT_NAME = "Aarav Dairy Works"   # the manufacturer; its QA lab signs lab results
SETTLE_HOURS = 6          # a window is anchored only once it has been closed this long
TX_PER_BLOCK = 100


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def canon(obj) -> str:
    """Canonical JSON: sorted keys, no whitespace. The one encoding that is hashed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(round(dt.timestamp() * 1000))


def party_key(identity: str) -> bytes:
    """DEMO ONLY. Deterministic key per identity (a party name or 'device:<unit>')."""
    return hashlib.sha256(f"chaintwin-demo-key:{identity}".encode()).digest()


def sign(identity: str, tx_hash: str) -> str:
    return hmac.new(party_key(identity), tx_hash.encode(), hashlib.sha256).hexdigest()


def merkle_root(hashes: list[str]) -> str:
    """Binary Merkle tree; an odd node is paired with itself. Empty -> hash of ''."""
    if not hashes:
        return sha("")
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def reading_leaf(unit_id: int, ts: datetime, temp: float, hum: float | None) -> str:
    h = "-" if hum is None else f"{hum:.1f}"
    return sha(f"{unit_id}|{ms(ts)}|{temp:.2f}|{h}")


def window_root(unit_id: int, rows) -> str:
    return merkle_root([reading_leaf(unit_id, t, c, h) for t, c, h in rows])


def block_hash(height: int, prev: str, root: str, nonce: int, ts_ms: int) -> str:
    return sha(f"{height}|{prev}|{root}|{nonce}|{ts_ms}")


def mine(height: int, prev: str, root: str, ts_ms: int, difficulty: int) -> tuple[int, str]:
    target = "0" * difficulty
    nonce = 0
    while True:
        h = block_hash(height, prev, root, nonce, ts_ms)
        if h.startswith(target):
            return nonce, h
        nonce += 1


def make_tx(tx_type: str, subject: str, ts_ms: int, payload: dict, signers: list[str]) -> dict:
    """Build a signed transaction. The required signers are part of the payload,
    so removing a signature later invalidates the payload hash."""
    payload = {**payload, "signers": sorted(signers)}
    payload_s = canon(payload)
    payload_hash = sha(payload_s)
    tx_hash = sha(canon({"type": tx_type, "subject": subject, "ts": ts_ms,
                         "payload_hash": payload_hash}))
    return {
        "tx_type": tx_type, "subject": subject, "ts_ms": ts_ms,
        "payload": payload_s, "payload_hash": payload_hash, "tx_hash": tx_hash,
        "signatures": canon({s: sign(s, tx_hash) for s in sorted(signers)}),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def tip(cur) -> tuple[int, str]:
    """(height, hash) of the chain head, or (-1, GENESIS_PREV) when empty."""
    cur.execute("SELECT height, block_hash FROM blocks ORDER BY height DESC LIMIT 1")
    row = cur.fetchone()
    return (row[0], row[1]) if row else (-1, GENESIS_PREV)


def append_block(cur, txs: list[dict], ts_ms: int | None = None,
                 difficulty: int | None = None) -> int:
    """Seal `txs` into one block on top of the current tip. Returns its height."""
    if not txs:
        raise ValueError("refusing to mine an empty block")
    difficulty = DIFFICULTY if difficulty is None else difficulty
    height, prev = tip(cur)
    height += 1
    ts_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    root = merkle_root([t["tx_hash"] for t in txs])
    nonce, bh = mine(height, prev, root, ts_ms, difficulty)
    cur.execute(
        "INSERT INTO blocks(height, prev_hash, merkle_root, nonce, difficulty, ts_ms, "
        "block_hash, tx_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (height, prev, root, nonce, difficulty, ts_ms, bh, len(txs)))
    cur.executemany(
        "INSERT INTO ledger_tx(height, tx_index, tx_type, subject, ts_ms, payload, "
        "payload_hash, tx_hash, signatures) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(height, i, t["tx_type"], t["subject"], t["ts_ms"], t["payload"],
          t["payload_hash"], t["tx_hash"], t["signatures"]) for i, t in enumerate(txs)])
    return height


def append_txs(cur, txs: list[dict], per_block: int = TX_PER_BLOCK, live: bool = False) -> int:
    """Chunk `txs` into blocks. Backfilled history is stamped with the latest event
    time in each block; live writes (live=True) are stamped with the wall clock."""
    blocks = 0
    for i in range(0, len(txs), per_block):
        chunk = txs[i:i + per_block]
        ts = None if live else max(t["ts_ms"] for t in chunk)
        append_block(cur, chunk, ts_ms=ts)
        blocks += 1
    return blocks


def save_checkpoint(cur) -> dict:
    """Record the chain head outside the database. Each party keeping such a
    record is what stops a database owner from quietly re-mining history."""
    h, bh = tip(cur)
    cp = {"height": h, "block_hash": bh, "saved_at": datetime.now(timezone.utc).isoformat()}
    CHECKPOINT.write_text(json.dumps(cp, indent=2))
    return cp


# ---------------------------------------------------------------------------
# Payload builders, shared by the writer (seed.py) and the verifier
# ---------------------------------------------------------------------------
def _q(v) -> str:
    return f"{float(v):.2f}"


def lot_payloads(cur) -> dict[str, tuple[dict, list[str]]]:
    """lot_code -> (payload, signers) rebuilt from the relational tables."""
    cur.execute("""
        SELECT f.to_lot, fl.lot_code, f.quantity, f.flow_type, s.shipment_code
        FROM lot_flows f JOIN lots fl ON fl.lot_id = f.from_lot
        LEFT JOIN shipments s ON s.shipment_id = f.shipment_id""")
    inputs = defaultdict(list)
    for to_lot, code, qty, ft, ship in cur.fetchall():
        inputs[to_lot].append({"lot_code": code, "quantity": _q(qty), "flow_type": ft,
                               "shipment": ship})
    cur.execute("""
        SELECT l.lot_id, l.lot_code, p.sku, l.lot_type, pa.name, l.quantity,
               l.created_at, l.expires_at
        FROM lots l JOIN products p USING (product_id) JOIN parties pa ON pa.party_id = l.party_id""")
    out = {}
    for lot_id, code, sku, ltype, holder, qty, created, expires in cur.fetchall():
        ins = sorted(inputs.get(lot_id, []), key=lambda i: i["lot_code"])
        out[code] = ({"lot_code": code, "sku": sku, "lot_type": ltype, "holder": holder,
                      "quantity": _q(qty), "created_ms": ms(created),
                      "expires_ms": ms(expires), "inputs": ins}, [holder])
    return out


def transfer_payloads(cur, cutoff: datetime | None = None) -> dict[str, tuple[dict, list[str]]]:
    """shipment_code -> (payload, signers) for shipments that arrived on or before
    `cutoff`. Sender, receiver and carrier all sign the hand-off."""
    cutoff = cutoff or datetime.now(timezone.utc) - timedelta(hours=SETTLE_HOURS)
    cur.execute("""
        SELECT s.shipment_id, s.shipment_code, o.name, d.name, c.name, u.name,
               s.depart_time, s.arrive_time
        FROM shipments s JOIN parties o ON o.party_id = s.origin_party
        JOIN parties d ON d.party_id = s.dest_party
        JOIN parties c ON c.party_id = s.carrier_party
        JOIN units u ON u.unit_id = s.unit_id
        WHERE s.arrive_time <= %s""", (cutoff,))
    ships = cur.fetchall()
    cur.execute("""
        SELECT c.shipment_id, l.lot_code FROM custody c JOIN lots l USING (lot_id)
        WHERE c.shipment_id IS NOT NULL""")
    carried = defaultdict(list)
    for sid, code in cur.fetchall():
        carried[sid].append(code)
    cur.execute("""
        SELECT f.shipment_id, a.lot_code, b.lot_code, f.quantity
        FROM lot_flows f JOIN lots a ON a.lot_id = f.from_lot JOIN lots b ON b.lot_id = f.to_lot
        WHERE f.shipment_id IS NOT NULL""")
    flows = defaultdict(list)
    for sid, src, dst, qty in cur.fetchall():
        flows[sid].append({"from_lot": src, "to_lot": dst, "quantity": _q(qty)})
    out = {}
    for sid, code, frm, to, carrier, unit, dep, arr in ships:
        out[code] = ({"shipment_code": code, "from": frm, "to": to, "carrier": carrier,
                      "unit": unit, "depart_ms": ms(dep), "arrive_ms": ms(arr),
                      "carried": sorted(carried.get(sid, [])),
                      "flows": sorted(flows.get(sid, []), key=lambda x: (x["from_lot"], x["to_lot"]))},
                     sorted({frm, to, carrier}))
    return out


def quality_payloads(cur, lab: str) -> dict[str, tuple[dict, list[str]]]:
    cur.execute("""
        SELECT q.event_id, l.lot_code, q.test_name, q.result, q.measured, q.limit_value, q.tested_at
        FROM quality_events q JOIN lots l USING (lot_id)""")
    out = {}
    for eid, code, test, res, meas, lim, at in cur.fetchall():
        key = f"{code}#{test}#{ms(at)}"
        out[key] = ({"lot_code": code, "test": test, "result": res,
                     "measured": None if meas is None else round(float(meas), 3),
                     "limit": None if lim is None else round(float(lim), 3),
                     "tested_ms": ms(at)}, [lab])
    return out


def digest_payload(unit_name: str, unit_id: int, start: datetime, end: datetime,
                   rows, shipment_code: str | None) -> tuple[dict, list[str]]:
    temps = [r[1] for r in rows]
    return ({"unit": unit_name, "window_start_ms": ms(start), "window_end_ms": ms(end),
             "count": len(rows), "min": round(min(temps), 2), "max": round(max(temps), 2),
             "merkle_root": window_root(unit_id, rows), "shipment": shipment_code},
            [f"device:{unit_name}"])


def status_payload(lot_code: str, old: str, new: str, reason: str, party: str):
    return ({"lot_code": lot_code, "from_status": old, "to_status": new, "reason": reason},
            [party])


# ---------------------------------------------------------------------------
# Anchoring sensor history
# ---------------------------------------------------------------------------
def _fetch_rows(cur, unit_id: int, start: datetime, end: datetime):
    cur.execute("SELECT time, temp_c, humidity FROM sensor_readings "
                "WHERE unit_id = %s AND time >= %s AND time < %s ORDER BY time",
                (unit_id, start, end))
    return cur.fetchall()


def anchor_closed_windows(cur, now: datetime | None = None,
                          settle_hours: int = SETTLE_HOURS) -> dict:
    """Anchor every sensor window that has been closed for `settle_hours` and is not
    anchored yet: one window per completed UTC day for fixed units, and one per
    completed trip for reefers. Open windows (today, trips in progress) are left
    alone, because their data is still arriving."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settle_hours)

    cur.execute("SELECT unit_id, unit_type, name FROM units ORDER BY unit_id")
    units = cur.fetchall()
    cur.execute("SELECT unit_id, window_start, window_end FROM telemetry_windows")
    done = {(u, s, e) for u, s, e in cur.fetchall()}

    todo = []   # (unit_id, unit_name, start, end, shipment_id, shipment_code)
    for unit_id, utype, name in units:
        if utype == "Reefer":
            cur.execute("SELECT shipment_id, shipment_code, depart_time, arrive_time "
                        "FROM shipments WHERE unit_id = %s AND arrive_time <= %s "
                        "ORDER BY depart_time", (unit_id, cutoff))
            for sid, code, dep, arr in cur.fetchall():
                if (unit_id, dep, arr) not in done:
                    todo.append((unit_id, name, dep, arr, sid, code))
        else:
            cur.execute("SELECT min(time) FROM sensor_readings WHERE unit_id = %s", (unit_id,))
            first = cur.fetchone()[0]
            if first is None:
                continue
            day = first.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            while day + timedelta(days=1) <= cutoff:
                end = day + timedelta(days=1)
                if (unit_id, day, end) not in done:
                    todo.append((unit_id, name, day, end, None, None))
                day = end

    txs, wins = [], []
    for unit_id, name, start, end, sid, code in todo:
        rows = _fetch_rows(cur, unit_id, start, end)
        if not rows:
            continue
        payload, signers = digest_payload(name, unit_id, start, end, rows, code)
        tx = make_tx("TELEMETRY_DIGEST", f"{name}@{ms(start)}", ms(end), payload, signers)
        txs.append(tx)
        wins.append((unit_id, start, end, sid, tx["tx_hash"]))
    txs_wins = sorted(zip(txs, wins), key=lambda p: p[0]["ts_ms"])
    blocks = 0
    for i in range(0, len(txs_wins), TX_PER_BLOCK):
        chunk = txs_wins[i:i + TX_PER_BLOCK]
        append_block(cur, [t for t, _ in chunk], ts_ms=max(t["ts_ms"] for t, _ in chunk))
        cur.executemany(
            "INSERT INTO telemetry_windows(unit_id, window_start, window_end, shipment_id, tx_hash) "
            "VALUES (%s,%s,%s,%s,%s)", [w for _, w in chunk])
        blocks += 1
    return {"windows": len(txs), "blocks": blocks}


def record_settled_shipments(cur, now: datetime | None = None,
                             settle_hours: int = SETTLE_HOURS) -> int:
    """Put every settled, not-yet-recorded shipment on the ledger as a hand-off
    signed by sender, receiver and carrier."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT subject FROM ledger_tx WHERE tx_type = 'CUSTODY_TRANSFER'")
    have = {r[0] for r in cur.fetchall()}
    txs = [make_tx("CUSTODY_TRANSFER", code, payload["arrive_ms"], payload, signers)
           for code, (payload, signers) in
           transfer_payloads(cur, now - timedelta(hours=settle_hours)).items() if code not in have]
    txs.sort(key=lambda t: t["ts_ms"])
    if txs:
        append_txs(cur, txs)
    return len(txs)


def anchor_pending(cur, now: datetime | None = None, settle_hours: int = SETTLE_HOURS) -> dict:
    """Everything that has settled and is not on the ledger yet: shipment hand-offs
    and sensor-history digests. Safe to run repeatedly."""
    shipments = record_settled_shipments(cur, now, settle_hours)
    tele = anchor_closed_windows(cur, now, settle_hours)
    return {"shipments": shipments, "windows": tele["windows"], "blocks": tele["blocks"]}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _problem(level: str, where: str, what: str) -> dict:
    return {"level": level, "where": where, "what": what}


def verify_chain(cur) -> dict:
    """Is the ledger internally consistent? Checks, per block: height continuity,
    prev-hash link, proof of work, recomputed block hash, Merkle root over its
    transactions; per transaction: payload hash, tx hash, signer set and every
    signature; then the external checkpoint, if one was saved."""
    problems: list[dict] = []
    cur.execute("SELECT height, prev_hash, merkle_root, nonce, difficulty, ts_ms, block_hash, "
                "tx_count FROM blocks ORDER BY height")
    blocks = cur.fetchall()
    cur.execute("SELECT height, tx_index, tx_type, subject, ts_ms, payload, payload_hash, "
                "tx_hash, signatures FROM ledger_tx ORDER BY height, tx_index")
    by_block = defaultdict(list)
    for r in cur.fetchall():
        by_block[r[0]].append(r)

    prev_hash, n_tx = GENESIS_PREV, 0
    for i, (h, prev, root, nonce, diff, ts, bh, cnt) in enumerate(blocks):
        where = f"block {h}"
        if h != i:
            problems.append(_problem("chain", where, f"height gap: expected {i}, found {h}"))
        if prev != prev_hash:
            problems.append(_problem("chain", where, "prev_hash does not match the previous block"))
        recomputed = block_hash(h, prev, root, nonce, ts)
        if recomputed != bh:
            problems.append(_problem("chain", where, "block hash does not match its contents"))
        if not bh.startswith("0" * diff):
            problems.append(_problem("chain", where, f"proof of work not met (difficulty {diff})"))
        txs = by_block.get(h, [])
        if len(txs) != cnt:
            problems.append(_problem("chain", where, f"tx_count {cnt} but {len(txs)} stored"))
        if merkle_root([t[7] for t in txs]) != root:
            problems.append(_problem("chain", where, "Merkle root does not match its transactions"))
        for (_, idx, ttype, subject, tts, payload, phash, thash, sigs) in txs:
            twhere = f"{where} tx {idx} ({ttype} {subject})"
            if sha(payload) != phash:
                problems.append(_problem("tx", twhere, "payload was altered (payload hash mismatch)"))
            if sha(canon({"type": ttype, "subject": subject, "ts": tts,
                          "payload_hash": phash})) != thash:
                problems.append(_problem("tx", twhere, "transaction header was altered"))
            try:
                required = set(json.loads(payload).get("signers", []))
                given = json.loads(sigs)
            except ValueError:
                problems.append(_problem("tx", twhere, "payload or signatures are not valid JSON"))
                continue
            if set(given) != required:
                problems.append(_problem("tx", twhere,
                                         f"signer set {sorted(given)} differs from required {sorted(required)}"))
            for who, sig in given.items():
                if not hmac.compare_digest(sig, sign(who, thash)):
                    problems.append(_problem("tx", twhere, f"invalid signature from {who}"))
        prev_hash = bh
        n_tx += len(txs)

    if CHECKPOINT.exists():
        try:
            cp = json.loads(CHECKPOINT.read_text())
            hit = next((b for b in blocks if b[0] == cp["height"]), None)
            if hit is None:
                problems.append(_problem("checkpoint", f"height {cp['height']}",
                                         "block recorded in the checkpoint no longer exists"))
            elif hit[6] != cp["block_hash"]:
                problems.append(_problem("checkpoint", f"height {cp['height']}",
                                         "history was rewritten: block hash differs from the "
                                         "one saved outside the database"))
        except (ValueError, KeyError):
            problems.append(_problem("checkpoint", str(CHECKPOINT), "checkpoint file is unreadable"))

    return {"ok": not problems, "blocks": len(blocks), "txs": n_tx, "problems": problems,
            "tip": blocks[-1][6] if blocks else None,
            "checkpoint": CHECKPOINT.exists()}


def _diff(expected: dict, ledger: dict) -> str:
    keys = sorted(set(expected) | set(ledger))
    parts = [f"{k}: ledger={ledger.get(k)!r} database={expected.get(k)!r}"
             for k in keys if expected.get(k) != ledger.get(k) and k != "signers"]
    return "; ".join(parts)[:300] or "payload differs"


def _compare(label: str, expected: dict, ledger: dict, problems: list[dict]) -> int:
    """expected: key -> (payload, signers) from the database; ledger: key -> tx payload."""
    checked = 0
    for key, (payload, signers) in expected.items():
        want = canon({**payload, "signers": sorted(signers)})
        got = ledger.get(key)
        if got is None:
            problems.append(_problem(label, key, "exists in the database but was never recorded on the ledger"))
        elif got[0] != sha(want):
            problems.append(_problem(label, key, "database record differs from what was signed: "
                                     + _diff({**payload, "signers": sorted(signers)}, json.loads(got[1]))))
        checked += 1
    for key in ledger.keys() - expected.keys():
        problems.append(_problem(label, key, "recorded on the ledger but missing from the database"))
    return checked


def verify_offchain(cur, lab: str = PLANT_NAME) -> dict:
    """Does the relational data still match what the parties signed?"""
    problems: list[dict] = []
    cur.execute("SELECT tx_type, subject, payload_hash, payload FROM ledger_tx "
                "WHERE tx_type IN ('LOT_REGISTERED','CUSTODY_TRANSFER','QUALITY_RESULT')")
    led = defaultdict(dict)
    for ttype, subject, ph, payload in cur.fetchall():
        led[ttype][subject] = (ph, payload)

    counts = {}
    counts["lots"] = _compare("lot", lot_payloads(cur), led["LOT_REGISTERED"], problems)
    counts["shipments"] = _compare("shipment", transfer_payloads(cur), led["CUSTODY_TRANSFER"], problems)
    qled = {}
    for subject, (ph, payload) in led["QUALITY_RESULT"].items():
        p = json.loads(payload)
        qled[f"{p['lot_code']}#{p['test']}#{p['tested_ms']}"] = (ph, payload)
    counts["lab_results"] = _compare("lab result", quality_payloads(cur, lab), qled, problems)

    # Status: the latest STATUS_CHANGE per lot is the truth; no entry means OK.
    cur.execute("SELECT subject, payload FROM ledger_tx WHERE tx_type = 'STATUS_CHANGE' "
                "ORDER BY ts_ms, tx_id")
    status = {s: json.loads(p)["to_status"] for s, p in cur.fetchall()}
    cur.execute("SELECT lot_code, status FROM lots")
    for code, st in cur.fetchall():
        if st != status.get(code, "OK"):
            problems.append(_problem("status", code, f"database says {st}, ledger says {status.get(code, 'OK')}"))
    counts["statuses"] = len(status)

    # Sensor windows: recompute the Merkle root from the raw rows.
    cur.execute("""SELECT w.unit_id, w.window_start, w.window_end, t.payload, u.name
                   FROM telemetry_windows w JOIN ledger_tx t USING (tx_hash)
                   JOIN units u ON u.unit_id = w.unit_id ORDER BY w.unit_id, w.window_start""")
    wins = cur.fetchall()
    for unit_id, start, end, payload, name in wins:
        led_p = json.loads(payload)
        rows = _fetch_rows(cur, unit_id, start, end)
        root = window_root(unit_id, rows)
        if root != led_p["merkle_root"] or len(rows) != led_p["count"]:
            lo = min((r[1] for r in rows), default=None)
            hi = max((r[1] for r in rows), default=None)
            problems.append(_problem(
                "telemetry", f"{name} {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}",
                f"sensor history differs from the anchored digest: ledger has {led_p['count']} readings "
                f"(max {led_p['max']} C), database has {len(rows)} (max "
                f"{hi if hi is None else round(hi, 2)} C, min {lo if lo is None else round(lo, 2)} C)"))
    counts["sensor_windows"] = len(wins)

    cur.execute("SELECT count(*) FROM sensor_readings")
    total = cur.fetchone()[0]
    cur.execute("""SELECT coalesce(sum(c.n), 0) FROM telemetry_windows w
                   CROSS JOIN LATERAL (SELECT count(*) AS n FROM sensor_readings r
                     WHERE r.unit_id = w.unit_id AND r.time >= w.window_start
                       AND r.time < w.window_end) c""")
    anchored = int(cur.fetchone()[0])
    return {"ok": not problems, "problems": problems, "counts": counts,
            "readings_total": total, "readings_anchored": anchored}


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    out = fn(*a, **k)
    return out, (time.perf_counter() - t0) * 1000.0
