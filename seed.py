"""
ChainTwin seed.

Simulates DAYS of operation of a dairy supply chain, then loads it:

    3 milk co-ops + 1 pulp supplier --> plant (RM cold store -> 2 pasteuriser lines
    -> packing -> FG cold store) --> 2 distribution hubs --> 8 retailers

and writes every lot, hand-off and sensor window onto the ledger.

Run after sql/01..02 (python run.py schema):

    python seed.py

Environment:
    DAYS              days of history                       default 30
    BENCH_VECTORS     rows in report_bench                  default 20000
    LEDGER_DIFFICULTY proof-of-work difficulty (hex zeros)  default 3
    DATABASE_URL      connection string

The plant clock is anchored so that "now" is always 18:30 on the last day: trucks
have just left the plant, the evening batch is still on the pasteuriser, and the
morning's deliveries are on retail shelves. That gives the dashboard live
material at any time of day.
"""
from __future__ import annotations

import os
import random
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

import chain
from embedding import EMBED_DIM, compose_report, load_embedder
from queries import FORWARD

ROOT = Path(__file__).resolve().parent
import settings  # noqa: E402  (loads .env; no secrets in source)
DB = settings.database_url()
DAYS = int(os.getenv("DAYS", "30"))
BENCH_VECTORS = int(os.getenv("BENCH_VECTORS", "20000"))
SEED = 7

H = timedelta(hours=1)
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
LAST_MIDNIGHT = NOW - timedelta(hours=18, minutes=30)      # plant clock: NOW is 18:30
PLANT = chain.PLANT_NAME

# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------
MILK_SUPPLIERS = [("Kaveri Milk Co-op", "Vellore"), ("Palar Dairy Farms", "Tirupattur"),
                  ("Nandi Milk Producers", "Krishnagiri")]
PULP_SUPPLIER = ("Orchard Fresh Pulp", "Chittoor")
CARRIERS = ["ColdLink Logistics", "FrostLine Transport", "SwiftChill Carriers"]   # inbound / trunk / last mile
HUBS = {"Chennai Distribution Hub": ("Chennai", 4.5, 2.5),        # city, trunk hours, last-mile hours
        "Bengaluru Distribution Hub": ("Bengaluru", 6.0, 3.0)}
RETAILERS = {
    "Chennai Distribution Hub": ["FreshMart T Nagar", "FreshMart Anna Nagar",
                                 "GreenBasket Adyar", "DailyNeeds Velachery"],
    "Bengaluru Distribution Hub": ["FreshMart Indiranagar", "GreenBasket Koramangala",
                                   "DailyNeeds Whitefield", "SuperSave Jayanagar"],
}

# sku, name, category, temp_min, temp_max, shelf_life_days
PRODUCTS = [
    ("RAW-MILK", "Raw chilled milk", "RAW", 1.0, 5.0, 3),
    ("RAW-PULP", "Fruit pulp", "RAW", 0.0, 6.0, 10),
    ("FIN-MILK1L", "Toned milk 1 L", "FINISHED", 2.0, 6.0, 5),
    ("FIN-YOG400", "Plain yoghurt 400 g", "FINISHED", 2.0, 6.0, 14),
    ("FIN-FYOG400", "Fruit yoghurt 400 g", "FINISHED", 2.0, 6.0, 14),
]
UNITS_PER_BATCH_UNIT = {"FIN-MILK1L": 1.0, "FIN-YOG400": 2.5, "FIN-FYOG400": 2.2}
BATCH_CYCLE = ["FIN-MILK1L", "FIN-YOG400", "FIN-FYOG400"]
BATCH_STARTS = [5.0, 8.0, 11.0, 17.0]           # plant-clock hours
BATCH_HOURS = 2.5
MILK_PER_BATCH = 10000.0
PULP_PER_FRUIT_BATCH = 1500.0
RAW_MILK_LOT = 13000.0      # 3 lots/day = 39,000 L against 40,000 L of demand, so lots do not queue
PULP_LOT = 2500.0


def log_step(label: str) -> float:
    print(f"\n[{label}]")
    return time.perf_counter()


def done(t0: float, msg: str = "") -> None:
    print(f"  {msg}  ({time.perf_counter() - t0:.1f}s)" if msg else f"  ({time.perf_counter() - t0:.1f}s)")


class Pool:
    """Reefers of one carrier, handed out greedily by availability."""

    def __init__(self, unit_ids: list[int]) -> None:
        self.free = {u: datetime.min.replace(tzinfo=timezone.utc) for u in unit_ids}

    def take(self, dep: datetime, arr: datetime) -> int:
        for u, f in self.free.items():
            if f <= dep:
                self.free[u] = arr
                return u
        raise RuntimeError("not enough reefers for the schedule")


class World:
    def __init__(self) -> None:
        self.lots: list[dict] = []
        self.flows: list[dict] = []
        self.ships: list[dict] = []
        self.cust: list[dict] = []
        self.quality: list[dict] = []
        self._seq: dict = defaultdict(int)

    def lot(self, prefix, sku, party, ltype, qty, created, expires=None) -> dict:
        key = (prefix, f"{created:%y%m%d}")
        self._seq[key] += 1
        lot = dict(id=len(self.lots) + 1, code=f"{prefix}-{key[1]}-{self._seq[key]:02d}", sku=sku,
                   party=party, type=ltype, qty=round(float(qty), 2), created=created,
                   expires=expires, status="OK")
        self.lots.append(lot)
        return lot

    def ship(self, origin, dest, carrier, unit, dep, arr) -> dict:
        key = ("SH", f"{dep:%y%m%d}")
        self._seq[key] += 1
        s = dict(id=len(self.ships) + 1, code=f"SH-{key[1]}-{self._seq[key]:03d}", origin=origin,
                 dest=dest, carrier=carrier, unit=unit, dep=dep, arr=arr)
        self.ships.append(s)
        return s

    def flow(self, src, dst, ftype, qty, ship=None) -> None:
        self.flows.append(dict(src=src["id"], dst=dst["id"], type=ftype, qty=round(float(qty), 2),
                               ship=None if ship is None else ship["id"]))

    def stay(self, lot, unit, frm, to, ship=None) -> dict:
        row = dict(lot=lot["id"], unit=unit, frm=frm, to=to, ship=None if ship is None else ship["id"])
        self.cust.append(row)
        return row


def simulate(ids: dict, rng: random.Random, shelf: dict) -> tuple[World, dict]:
    """Run the plant day by day. Pure Python, no database."""
    w = World()
    P, U = ids["party"], ids["unit"]
    pools = {c: Pool(ids["reefers"][c]) for c in CARRIERS}
    raw_milk: list[dict] = []
    raw_pulp: list[dict] = []
    ready: list[dict] = []                         # finished lots waiting for the trunk run
    at_hub: dict[str, list[dict]] = {h: [] for h in HUBS}
    marks = {"hist_fail": [], "demo_fail": None, "inflight": []}
    hub_cycle = 0
    batch_no = 0

    def mark_raw(k: int, i: int) -> dict:
        """Which raw lots are scripted to fail a lab test."""
        if k in (DAYS // 3, DAYS // 2 + 1) and i == (1 if k == DAYS // 3 else 0):
            return {"fail": "hist"}
        if k == DAYS - 5 and i == 1:      # confirmatory results can take days
            return {"fail": "demo"}
        return {}

    for k in range(DAYS):
        mid = LAST_MIDNIGHT - timedelta(days=DAYS - 1 - k)

        # -- inbound raw material --------------------------------------------
        inbound = [(s, f"RM", RAW_MILK_LOT, "RAW-MILK", mid + timedelta(minutes=30 * i),
                    timedelta(hours=3 + 0.5 * i), i) for i, (s, _) in enumerate(MILK_SUPPLIERS)]
        inbound.append((PULP_SUPPLIER[0], "RP", PULP_LOT, "RAW-PULP", mid + timedelta(hours=1),
                        timedelta(hours=3), 9))
        for sup, prefix, qty, sku, dep, dur, i in inbound:
            arr = dep + dur
            unit = pools[CARRIERS[0]].take(dep, arr)
            sh = w.ship(P[sup], P[PLANT], P[CARRIERS[0]], unit, dep, arr)
            lot = w.lot(prefix, sku, P[sup], "RAW", qty, arr, arr + timedelta(days=shelf[sku]))
            lot["remaining"], lot["last_use"], lot["blocked_after"] = qty, arr, None
            w.stay(lot, unit, dep, arr, sh)
            lot["rm_stay"] = w.stay(lot, U["Plant RM Cold Store"], arr, None)
            (raw_milk if sku == "RAW-MILK" else raw_pulp).append(lot)
            m = mark_raw(k, i) if i < 9 else {}
            if m.get("fail") == "hist":
                lot["blocked_after"] = arr + timedelta(hours=30)
                marks["hist_fail"].append(lot)
            elif m.get("fail") == "demo":
                marks["demo_fail"] = lot

        # -- last-mile delivery of what reached the hubs yesterday evening ----
        for hub, (_, _, lm_hours) in HUBS.items():
            lots = [s for s in at_hub[hub] if s["created"] <= mid + timedelta(hours=6)]
            if not lots:
                continue
            taken = {s["id"] for s in lots}
            at_hub[hub] = [s for s in at_hub[hub] if s["id"] not in taken]
            shops = RETAILERS[hub]
            portions: dict[str, list[tuple[dict, float]]] = defaultdict(list)
            for t, s in enumerate(lots):
                for r in (shops[t % 4], shops[(t + 1) % 4]):
                    portions[r].append((s, s["qty"] / 2))
            last_depart: dict[int, datetime] = {}
            for j, shop in enumerate(shops):
                if shop not in portions:
                    continue
                dep = mid + timedelta(hours=6) + timedelta(minutes=45 * j)
                arr = dep + timedelta(hours=lm_hours)
                unit = pools[CARRIERS[2]].take(dep, arr)
                sh = w.ship(P[hub], P[shop], P[CARRIERS[2]], unit, dep, arr)
                for s, q in portions[shop]:
                    last_depart[s["id"]] = max(last_depart.get(s["id"], dep), dep)
                    w.stay(s, unit, dep, arr, sh)
                    sold = arr + timedelta(days=rng.uniform(2, 4) if s["sku"] == "FIN-MILK1L"
                                           else rng.uniform(4, 7))
                    sold = min(sold, s["expires"])
                    rs = w.lot("RS", s["sku"], P[shop], "STOCK", q, arr, s["expires"])
                    w.flow(s, rs, "shipped_as", q, sh)
                    w.stay(rs, U[f"{shop} Display"], arr, sold if sold <= NOW else None)
            for s in lots:
                s["hub_stay"]["to"] = last_depart.get(s["id"], s["created"] + H)

        # -- production -------------------------------------------------------
        for idx, start_h in enumerate(BATCH_STARTS):
            start = mid + timedelta(hours=start_h)
            if start > NOW:
                continue
            end = start + timedelta(hours=BATCH_HOURS)
            sku = BATCH_CYCLE[batch_no % 3]
            batch_no += 1

            def draw(pool: list[dict], need: float, cap: float | None) -> list[tuple[dict, float]]:
                live = sorted((r for r in pool if r["created"] <= start < r["expires"]
                               and r["remaining"] > 1
                               and (r["blocked_after"] is None or start < r["blocked_after"])),
                              key=lambda r: r["created"])
                taken = []
                for n, r in enumerate(live):
                    if need <= 0.5:
                        break
                    q = min(r["remaining"], need, cap if (cap and n == 0) else need)
                    taken.append((r, round(q, 2)))
                    need -= q
                return taken

            milk = draw(raw_milk, MILK_PER_BATCH, 6000.0)
            pulp = []
            if sku == "FIN-FYOG400":
                pulp = draw(raw_pulp, PULP_PER_FRUIT_BATCH, None)
                if not pulp:
                    sku = "FIN-YOG400"
            inputs = milk + pulp
            if sum(q for _, q in inputs) < 5000:
                continue
            total = sum(q for _, q in inputs)
            line = U["Pasteuriser Line A" if idx % 2 == 0 else "Pasteuriser Line B"]
            batch = w.lot("BT", sku, P[PLANT], "BATCH", total, start)
            for r, q in inputs:
                r["remaining"] = round(r["remaining"] - q, 2)
                r["last_use"] = max(r["last_use"], start)
                w.flow(r, batch, "consumed_in", q)
            w.stay(batch, line, start, end if end <= NOW else None)

            pack = end + H
            if pack > NOW:
                marks["inflight"].append(batch)
                continue
            n_fin = rng.choice([2, 3])
            cuts = [rng.uniform(0.7, 1.3) for _ in range(n_fin)]
            for c in cuts:
                qty = total * UNITS_PER_BATCH_UNIT[sku] * c / sum(cuts)
                f = w.lot("FG", sku, P[PLANT], "FINISHED", qty, pack, pack + timedelta(days=shelf[sku]))
                w.flow(batch, f, "packed_as", qty / UNITS_PER_BATCH_UNIT[sku])
                f["fg_stay"] = w.stay(f, U["Plant FG Cold Store"], pack, None)
                f["hub"] = list(HUBS)[hub_cycle % 2]
                hub_cycle += 1
                ready.append(f)

        # -- trunk run to the hubs --------------------------------------------
        for n, (hub, (_, trunk_h, _)) in enumerate(HUBS.items()):
            dep = mid + timedelta(hours=16, minutes=15 * n)
            lots = [f for f in ready if f["hub"] == hub and f["created"] <= dep]
            if not lots:
                continue
            taken = {f["id"] for f in lots}
            ready = [f for f in ready if f["id"] not in taken]
            arr = dep + timedelta(hours=trunk_h)
            unit = pools[CARRIERS[1]].take(dep, arr)
            sh = w.ship(P[PLANT], P[hub], P[CARRIERS[1]], unit, dep, arr)
            for f in lots:
                f["fg_stay"]["to"] = dep
                w.stay(f, unit, dep, arr if arr <= NOW else None, sh)
                if arr > NOW:
                    marks.setdefault("on_truck", []).append(f)
                    continue
                s = w.lot("DS", f["sku"], P[hub], "STOCK", f["qty"], arr, f["expires"])
                w.flow(f, s, "shipped_as", f["qty"], sh)
                s["hub_stay"] = w.stay(s, U[f"{hub} Cold Room"], arr, None)
                at_hub[hub].append(s)

    # -- close the raw-material stays -----------------------------------------
    for r in raw_milk + raw_pulp:
        if r["remaining"] <= 1:
            r["rm_stay"]["to"] = r["last_use"]
        elif r["expires"] <= NOW:
            r["rm_stay"]["to"] = r["expires"]
    return w, marks


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_reference(cur) -> dict:
    cur.execute("TRUNCATE parties, products, units, lots, lot_flows, shipments, custody, "
                "quality_events, incidents, incident_reports, report_bench, audit_logs, "
                "demo_injection, sensor_readings, telemetry_windows, ledger_tx, blocks "
                "RESTART IDENTITY CASCADE")
    P: dict[str, int] = {}

    def party(name, ptype, city):
        cur.execute("INSERT INTO parties(name, party_type, city) VALUES (%s,%s,%s) RETURNING party_id",
                    (name, ptype, city))
        P[name] = cur.fetchone()[0]

    for n, c in MILK_SUPPLIERS + [PULP_SUPPLIER]:
        party(n, "Supplier", c)
    party(PLANT, "Manufacturer", "Vellore")
    for c in CARRIERS:
        party(c, "Carrier", "Vellore")
    for h, (city, _, _) in HUBS.items():
        party(h, "Distributor", city)
        for r in RETAILERS[h]:
            party(r, "Retailer", city)

    shelf = {}
    for sku, name, cat, lo, hi, days in PRODUCTS:
        cur.execute("INSERT INTO products(sku, name, category, temp_min, temp_max, shelf_life_days) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (sku, name, cat, lo, hi, days))
        shelf[sku] = days

    U: dict[str, int] = {}
    reefers: dict[str, list[int]] = {}

    def unit(name, utype, owner, sp, lo, hi):
        cur.execute("INSERT INTO units(name, unit_type, party_id, setpoint, temp_min, temp_max) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING unit_id", (name, utype, P[owner], sp, lo, hi))
        U[name] = cur.fetchone()[0]
        return U[name]

    unit("Plant RM Cold Store", "ColdRoom", PLANT, 3.0, 1.0, 5.0)
    unit("Plant FG Cold Store", "ColdRoom", PLANT, 3.5, 2.0, 6.0)
    unit("Pasteuriser Line A", "ProcessLine", PLANT, 72.5, 71.8, 76.0)
    unit("Pasteuriser Line B", "ProcessLine", PLANT, 72.5, 71.8, 76.0)
    for h in HUBS:
        unit(f"{h} Cold Room", "ColdRoom", h, 3.5, 2.0, 6.0)
        for r in RETAILERS[h]:
            unit(f"{r} Display", "DisplayCase", r, 4.0, 2.0, 6.0)
    for carrier, tag, count, sp, lo, hi in [(CARRIERS[0], "CL", 4, 3.0, 1.0, 5.0),
                                            (CARRIERS[1], "FL", 3, 4.0, 2.0, 6.0),
                                            (CARRIERS[2], "SC", 8, 4.0, 2.0, 6.0)]:
        reefers[carrier] = [unit(f"{tag}-RF-{i:02d}", "Reefer", carrier, sp, lo, hi)
                            for i in range(1, count + 1)]
    return {"party": P, "unit": U, "reefers": reefers, "shelf": shelf}


def persist(cur, w: World, ids: dict) -> None:
    cur.execute("SELECT sku, product_id FROM products")
    prod = dict(cur.fetchall())
    cur.executemany(
        "INSERT INTO shipments(shipment_id, shipment_code, origin_party, dest_party, carrier_party, "
        "unit_id, depart_time, arrive_time) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        [(s["id"], s["code"], s["origin"], s["dest"], s["carrier"], s["unit"], s["dep"], s["arr"])
         for s in w.ships])
    cur.executemany(
        "INSERT INTO lots(lot_id, lot_code, product_id, party_id, lot_type, quantity, created_at, "
        "expires_at, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(l["id"], l["code"], prod[l["sku"]], l["party"], l["type"], l["qty"], l["created"],
          l["expires"], l["status"]) for l in w.lots])
    cur.executemany(
        "INSERT INTO lot_flows(from_lot, to_lot, flow_type, quantity, shipment_id) VALUES (%s,%s,%s,%s,%s)",
        [(f["src"], f["dst"], f["type"], f["qty"], f["ship"]) for f in w.flows])
    cur.executemany(
        "INSERT INTO custody(lot_id, unit_id, shipment_id, from_time, to_time) VALUES (%s,%s,%s,%s,%s)",
        [(c["lot"], c["unit"], c["ship"], c["frm"], c["to"]) for c in w.cust])
    cur.execute("SELECT setval(pg_get_serial_sequence('lots','lot_id'), (SELECT max(lot_id) FROM lots))")
    cur.execute("SELECT setval(pg_get_serial_sequence('shipments','shipment_id'), "
                "(SELECT max(shipment_id) FROM shipments))")


def simulate_readings(cur) -> int:
    """Server-side: generate_series per unit. Millions of Python round-trips would
    be the difference between seconds and an hour."""
    first = LAST_MIDNIGHT - timedelta(days=DAYS - 1)
    shape = """
        round((u.setpoint
          + CASE u.unit_type WHEN 'DisplayCase' THEN 0.40 WHEN 'ColdRoom' THEN 0.20 ELSE 0 END
            * sin(extract(epoch FROM t) / 86400 * 2 * pi())
          + CASE u.unit_type WHEN 'Reefer' THEN 0.35 WHEN 'ColdRoom' THEN 0.25
                             WHEN 'DisplayCase' THEN 0.30 ELSE 0.15 END
            * (random() + random() + random() - 1.5) * 2)::numeric, 2)::real,
        round((62 + random() * 14)::numeric, 1)::real"""
    cur.execute(f"""
        INSERT INTO sensor_readings (time, unit_id, temp_c, humidity)
        SELECT t, u.unit_id, {shape}
        FROM units u CROSS JOIN generate_series(%s::timestamptz, %s::timestamptz, interval '1 minute') t
        WHERE u.unit_type <> 'Reefer'""", (first, NOW))
    cur.execute(f"""
        INSERT INTO sensor_readings (time, unit_id, temp_c, humidity)
        SELECT t, u.unit_id, {shape}
        FROM shipments s JOIN units u ON u.unit_id = s.unit_id
        CROSS JOIN LATERAL generate_series(s.depart_time,
                           least(s.arrive_time - interval '1 minute', %s::timestamptz),
                           interval '1 minute') t
        WHERE s.depart_time <= %s::timestamptz""", (NOW, NOW))
    cur.execute("SELECT count(*) FROM sensor_readings")
    return cur.fetchone()[0]


def descendants(w: World, root: dict) -> list[dict]:
    kids = defaultdict(list)
    for f in w.flows:
        kids[f["src"]].append(f["dst"])
    by_id = {l["id"]: l for l in w.lots}
    seen, queue, out = {root["id"]}, deque([root["id"]]), []
    while queue:
        cur_id = queue.popleft()
        for nxt in kids[cur_id]:
            if nxt not in seen:
                seen.add(nxt)
                out.append(by_id[nxt])
                queue.append(nxt)
    return out


def add_quality(w: World, marks: dict, rng: random.Random) -> None:
    """A rapid-kit screen on every raw lot; scripted confirmatory failures on three."""
    hist_ids = {l["id"] for l in marks["hist_fail"]}
    demo = marks["demo_fail"]
    for l in w.lots:
        if l["type"] != "RAW":
            continue
        milk = l["sku"] == "RAW-MILK"
        name = "Antibiotic residue, rapid kit (ppb)" if milk else "Pesticide residue (mg/kg)"
        limit = 4.0 if milk else 0.5
        w.quality.append(dict(lot=l["id"], test=name, result="PASS",
                              measured=round(rng.uniform(0.1, 0.4) * limit, 3), limit=limit,
                              at=l["created"] + timedelta(hours=1, minutes=rng.randint(0, 40)),
                              note="Screened on receipt"))
        if l["id"] in hist_ids:
            w.quality.append(dict(lot=l["id"], test="Antibiotic residue, confirmatory (ppb)",
                                  result="FAIL", measured=9.6, limit=4.0,
                                  at=l["created"] + timedelta(hours=28),
                                  note="Confirmatory lab result, lot already blended into production"))
        if demo is not None and l["id"] == demo["id"]:
            w.quality.append(dict(lot=l["id"], test="Antibiotic residue, confirmatory (ppb)",
                                  result="FAIL", measured=11.2, limit=4.0, at=NOW - H,
                                  note="Confirmatory result reported 5 days after receipt by the external lab"))


def persist_quality_and_status(cur, w: World, marks: dict) -> list[dict]:
    """Quality rows, then historical recalls (status + ledger STATUS_CHANGE txs)."""
    cur.executemany(
        "INSERT INTO quality_events(lot_id, test_name, result, measured, limit_value, tested_at, note) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        [(q["lot"], q["test"], q["result"], q["measured"], q["limit"], q["at"], q["note"])
         for q in w.quality])
    status_txs = []
    for root in marks["hist_fail"]:
        fail_at = next(q["at"] for q in w.quality if q["lot"] == root["id"] and q["result"] == "FAIL")
        for lot in [root] + descendants(w, root):
            at = max(fail_at + 2 * H, lot["created"]) + timedelta(minutes=1)
            payload, signers = chain.status_payload(
                lot["code"], "OK", "RECALLED",
                f"Supplier lot {root['code']} failed the confirmatory antibiotic test", PLANT)
            status_txs.append(chain.make_tx("STATUS_CHANGE", lot["code"], chain.ms(at), payload, signers))
            lot["status"] = "RECALLED"
    cur.execute("ALTER TABLE lots DISABLE TRIGGER trg_lot_audit")
    cur.executemany("UPDATE lots SET status = 'RECALLED' WHERE lot_code = %s",
                    [(l["code"],) for l in w.lots if l["status"] == "RECALLED"])
    cur.execute("ALTER TABLE lots ENABLE TRIGGER trg_lot_audit")
    return status_txs


def persist_incidents(cur, w: World, ids: dict, marks: dict, rng: random.Random) -> int:
    """Historic incident memory with embedded reports."""
    cur.execute("SELECT unit_id, unit_type FROM units")
    by_type = defaultdict(list)
    for uid, ut in cur.fetchall():
        by_type[ut].append(uid)
    raws = [l for l in w.lots if l["type"] == "RAW" and l["created"] < NOW - 3 * 24 * H]
    kinds = (["REEFER_FAULT"] * 110 + ["COLDROOM_FAULT"] * 110 + ["PROCESS_DEVIATION"] * 70
             + ["SUPPLIER_QUALITY"] * 60 + ["SENSOR_FAULT"] * 70)
    rng.shuffle(kinds)
    rows = []
    hist_lots = [l["id"] for l in marks["hist_fail"]]
    for kind in kinds:
        when = NOW - timedelta(hours=rng.uniform(8, DAYS * 24 - 4))
        unit = lot = None
        if kind == "REEFER_FAULT":
            unit = rng.choice(by_type["Reefer"])
        elif kind == "COLDROOM_FAULT":
            unit = rng.choice(by_type["ColdRoom"] + by_type["DisplayCase"])
        elif kind == "PROCESS_DEVIATION":
            unit = rng.choice(by_type["ProcessLine"])
        elif kind == "SUPPLIER_QUALITY":
            lot = hist_lots.pop() if hist_lots else rng.choice(raws)["id"]
        else:
            unit = rng.choice([u for v in by_type.values() for u in v])
        cur.execute("INSERT INTO incidents(kind, unit_id, lot_id, occurred_at) VALUES (%s,%s,%s,%s) "
                    "RETURNING incident_id", (kind, unit, lot, when))
        rows.append((cur.fetchone()[0], *compose_report(kind, rng)))
    print(f"  composed {len(rows)} reports, {len({r[1] for r in rows})} distinct symptom texts")
    print("  embedding report text ...")
    embedder = load_embedder()
    vecs = np.asarray(embedder.encode([f"{s}. {a}" for _, s, a, _ in rows],
                                      normalize_embeddings=True), dtype=np.float32)
    cur.executemany(
        "INSERT INTO incident_reports(incident_id, summary, action_taken, outcome, embedding) "
        "VALUES (%s,%s,%s,%s,%s)", [(i, s, a, o, v) for (i, s, a, o), v in zip(rows, vecs)])
    print(f"  embedder: {getattr(embedder, 'name', 'unknown')}")
    return len(rows)


def build_ledger(cur, status_txs: list[dict]) -> dict:
    txs = []
    for code, (payload, signers) in chain.lot_payloads(cur).items():
        txs.append(chain.make_tx("LOT_REGISTERED", code, payload["created_ms"], payload, signers))
    for code, (payload, signers) in chain.quality_payloads(cur, PLANT).items():
        txs.append(chain.make_tx("QUALITY_RESULT", code, payload["tested_ms"], payload, signers))
    txs.extend(status_txs)
    txs.sort(key=lambda t: (t["ts_ms"], t["tx_type"], t["subject"]))
    n_blocks = chain.append_txs(cur, txs)
    out = chain.anchor_pending(cur, NOW)
    return {"txs": len(txs), "blocks": n_blocks, **out}


def main() -> int:
    if DAYS < 8:
        print("DAYS must be at least 8: the scripted recalls need history to act on")
        return 1
    rng = random.Random(SEED)
    np.random.seed(SEED)
    conn = psycopg.connect(DB, autocommit=False)
    register_vector(conn)
    cur = conn.cursor()
    print(f"connected: {DB.rsplit('@', 1)[-1]}   plant clock anchored at NOW={NOW:%Y-%m-%d %H:%M}Z")

    t = log_step("1/7 clearing previous run")
    cur.execute("DROP MATERIALIZED VIEW IF EXISTS readings_hourly")
    cur.execute("DROP TABLE IF EXISTS sensor_readings_plain")
    ids = load_reference(cur)
    conn.commit()
    done(t)

    t = log_step("2/7 simulating the supply chain")
    w, marks = simulate(ids, rng, ids["shelf"])
    persist(cur, w, ids)
    conn.commit()
    by_type = defaultdict(int)
    for l in w.lots:
        by_type[l["type"]] += 1
    done(t, f"{len(w.lots)} lots ({', '.join(f'{v} {k}' for k, v in by_type.items())}), "
            f"{len(w.flows)} genealogy edges, {len(w.ships)} shipments, {len(w.cust)} custody intervals")

    t = log_step("3/7 simulating sensor readings (the slow part)")
    n_read = simulate_readings(cur)
    conn.commit()
    done(t, f"{n_read:,} readings across {sum(len(v) for v in ids['reefers'].values()) + 14} units")

    t = log_step("4/7 injecting live demo anomalies")
    from scripts.refresh_demo import inject_anomalies, shift
    active = [s for s in w.ships if s["dep"] <= NOW < s["arr"] and s["carrier"] == ids["party"][CARRIERS[1]]]
    plan = {ids["unit"]["Plant RM Cold Store"]: 4.5, ids["unit"]["Pasteuriser Line B"]: -3.0,
            ids["unit"]["FreshMart T Nagar Display"]: 6.0}
    if active:
        plan[active[0]["unit"]] = 7.0
    names = inject_anomalies(cur, plan, NOW)
    # A finished excursion: the Chennai hub cold room ran warm for five hours about
    # 40 hours ago, while it held product that was then delivered to four stores.
    # Injected before anchoring, so the ledger signs the excursion as it happened.
    hub_room = ids["unit"]["Chennai Distribution Hub Cold Room"]
    shift(cur, hub_room, NOW - timedelta(hours=44), NOW - timedelta(hours=39), 5.5)
    names.append("Chennai Distribution Hub Cold Room (+5.5 C, 44-39 h ago)")
    conn.commit()
    done(t, "anomalies on " + ", ".join(names))

    t = log_step("5/7 lab results, historical recalls, incident memory")
    add_quality(w, marks, rng)
    status_txs = persist_quality_and_status(cur, w, marks)
    n_inc = persist_incidents(cur, w, ids, marks, rng)
    conn.commit()
    demo = marks["demo_fail"]
    done(t, f"{len(w.quality)} lab results, {len(status_txs)} lots recalled historically, {n_inc} incident reports")

    t = log_step("6/7 benchmark vectors")
    vecs = np.random.randn(BENCH_VECTORS, EMBED_DIM).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    with cur.copy("COPY report_bench (embedding) FROM STDIN") as copy:
        for v in vecs:
            copy.write_row([v])
    conn.commit()
    done(t, f"{BENCH_VECTORS:,} random unit vectors")

    t = log_step(f"7/7 writing the ledger (difficulty {chain.DIFFICULTY})")
    info = build_ledger(cur, status_txs)
    conn.commit()
    cp = chain.save_checkpoint(cur)
    done(t, f"{info['txs']} business txs + {info['windows']} sensor digests + {info['shipments']} "
            f"custody transfers in {cur.execute('SELECT count(*) FROM blocks').fetchone()[0]} blocks; "
            f"checkpoint at height {cp['height']}")

    cur.execute(FORWARD, {"seeds": [demo["id"]], "since": None})
    kinds: dict[str, int] = defaultdict(int)
    for r in cur.fetchall():
        kinds[r[1]] += 1
    cur.execute("SELECT count(*) FROM audit_logs")
    print("\nseed complete.")
    print(f"  scripted demo recall: lot {demo['code']} (raw milk from Palar Dairy Farms) failed "
          f"its confirmatory antibiotic test an hour ago, five days after it arrived")
    print("  it traces forward to " + ", ".join(f"{v} {k}" for k, v in kinds.items()))
    if "STOCK" not in kinds:
        print("  WARNING: the scripted lot reaches no retail stock -- the recall demo would be empty")
    print(f"  audit rows {cur.fetchone()[0]} | next:  python run.py optimise   (then: verify, app)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
