"""
ChainTwin tests.

Part 1 needs no database: ledger primitives, the embedder, and the simulation's
invariants (run on the pure-Python world model).
Part 2 needs the seeded database and is skipped when it is not reachable.

    python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chain  # noqa: E402
import seed  # noqa: E402
import tamper  # noqa: E402
from embedding import VocabEmbedder, build_fallback_corpus, compose_report  # noqa: E402

DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5434/postgres")


# ===========================================================================
# Part 1 -- no database
# ===========================================================================
class TestLedgerPrimitives:
    def test_canonical_json_ignores_key_order(self):
        assert chain.canon({"b": 1, "a": [2, 1]}) == chain.canon({"a": [2, 1], "b": 1})

    def test_canonical_json_has_no_whitespace(self):
        assert " " not in chain.canon({"a": 1, "b": "x"})

    def test_merkle_root_single_leaf_and_empty(self):
        leaf = chain.sha("x")
        assert chain.merkle_root([leaf]) == leaf
        assert chain.merkle_root([]) == chain.sha("")

    def test_merkle_root_changes_if_any_leaf_changes(self):
        leaves = [chain.sha(str(i)) for i in range(7)]          # odd count on purpose
        base = chain.merkle_root(leaves)
        for i in range(len(leaves)):
            changed = list(leaves)
            changed[i] = chain.sha("tampered")
            assert chain.merkle_root(changed) != base

    def test_merkle_root_depends_on_order(self):
        a, b = chain.sha("a"), chain.sha("b")
        assert chain.merkle_root([a, b]) != chain.merkle_root([b, a])

    def test_signature_is_per_identity(self):
        h = chain.sha("tx")
        assert chain.sign("Kaveri Milk Co-op", h) != chain.sign("Palar Dairy Farms", h)
        assert chain.sign("Kaveri Milk Co-op", h) == chain.sign("Kaveri Milk Co-op", h)

    def test_make_tx_binds_signers_into_the_payload(self):
        tx = chain.make_tx("LOT_REGISTERED", "L1", 1000, {"a": 1}, ["B", "A"])
        assert json.loads(tx["payload"])["signers"] == ["A", "B"]
        assert set(json.loads(tx["signatures"])) == {"A", "B"}
        # Dropping a signer changes the payload, so the payload hash and tx hash change.
        other = chain.make_tx("LOT_REGISTERED", "L1", 1000, {"a": 1}, ["A"])
        assert other["payload_hash"] != tx["payload_hash"] and other["tx_hash"] != tx["tx_hash"]

    def test_mining_meets_difficulty_and_is_reproducible(self):
        nonce, h = chain.mine(3, "p" * 64, "r" * 64, 123, 3)
        assert h.startswith("000")
        assert chain.block_hash(3, "p" * 64, "r" * 64, nonce, 123) == h

    def test_reading_leaf_depends_on_every_field(self):
        from datetime import datetime, timezone
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        base = chain.reading_leaf(1, t, 4.12, 60.0)
        assert chain.reading_leaf(2, t, 4.12, 60.0) != base
        assert chain.reading_leaf(1, t, 4.13, 60.0) != base
        assert chain.reading_leaf(1, t, 4.12, 60.1) != base
        assert chain.reading_leaf(1, t.replace(minute=1), 4.12, 60.0) != base


@pytest.fixture(scope="module")
def emb():
    return VocabEmbedder(build_fallback_corpus())


class TestEmbedder:
    def test_vectors_are_384_dim_and_unit_length(self, emb):
        v = emb.encode("reefer compressor tripped", normalize_embeddings=True)
        assert v.shape == (384,) and abs(np.linalg.norm(v) - 1.0) < 1e-5

    def test_synonym_folding(self, emb):
        assert emb._tokens("the truck warmed up") == emb._tokens("the trailer warmed up")
        assert "contamination" in emb._tokens("antibiotic residue found")

    def test_similar_text_scores_above_unrelated(self, emb):
        q = emb.encode("truck compressor failed and the trailer warmed up")
        same = emb.encode("Reefer compressor tripped en route, return air temperature climbed")
        other = emb.encode("Probe reading disagrees with the reference thermometer")
        assert float(q @ same) > float(q @ other)

    def test_reports_are_varied(self):
        rng = random.Random(1)
        texts = {compose_report("REEFER_FAULT", rng)[0] for _ in range(60)}
        assert len(texts) > 30


@pytest.fixture(scope="module")
def world():
    """The pure-Python simulation, without a database."""
    P, U, R = {}, {}, {}
    names = ([n for n, _ in seed.MILK_SUPPLIERS] + [seed.PULP_SUPPLIER[0], seed.PLANT] + seed.CARRIERS
             + [h for h in seed.HUBS] + [r for rs in seed.RETAILERS.values() for r in rs])
    for i, n in enumerate(names, 1):
        P[n] = i
    n = 0

    def unit(name):
        nonlocal n
        n += 1
        U[name] = n
        return n

    for u in ["Plant RM Cold Store", "Plant FG Cold Store", "Pasteuriser Line A", "Pasteuriser Line B"]:
        unit(u)
    for h in seed.HUBS:
        unit(f"{h} Cold Room")
        for r in seed.RETAILERS[h]:
            unit(f"{r} Display")
    for carrier, tag, count in [(seed.CARRIERS[0], "CL", 4), (seed.CARRIERS[1], "FL", 3),
                                (seed.CARRIERS[2], "SC", 8)]:
        R[carrier] = [unit(f"{tag}-RF-{i:02d}") for i in range(1, count + 1)]
    ids = {"party": P, "unit": U, "reefers": R}
    shelf = {sku: days for sku, _, _, _, _, days in seed.PRODUCTS}
    w, marks = seed.simulate(ids, random.Random(7), shelf)
    return w, marks, ids


class TestSimulation:
    def test_every_non_raw_lot_has_a_source(self, world):
        w, _, _ = world
        fed = {f["dst"] for f in w.flows}
        assert all(l["id"] in fed for l in w.lots if l["type"] != "RAW")

    def test_batch_quantity_equals_consumed_inputs(self, world):
        w, _, _ = world
        into = defaultdict(float)
        for f in w.flows:
            if f["type"] == "consumed_in":
                into[f["dst"]] += f["qty"]
        for l in w.lots:
            if l["type"] == "BATCH":
                assert abs(into[l["id"]] - l["qty"]) < 0.05

    def test_raw_lots_are_never_over_consumed(self, world):
        w, _, _ = world
        used = defaultdict(float)
        for f in w.flows:
            if f["type"] == "consumed_in":
                used[f["src"]] += f["qty"]
        for l in w.lots:
            if l["type"] == "RAW":
                assert used[l["id"]] <= l["qty"] + 0.05

    def test_flow_graph_is_acyclic_and_goes_forward_in_time(self, world):
        w, _, _ = world
        by_id = {l["id"]: l for l in w.lots}
        for f in w.flows:
            assert by_id[f["src"]]["created"] <= by_id[f["dst"]]["created"]

    def test_custody_intervals_are_ordered(self, world):
        w, _, _ = world
        assert all(c["to"] is None or c["to"] >= c["frm"] for c in w.cust)

    def test_a_reefer_never_makes_two_trips_at_once(self, world):
        w, _, _ = world
        trips = defaultdict(list)
        for s in w.ships:
            trips[s["unit"]].append((s["dep"], s["arr"]))
        for spans in trips.values():
            spans.sort()
            assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))

    def test_nothing_is_created_in_the_future(self, world):
        w, _, _ = world
        assert all(l["created"] <= seed.NOW for l in w.lots)
        assert all(s["dep"] <= seed.NOW for s in w.ships)

    def test_the_demo_has_live_material(self, world):
        w, marks, _ = world
        assert any(s["dep"] <= seed.NOW < s["arr"] for s in w.ships), "a truck should be in transit"
        assert marks["inflight"], "a batch should still be on the pasteuriser"

    def test_scripted_failing_lots_exist_and_flow_downstream(self, world):
        w, marks, _ = world
        assert len(marks["hist_fail"]) == 2 and marks["demo_fail"] is not None
        for root in marks["hist_fail"] + [marks["demo_fail"]]:
            kinds = {l["type"] for l in seed.descendants(w, root)}
            assert {"BATCH", "FINISHED", "STOCK"} <= kinds

    def test_stock_flows_conserve_quantity(self, world):
        w, _, _ = world
        by_id = {l["id"]: l for l in w.lots}
        sent = defaultdict(float)
        for f in w.flows:
            if f["type"] == "shipped_as":
                sent[f["src"]] += f["qty"]
        for lot_id, q in sent.items():
            assert q <= by_id[lot_id]["qty"] + 0.05


# ===========================================================================
# Part 2 -- needs the seeded database
# ===========================================================================
def _connect():
    import psycopg
    try:
        c = psycopg.connect(DB, autocommit=True, connect_timeout=3)
    except Exception:
        return None
    return c


@pytest.fixture(scope="module")
def conn():
    c = _connect()
    if c is None:
        pytest.skip("database not reachable")
    cur = c.cursor()
    try:
        cur.execute("SELECT count(*) FROM blocks")
        if cur.fetchone()[0] == 0:
            pytest.skip("database not seeded")
    except Exception:
        pytest.skip("schema not applied")
    yield c
    c.close()


class TestLedgerOnDatabase:
    def test_untouched_database_verifies(self, conn):
        r = tamper.baseline(conn)
        assert r["ok"], tamper.summarise(r["problems"])

    @pytest.mark.parametrize("scenario", list(tamper.SCENARIOS))
    def test_every_tamper_scenario_is_caught(self, conn, scenario):
        r = tamper.run(conn, scenario)
        assert r["detected"], f"{scenario} was not detected"

    def test_tamper_scenarios_roll_back(self, conn):
        tamper.run(conn, "quantity")
        assert tamper.baseline(conn)["ok"]

    def test_rewrite_is_caught_by_signatures_and_checkpoint(self, conn):
        r = tamper.run(conn, "rewrite")
        levels = {p["level"] for p in r["problems"]}
        assert "tx" in levels, "forged transaction should fail signature verification"
        if chain.CHECKPOINT.exists():
            assert "checkpoint" in levels, "re-mined history should disagree with the saved checkpoint"

    def test_set_status_writes_db_and_ledger_together(self, conn, tmp_path, monkeypatch):
        import actions
        monkeypatch.setattr(chain, "CHECKPOINT", tmp_path / "cp.json")
        cur = conn.cursor()
        cur.execute("SELECT lot_id, lot_code, status FROM lots WHERE status = 'OK' ORDER BY lot_id LIMIT 2")
        lots = cur.fetchall()
        with conn.transaction(force_rollback=True):
            assert actions.set_status(conn, lots, "HOLD", "test") == 2
            cur.execute("SELECT count(*) FROM lots WHERE status = 'HOLD' AND lot_id = ANY(%s)",
                        ([l[0] for l in lots],))
            assert cur.fetchone()[0] == 2
            assert chain.verify_chain(cur)["ok"] and chain.verify_offchain(cur)["ok"]
            # releasing writes a second record and still verifies
            assert actions.set_status(conn, [(l[0], l[1], "HOLD") for l in lots], "OK", "test") == 2
            assert chain.verify_offchain(cur)["ok"]

    def test_recalled_lots_are_never_downgraded(self, conn, tmp_path, monkeypatch):
        import actions
        monkeypatch.setattr(chain, "CHECKPOINT", tmp_path / "cp.json")
        cur = conn.cursor()
        cur.execute("SELECT lot_id, lot_code, status FROM lots WHERE status = 'RECALLED' LIMIT 1")
        row = cur.fetchone()
        with conn.transaction(force_rollback=True):
            assert actions.set_status(conn, [row], "HOLD", "test") == 0
            assert actions.set_status(conn, [row], "OK", "test") == 0


class TestQueriesOnDatabase:
    def test_forward_trace_reaches_retail_and_shares_are_valid(self, conn):
        from queries import FORWARD
        cur = conn.cursor()
        cur.execute("SELECT lot_id FROM quality_events WHERE result = 'FAIL' AND lot_id IN "
                    "(SELECT lot_id FROM lots WHERE status = 'OK') LIMIT 1")
        seed_lot = cur.fetchone()[0]
        cur.execute(FORWARD, {"seeds": [seed_lot], "since": None})
        rows = cur.fetchall()
        assert {"BATCH", "FINISHED", "STOCK"} <= {r[1] for r in rows}
        assert all(0 < r[4] <= 1.0000001 for r in rows)

    def test_forward_trace_survives_a_cycle(self, conn):
        from queries import FORWARD
        cur = conn.cursor()
        with conn.transaction(force_rollback=True):
            cur.execute("SELECT from_lot, to_lot FROM lot_flows LIMIT 1")
            a, b = cur.fetchone()
            cur.execute("INSERT INTO lot_flows(from_lot,to_lot,flow_type,quantity) VALUES (%s,%s,'packed_as',1)",
                        (b, a))
            cur.execute(FORWARD, {"seeds": [a], "since": None})
            assert cur.fetchall()

    def test_since_filter_excludes_older_handoffs(self, conn):
        from queries import FORWARD
        cur = conn.cursor()
        cur.execute("SELECT lot_id FROM lots WHERE lot_type = 'RAW' ORDER BY created_at LIMIT 1")
        raw = cur.fetchone()[0]
        cur.execute(FORWARD, {"seeds": [raw], "since": None})
        everything = len(cur.fetchall())
        cur.execute(FORWARD, {"seeds": [raw], "since": "2999-01-01"})
        nothing = len(cur.fetchall())
        assert everything > 1 and nothing == 1          # only the seed itself

    def test_exposed_lots_follow_custody(self, conn):
        from queries import EXPOSED_LOTS
        cur = conn.cursor()
        cur.execute("""SELECT c.unit_id, c.lot_id, c.from_time FROM custody c
                       WHERE c.to_time IS NOT NULL ORDER BY c.custody_id LIMIT 1""")
        unit_id, lot_id, frm = cur.fetchone()
        cur.execute(EXPOSED_LOTS, {"unit": unit_id, "since": frm, "until": frm})
        assert lot_id in {r[0] for r in cur.fetchall()}
        cur.execute(EXPOSED_LOTS, {"unit": unit_id, "since": "1999-01-01", "until": "1999-01-02"})
        assert cur.fetchall() == []

    def test_every_stock_lot_traces_to_a_raw_lot(self, conn):
        from queries import BACKWARD
        cur = conn.cursor()
        cur.execute("SELECT lot_id FROM lots WHERE lot_type = 'STOCK' ORDER BY random() LIMIT 25")
        for (lot_id,) in cur.fetchall():
            cur.execute(BACKWARD, {"lot": lot_id})
            rows = cur.fetchall()
            assert any(r[1] == "RAW" for r in rows)
            raw_share = sum(r[6] for r in rows if r[1] == "RAW")
            assert abs(raw_share - 1.0) < 1e-6, "raw ancestors must account for the whole lot"

    def test_readings_not_in_default_partition(self, conn):
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM sensor_readings_default")
        assert cur.fetchone()[0] == 0
