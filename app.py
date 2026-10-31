"""
ChainTwin dashboard.

A supply-chain digital twin on one PostgreSQL database plus a tamper-evident ledger.

  Control tower     a unit leaves its limits -> which lots were inside -> what
                    downstream is touched -> who held custody -> have we seen it before
  Recall simulator  a lab result fails -> exact recall scope from lot genealogy
  Provenance        farm-to-shelf history of any lot, with temperatures
  Ledger            the blockchain: verify it, then try to break it
  Benchmark / Gap   what the structures buy, and what problem this solves

    streamlit run app.py
"""
from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
from pgvector.psycopg import register_vector

import auth
import chain
import services
import tamper
import ui_registry
from auth import AuthError, Principal
from db import session_scope
from embedding import load_embedder
from queries import (ANOMALY, BACKWARD, EXCURSION_LOG, EXPOSED_LOTS, FORWARD, JOURNEY,
                     LOT_LEDGER, NAIVE, SIMILAR, TREND, exposure_start)

ROOT = Path(__file__).resolve().parent
import settings  # noqa: E402  (loads .env; no secrets in source)
DB = settings.database_url()

st.set_page_config(page_title="ChainTwin", layout="wide", page_icon="🔗")


@st.cache_resource
def get_conn():
    c = psycopg.connect(DB, autocommit=True)
    c.execute("SET TIME ZONE 'UTC'")
    register_vector(c)
    return c


@st.cache_resource
def get_embedder():
    return load_embedder(quiet=True)


def q(sql: str, params=None) -> tuple[pd.DataFrame, float]:
    """Run a query, return the rows and the wall time in ms."""
    t0 = time.perf_counter()
    with get_conn().cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols), (time.perf_counter() - t0) * 1000.0


def utc(ts) -> str:
    return pd.Timestamp(ts).strftime("%d %b %H:%M")


TYPE_NAME = {"RAW": "Raw material", "BATCH": "Production batch", "FINISHED": "Finished lot",
             "STOCK": "Stock at hub / store"}
STATUS_ICON = {"OK": "", "HOLD": "⏸ HOLD", "RECALLED": "⛔ RECALLED"}


def impact_summary(df: pd.DataFrame, seeds: set[int]) -> None:
    """Counts of what a trace reached, split by whether it can still be pulled back."""
    down = df[~df["lot_id"].isin(seeds)]
    stock = down[down["lot_type"].isin(["FINISHED", "STOCK"])]
    still = stock[stock["in_chain"]]
    gone = stock[~stock["in_chain"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Lots downstream", f"{len(down):,}")
    c2.metric("Finished / stock lots", f"{len(stock):,}")
    c3.metric("Still in the chain", f"{len(still):,}", help="Can be pulled back from trucks, hubs and shelves")
    c4.metric("Already left the chain", f"{len(gone):,}", help="Sold or consumed: customers must be notified")


def show_trace(df: pd.DataFrame) -> None:
    d = df.copy()
    d["lot_type"] = d["lot_type"].map(TYPE_NAME)
    d["share"] = (d["share"] * 100).round(0).astype(int).astype(str) + " %"
    d["where"] = d["location"].fillna("left the chain")
    d["status"] = d["status"].map(STATUS_ICON)
    st.dataframe(
        d[["lot_code", "lot_type", "product", "share", "holder", "where", "quantity", "status", "depth"]]
        .rename(columns={"share": "affected", "depth": "hops"}),
        hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Sign-in. The JWT lives only in this browser session's state; every rerun verifies it
# and re-reads the user from the database, so a disabled account or a changed role
# takes effect on the next click.
# ---------------------------------------------------------------------------
def login_screen(notice: str | None = None) -> None:
    left, mid, right = st.columns([1, 1.2, 1])
    with mid:
        st.title("ChainTwin")
        st.caption("Supply-chain digital twin with a tamper-evident ledger. Sign in to continue.")
        if notice:
            st.warning(notice)
        if st.session_state.get("failed", 0) >= 5:
            st.error("Too many failed attempts in this session. Reload the page to try again.")
            return
        with st.form("login"):
            email = st.text_input("Email", placeholder="you@company.com")
            password = st.text_input("Password", type="password")
            go = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if go:
            if not email.strip() or not password:
                st.error("Enter both email and password.")
                return
            try:
                with session_scope() as sess:
                    token = auth.create_token(auth.authenticate(sess, email, password))
            except AuthError as exc:
                st.session_state["failed"] = st.session_state.get("failed", 0) + 1
                st.error(str(exc))
                return
            except Exception:  # noqa: BLE001
                st.error("Cannot reach the database. Start it with `python run.py up`.")
                return
            st.session_state["token"] = token
            st.session_state["failed"] = 0
            st.rerun()


def current_principal() -> tuple[Principal | None, str | None]:
    token = st.session_state.get("token")
    if not token:
        return None, None
    try:
        with session_scope() as sess:
            return auth.principal_from_token(sess, token), None
    except AuthError as exc:
        st.session_state.pop("token", None)
        return None, str(exc)


principal, why = current_principal()
if principal is None:
    login_screen(why)
    st.stop()

with st.sidebar:
    st.markdown(f"**{principal.full_name}**  \n{principal.email}")
    st.markdown(f"Role: `{principal.role}`")
    with st.expander("Session (authenticated user info)"):
        claims = auth.decode_token(st.session_state["token"])
        st.json({"sub": claims["sub"], "email": claims["email"], "role": claims["role"],
                 "expires": pd.Timestamp(claims["exp"], unit="s", tz="UTC").strftime("%d %b %H:%M UTC"),
                 "permissions": sorted(auth.PERMISSIONS[principal.role])})
    if st.button("Sign out", use_container_width=True):
        st.session_state.pop("token", None)
        st.rerun()


def change_status(df: pd.DataFrame, new_status: str, reason: str) -> int:
    """Hold / release through the ORM service: the update and the signed ledger block commit together."""
    with session_scope() as sess:
        n = services.set_lot_status(sess, principal, [int(x) for x in df["lot_id"]], new_status, reason)
    if n:
        services.save_ledger_checkpoint()
    return n


can_change = principal.can("status:change")

# ---------------------------------------------------------------------------
st.title("ChainTwin")
st.caption(
    "A lot-level digital twin of a dairy supply chain: sensors, genealogy and custody "
    "in one PostgreSQL database, with a blockchain ledger that makes the records "
    "tamper-evident across companies. Times are UTC.")

try:
    meta, _ = q("""
        SELECT (SELECT count(*) FROM parties)          AS parties,
               (SELECT count(*) FROM lots)             AS lots,
               (SELECT count(*) FROM sensor_readings)  AS readings,
               (SELECT count(*) FROM blocks)           AS blocks,
               (SELECT count(*) FROM ledger_tx)        AS txs""")
except Exception as exc:
    st.error(f"Cannot reach the database at {DB.rsplit('@', 1)[-1]}")
    st.code(str(exc))
    st.info("Start it with `python run.py up`, then `python run.py all`.")
    st.stop()

m = meta.iloc[0]
chain_state = chain.verify_chain(get_conn().cursor())
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Parties", f"{m['parties']:,}")
k2.metric("Lots tracked", f"{m['lots']:,}")
k3.metric("Sensor readings", f"{m['readings']:,}")
k4.metric("Ledger blocks", f"{m['blocks']:,}", f"{m['txs']:,} transactions", delta_color="off")
k5.metric("Ledger", "✅ intact" if chain_state["ok"] else "❌ BROKEN")

pages = auth.visible_pages(principal)           # role-based menu: a role only gets its own pages
tabs = dict(zip(pages, st.tabs(pages)))
tab1, tab2, tab3, tab4 = (tabs["Control tower"], tabs["Recall simulator"], tabs["Provenance"], tabs["Ledger"])
tab5, tab6 = tabs["Benchmark"], tabs["The gap"]

# ===========================================================================
def control_tower() -> None:
    anomalies, t_anom = q(ANOMALY)
    st.subheader("1 · Units behaving abnormally right now")
    st.caption(f"Time-series layer · hourly roll-up for each unit's own baseline, raw rows for the "
               f"last hour, z-score per unit · **{t_anom:.0f} ms**")
    if anomalies.empty:
        st.success("Every unit is within 3 standard deviations of its own baseline.")
        st.caption("Run `python run.py demo` to re-arm the demo anomalies against the current clock.")
    else:
        st.dataframe(anomalies, hide_index=True, use_container_width=True)

    log, _ = q(EXCURSION_LOG)
    st.subheader("2 · Out-of-spec episodes, last 72 hours")
    if log.empty:
        st.info("No unit left its limits in the last 72 hours.")
        return
    show = log.copy()
    show["first_breach"] = show["first_breach"].map(utc)
    show["last_breach"] = show["last_breach"].map(utc)
    st.dataframe(show[["name", "unit_type", "operator", "first_breach", "last_breach", "minutes_out",
                       "peak_c", "low_c", "temp_min", "temp_max"]], hide_index=True, use_container_width=True)

    labels = [f"{r.name} · {utc(r.first_breach)} → {utc(r.last_breach)}" for r in log.itertuples()]
    pick = st.selectbox("Investigate", labels)
    ep = log.iloc[labels.index(pick)]
    unit_id, since, until = int(ep["unit_id"]), ep["first_breach"], ep["last_breach"]
    live = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(until)) < pd.Timedelta(minutes=10)
    until_q = pd.Timestamp.now(tz="UTC") if live else until

    trend, _ = q(TREND, {"unit": unit_id, "start": pd.Timestamp(since) - pd.Timedelta(hours=3),
                         "end": pd.Timestamp(until) + pd.Timedelta(hours=2)})
    if not trend.empty:
        st.caption(f"{ep['name']} · temperature around the episode (limits {ep['temp_min']}–{ep['temp_max']} °C)")
        st.line_chart(trend.set_index("t")[["temp_c"]], height=220)

    exposed, t_exp = q(EXPOSED_LOTS, {"unit": unit_id, "since": since, "until": until_q})
    left, right = st.columns(2)

    with left:
        st.subheader("3 · Lots that were inside it")
        st.caption(f"Custody join · lots whose chain of custody overlaps the episode · **{t_exp:.0f} ms**")
        if exposed.empty:
            st.info("No lot was in this unit during the episode.")
        else:
            e = exposed.copy()
            e["lot_type"] = e["lot_type"].map(TYPE_NAME)
            e["status"] = e["status"].map(STATUS_ICON)
            st.dataframe(e[["lot_code", "lot_type", "product", "quantity", "exposed_min", "status"]],
                         hide_index=True, use_container_width=True)
            st.caption(f"Operator at the time: **{ep['operator']}** — see the Ledger tab for the signed "
                       f"hand-offs and sensor digests that prove custody.")

    with right:
        st.subheader("4 · Seen this before?")
        kind = {"Reefer": "reefer truck", "ColdRoom": "cold room", "DisplayCase": "display case shelf",
                "ProcessLine": "pasteuriser hold temperature"}[ep["unit_type"]]
        verb = "dropped below" if ep["low_c"] < ep["temp_min"] and ep["peak_c"] <= ep["temp_max"] else "rose to"
        default = f"{kind.capitalize()} temperature {verb} {ep['low_c'] if verb.startswith('dropped') else ep['peak_c']} C for {ep['minutes_out']} minutes"
        symptom = st.text_input("Describe the problem", default)
        if symptom.strip():
            vec = get_embedder().encode(symptom, normalize_embeddings=True)
            sim, t_sim = q(SIMILAR, (vec, vec))
            st.caption(f"Vector layer · pgvector HNSW · **{t_sim:.0f} ms** · embedder `{getattr(get_embedder(), 'name', '?')}`")
            for _, r in sim.iterrows():
                with st.container(border=True):
                    st.markdown(f"**{r['kind'].replace('_', ' ').title()}** · {r['subject']} · similarity {r['similarity']}")
                    st.write(r["summary"])
                    st.caption(f"Action: {r['action_taken']}  \nOutcome: {r['outcome']}")

    if not exposed.empty:
        st.subheader("5 · What else is affected downstream")
        seeds = set(int(x) for x in exposed["lot_id"])
        trace, t_tr = q(FORWARD, {"seeds": list(seeds), "since": since})
        st.caption(f"Graph layer · recursive CTE over lot_flows, following only hand-offs made after "
                   f"{utc(since)} · **{t_tr:.0f} ms**")
        impact_summary(trace, seeds)
        down = trace[~trace["lot_id"].isin(seeds)]
        if down.empty:
            st.info("Nothing downstream yet: this was caught before the affected lots moved on. "
                    "Holding the lots above stops any spread.")
        else:
            show_trace(down)

        act1, act2, _ = st.columns([1, 1, 3])
        todo = pd.concat([exposed[["lot_id", "lot_code", "status"]],
                          trace[["lot_id", "lot_code", "status"]]]).drop_duplicates("lot_id")
        todo = todo[todo["status"] == "OK"]
        if act1.button(f"Place {len(todo)} lots on HOLD", disabled=todo.empty or not can_change, type="primary",
                       help="Updates the lots and writes a signed block to the ledger in one transaction"):
            n = change_status(todo, "HOLD", f"Excursion on {ep['name']} from {utc(since)}")
            st.success(f"{n} lots held and recorded on the ledger.")
            st.rerun()
        held = pd.concat([exposed[["lot_id", "lot_code", "status"]],
                          trace[["lot_id", "lot_code", "status"]]]).drop_duplicates("lot_id")
        held = held[held["status"] == "HOLD"]
        if act2.button(f"Release {len(held)} held lots", disabled=held.empty or not can_change):
            change_status(held, "OK", f"Cleared after review of {ep['name']}")
            st.rerun()
        if not can_change:
            st.caption("Your role is read-only: holds and releases need MANAGER or ADMIN.")


with tab1:
    control_tower()

# ===========================================================================
with tab2:
    st.subheader("Lab result fails → exact recall scope")
    st.caption("Recursive CTE over the lot genealogy, weighted by how much of each downstream lot "
               "came from the bad one (mass balance).")
    fails, _ = q("""SELECT l.lot_id, l.lot_code, l.status, q.test_name, q.measured, q.limit_value, q.tested_at,
                           pa.name AS supplier, l.created_at
                    FROM quality_events q JOIN lots l USING (lot_id) JOIN parties pa ON pa.party_id = l.party_id
                    WHERE q.result = 'FAIL' ORDER BY q.tested_at DESC""")
    if fails.empty:
        st.info("No failed lab results on record.")
    else:
        opts = [f"{r.lot_code} · {r.supplier} · {r.test_name} {r.measured} > {r.limit_value} · {STATUS_ICON[r.status] or 'open'}"
                for r in fails.itertuples()]
        idx = opts.index(st.selectbox("Failed lab result", opts))
        bad = fails.iloc[idx]
        st.warning(f"**{bad['lot_code']}** ({bad['supplier']}) arrived {utc(bad['created_at'])} and failed "
                   f"**{bad['test_name']}** at {bad['measured']} (limit {bad['limit_value']}), "
                   f"reported {utc(bad['tested_at'])}.")

        trace, t_tr = q(FORWARD, {"seeds": [int(bad["lot_id"])], "since": None})
        naive, t_nv = q(NAIVE, {"seeds": [int(bad["lot_id"])]})
        seeds = {int(bad["lot_id"])}
        down = trace[~trace["lot_id"].isin(seeds)]
        tgt = down[down["lot_type"].isin(["FINISHED", "STOCK"])]

        st.markdown("#### Targeted recall vs. recalling everything made since the lot arrived")
        a, b, c = st.columns(3)
        a.metric("With lot genealogy", f"{len(tgt):,} lots", f"{int(tgt['in_chain'].sum())} still in the chain", delta_color="off")
        b.metric("Without it", f"{len(naive):,} lots", f"{int(naive['in_chain'].sum())} still in the chain", delta_color="off")
        saved = len(naive) - len(tgt)
        c.metric("Avoided over-recall", f"{saved:,} lots", f"{(saved / len(naive) * 100 if len(naive) else 0):.0f}% fewer", delta_color="off")
        st.caption(f"Without genealogy you cannot tell which batches consumed the lot, so the only safe scope is "
                   f"everything produced since it arrived. That is an assumption, stated openly; queries took "
                   f"{t_tr:.0f} ms and {t_nv:.0f} ms.")

        impact_summary(trace, seeds)
        shelf = tgt[(tgt["lot_type"] == "STOCK") & tgt["in_chain"]].copy()
        if not shelf.empty:
            shelf["units_at_risk"] = (shelf["quantity"].astype(float) * shelf["share"]).round(0)
            by_holder = shelf.groupby("holder")["units_at_risk"].sum().sort_values(ascending=False)
            st.markdown("#### Units still to pull, by location")
            st.bar_chart(by_holder, height=240)
        st.markdown("#### Every lot reached")
        show_trace(down)

        todo = trace[(trace["status"] == "OK") & trace["in_chain"]]
        if st.button(f"Hold {len(todo)} lots still in the chain and record on ledger", type="primary",
                     disabled=todo.empty or not can_change, key="hold_recall"):
            n = change_status(todo, "HOLD", f"Downstream of {bad['lot_code']} ({bad['test_name']})")
            st.success(f"{n} lots held and recorded on the ledger.")
            st.rerun()

# ===========================================================================
with tab3:
    st.subheader("Farm-to-shelf provenance")
    st.caption("Backward recursive CTE plus each custody leg's temperature range, with the ledger records behind it.")
    shelf, _ = q("""SELECT l.lot_id, l.lot_code, pr.name AS product, pa.name AS store, l.created_at
                    FROM lots l JOIN products pr USING (product_id) JOIN parties pa ON pa.party_id = l.party_id
                    WHERE l.lot_type = 'STOCK' AND pa.party_type = 'Retailer'
                      AND EXISTS (SELECT 1 FROM custody c WHERE c.lot_id = l.lot_id AND c.to_time IS NULL)
                    ORDER BY l.created_at DESC LIMIT 60""")
    opts = [f"{r.lot_code} · {r.product} · {r.store}" for r in shelf.itertuples()]
    if not opts:
        st.info("No retail stock on shelf.")
    else:
        row = shelf.iloc[opts.index(st.selectbox("Scan a lot on a store shelf", opts))]
        back, _ = q(BACKWARD, {"lot": int(row["lot_id"])})
        src = back[back["lot_type"] == "RAW"]
        st.markdown(f"**{row['product']}** at **{row['store']}** was made from:")
        for r in src.itertuples():
            st.write(f"- {r.share_of_lot * 100:.0f}% **{r.product}** — lot {r.lot_code} from {r.holder}, "
                     f"received {utc(r.created_at)}")
        journey, t_j = q(JOURNEY, {"lots": [int(x) for x in back["lot_id"]]})
        st.markdown("#### Custody legs and temperatures")
        j = journey.copy()
        j["from_time"] = j["from_time"].map(utc)
        j["to_time"] = j["to_time"].map(lambda x: "now" if pd.isna(x) else utc(x))
        j["min_c"] = j["min_c"].astype(float).round(1)
        j["max_c"] = j["max_c"].astype(float).round(1)
        j["in spec"] = j["out_of_spec"].map({True: "❌", False: "✅"})
        st.dataframe(j[["lot_code", "unit", "operator", "from_time", "to_time", "min_c", "max_c", "in spec"]],
                     hide_index=True, use_container_width=True)
        st.caption(f"{int(j['out_of_spec'].sum())} legs out of spec · {t_j:.0f} ms")
        proof, _ = q(LOT_LEDGER, {"code": row["lot_code"]})
        with st.expander(f"Ledger records for {row['lot_code']} ({len(proof)})"):
            st.dataframe(proof.assign(tx_hash=proof["tx_hash"].str[:16] + "…",
                                      block_hash=proof["block_hash"].str[:16] + "…"),
                         hide_index=True, use_container_width=True)

# ===========================================================================
with tab4:
    st.subheader("The ledger")
    st.markdown(
        "Business data stays in the relational tables. The ledger holds **hashes and signatures** of it: "
        "every lot registration, every hand-off (signed by sender, receiver *and* carrier), every lab result, "
        "every status change, and a **Merkle digest of each sensor window**. Blocks are chained by hash and "
        "sealed with proof of work; each party's signature is checked against its own key. "
        "Changing a record, a thermometer reading or a block afterwards is detected.")
    cur = get_conn().cursor()
    blocks, _ = q("""SELECT height, tx_count, left(block_hash, 16) || '…' AS block_hash,
                            left(prev_hash, 16) || '…' AS prev_hash, nonce, difficulty,
                            to_timestamp(ts_ms / 1000.0) AS sealed
                     FROM blocks ORDER BY height DESC LIMIT 12""")
    types, _ = q("SELECT tx_type, count(*) AS n FROM ledger_tx GROUP BY 1 ORDER BY 2 DESC")
    c1, c2 = st.columns([3, 2])
    c1.markdown("**Latest blocks**")
    c1.dataframe(blocks, hide_index=True, use_container_width=True)
    c2.markdown("**Transactions by type**")
    c2.bar_chart(types.set_index("tx_type"), height=260)

    st.markdown("#### Verify")
    v1, v2 = st.columns(2)
    if v1.button("Verify the chain (fast)"):
        res, ms_ = chain.timed(chain.verify_chain, cur)
        if res["ok"]:
            st.success(f"Intact: {res['blocks']} blocks, {res['txs']} transactions, every hash, Merkle root "
                       f"and signature checks out ({ms_:.0f} ms).")
        else:
            st.error(f"{len(res['problems'])} problems")
            st.dataframe(pd.DataFrame(res["problems"]), hide_index=True, use_container_width=True)
    if v2.button("Verify the database against the ledger"):
        with st.spinner("Recomputing every signed record and sensor digest…"):
            res, ms_ = chain.timed(chain.verify_offchain, cur)
        if res["ok"]:
            n = res["counts"]
            st.success(f"Database matches what was signed: {n['lots']:,} lots, {n['shipments']:,} hand-offs, "
                       f"{n['lab_results']:,} lab results, {n['sensor_windows']:,} sensor windows "
                       f"({res['readings_anchored']:,} of {res['readings_total']:,} readings anchored; the rest are "
                       f"still inside their {chain.SETTLE_HOURS} h settle window) in {ms_ / 1000:.1f} s.")
        else:
            st.error(f"{len(res['problems'])} records differ from what was signed")
            st.dataframe(pd.DataFrame(res["problems"]), hide_index=True, use_container_width=True)

    st.markdown("#### Try to break it")
    if not principal.can("tamper:run"):
        st.info("Running attacks is limited to the ADMIN role.")
    st.caption("Each scenario plays an attacker with full SQL access — they can even disable the append-only "
               "trigger. The change is made inside a transaction that is rolled back afterwards, so nothing is "
               "left altered.")
    key = st.selectbox("Attack", list(tamper.SCENARIOS), format_func=lambda k: tamper.SCENARIOS[k][0])
    if st.button("Run the attack and verify", disabled=not principal.can("tamper:run")):
        with st.spinner("Tampering, then verifying…"):
            r = tamper.run(get_conn(), key)
        st.write(f"**Attacker:** {r['what']}.")
        if r["detected"]:
            st.error(f"CAUGHT — {len(r['problems'])} findings in {r['ms']:.0f} ms")
            st.dataframe(pd.DataFrame(r["problems"]).head(8), hide_index=True, use_container_width=True)
        else:
            st.warning("Not detected.")
        st.caption("Database restored (transaction rolled back).")

# ===========================================================================
if "Registry" in tabs:
    with tabs["Registry"]:
        ui_registry.registry_page(principal)

if "Users" in tabs:
    with tabs["Users"]:
        ui_registry.users_page(principal)

# ===========================================================================
with tab5:
    st.subheader("Baseline vs optimised")
    csv = ROOT / "benchmark_results.csv"
    if csv.exists():
        res = pd.read_csv(csv)
        st.dataframe(res, hide_index=True, use_container_width=True)
        st.bar_chart(res.set_index("query")[["baseline_ms", "optimised_ms"]])
        st.caption("Baseline is the same rows in one unpartitioned heap with no indexes. Optimised is the "
                   "day-partitioned table with the (unit, time) index, the hourly roll-up, and the HNSW vector index.")
    else:
        st.info("Run `python run.py benchmark` to generate benchmark_results.csv.")

# ===========================================================================
with tab6:
    st.subheader("What gap this fills")
    st.markdown((ROOT / "docs" / "GAP_ANALYSIS.md").read_text(encoding="utf-8")
                if (ROOT / "docs" / "GAP_ANALYSIS.md").exists() else "See docs/GAP_ANALYSIS.md.")
    st.caption("Sensor volume, the supply network and the incident text are simulated. See the README for what is "
               "real, what is simulated, and what the demo-grade cryptography does not claim.")
