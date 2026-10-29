"""
InfraTwin demo app.

Three panels, three database capabilities, one PostgreSQL connection:
  1. anomaly detection      time-series roll-up + per-asset z-score
  2. impact cascade         recursive CTE over the dependency graph
  3. similar past failures  pgvector HNSW nearest neighbour

    streamlit run app.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
from pgvector.psycopg import register_vector

from seed import load_embedder

ROOT = Path(__file__).resolve().parent
DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5433/postgres")

st.set_page_config(page_title="InfraTwin", layout="wide", page_icon="🏭")


@st.cache_resource
def get_conn():
    c = psycopg.connect(DB, autocommit=True)
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


ANOMALY = """
WITH base AS (
  SELECT asset_id, avg(avg_temp) AS mean_temp, GREATEST(stddev(avg_temp), 0.05) AS sd_temp
  FROM readings_hourly
  WHERE bucket < now() - interval '3 hours'
  GROUP BY asset_id),
recent AS (
  SELECT asset_id, avg(process_temp) AS recent_temp
  FROM sensor_readings
  WHERE time > now() - interval '1 hour'
  GROUP BY asset_id)
SELECT a.asset_id, a.name, a.asset_type,
       round(r.recent_temp::numeric, 2) AS recent_temp,
       round(b.mean_temp::numeric, 2)   AS baseline_temp,
       round((r.recent_temp - b.mean_temp)::numeric, 2) AS rise_k,
       round(((r.recent_temp - b.mean_temp) / b.sd_temp)::numeric, 1) AS z_score
FROM recent r JOIN base b USING (asset_id) JOIN assets a USING (asset_id)
WHERE (r.recent_temp - b.mean_temp) / b.sd_temp > 3     -- unusual for THIS machine
  AND (r.recent_temp - b.mean_temp)            > 1.0    -- and big enough to matter
ORDER BY z_score DESC"""

IMPACT = """
WITH RECURSIVE impacted AS (
  SELECT d.dependent_id AS asset_id, 1 AS depth, d.dependency_type
  FROM asset_dependencies d WHERE d.asset_id = %s
  UNION ALL
  SELECT d.dependent_id, i.depth + 1, d.dependency_type
  FROM asset_dependencies d JOIN impacted i ON d.asset_id = i.asset_id
  WHERE i.depth < 5)
SELECT a.name, a.asset_type, i.depth, i.dependency_type
FROM impacted i JOIN assets a USING (asset_id)
ORDER BY i.depth, a.name"""

SIMILAR = """
SELECT f.failure_type, a.name AS failed_asset, f.event_time,
       r.summary, r.action_taken,
       round((1 - (r.embedding <=> %s))::numeric, 3) AS similarity
FROM maintenance_reports r
JOIN failure_events f USING (event_id)
JOIN assets a ON a.asset_id = f.asset_id
ORDER BY r.embedding <=> %s
LIMIT 3"""

TREND = """
SELECT date_trunc('minute', time) AS t, process_temp, torque, rpm
FROM sensor_readings
WHERE asset_id = %s AND time > now() - interval '8 hours'
ORDER BY t"""


# --------------------------------------------------------------------------
st.title("InfraTwin")
st.caption(
    "Predictive maintenance on one PostgreSQL database: time-series, graph and "
    "vector search without three separate systems."
)

try:
    meta, _ = q("""
        SELECT (SELECT count(*) FROM assets)              AS assets,
               (SELECT count(*) FROM sensor_readings)     AS readings,
               (SELECT count(*) FROM failure_events)      AS failures,
               (SELECT count(*) FROM maintenance_reports) AS reports,
               (SELECT count(*) FROM pg_extension WHERE extname='timescaledb') AS ts
    """)
except Exception as exc:
    st.error(f"Cannot reach the database at {DB.rsplit('@', 1)[-1]}")
    st.code(str(exc))
    st.info("Start it with `docker compose up -d`, then run `make all`.")
    st.stop()

row = meta.iloc[0]
engine = "TimescaleDB hypertable" if row["ts"] else "PostgreSQL native partitioning"
c1, c2, c3, c4 = st.columns(4)
c1.metric("Assets", f"{row['assets']:,}")
c2.metric("Sensor readings", f"{row['readings']:,}")
c3.metric("Failure events", f"{row['failures']:,}")
c4.metric("Reports indexed", f"{row['reports']:,}")
st.caption(f"Time-series engine: **{engine}**")

tab1, tab2, tab3 = st.tabs(["Live diagnosis", "Benchmark", "How it works"])

# --------------------------------------------------------------------------
with tab1:
    anomalies, t_anom = q(ANOMALY)

    st.subheader("1 · Detected anomalies")
    st.caption(
        f"Time-series layer — hourly roll-up for the baseline, raw rows for the "
        f"last hour, z-score per asset · **{t_anom:.0f} ms**"
    )

    if anomalies.empty:
        st.success("No anomalies in the last hour. Every machine is within "
                   "3 standard deviations of its own baseline.")
        st.caption("Re-run `python seed.py` to inject a fresh demo anomaly.")
    else:
        st.dataframe(anomalies, hide_index=True, use_container_width=True)

        pick = st.selectbox("Investigate machine", anomalies["name"])
        asset_id = int(anomalies.loc[anomalies["name"] == pick, "asset_id"].iloc[0])

        trend, _ = q(TREND, (asset_id,))
        if not trend.empty:
            trend = trend.set_index("t")
            # Separate charts: process_temp sits near 310 K and torque near
            # 40 Nm, so one shared axis flattens the torque line into the floor.
            g1, g2 = st.columns(2)
            with g1:
                st.caption("Process temperature (K) · last 8 hours")
                st.line_chart(trend[["process_temp"]], height=220)
            with g2:
                st.caption("Torque (Nm) · last 8 hours")
                st.line_chart(trend[["torque"]], height=220)

        left, right = st.columns(2)

        with left:
            impact, t_imp = q(IMPACT, (asset_id,))
            st.subheader("2 · Machines affected")
            st.caption(f"Graph layer — recursive CTE over asset_dependencies · "
                       f"**{t_imp:.0f} ms**")
            if impact.empty:
                st.info("Nothing downstream depends on this asset.")
            else:
                st.dataframe(impact, hide_index=True, use_container_width=True)
                worst = impact["depth"].max()
                st.caption(f"Blast radius: {len(impact)} assets, up to {worst} hops away.")

        with right:
            st.subheader("3 · Similar past failures")
            st.caption("Vector layer — pgvector HNSW nearest neighbour")
            symptom = st.text_input(
                "Describe the symptom",
                "Process temperature rising above normal with high torque and falling speed",
            )
            if symptom.strip():
                vec = get_embedder().encode(symptom, normalize_embeddings=True)
                sim, t_sim = q(SIMILAR, (vec, vec))
                st.caption(f"**{t_sim:.0f} ms** · embedder: "
                           f"`{getattr(get_embedder(), 'name', 'unknown')}`")
                for _, r in sim.iterrows():
                    with st.container(border=True):
                        st.markdown(
                            f"**{r['failure_type']}** on {r['failed_asset']} · "
                            f"similarity {r['similarity']}"
                        )
                        st.write(r["summary"])
                        st.caption(f"Action taken: {r['action_taken']}")

# --------------------------------------------------------------------------
with tab2:
    st.subheader("Baseline vs optimised")
    csv = ROOT / "benchmark_results.csv"
    if csv.exists():
        res = pd.read_csv(csv)
        st.dataframe(res, hide_index=True, use_container_width=True)
        st.bar_chart(res.set_index("query")[["baseline_ms", "optimised_ms"]])
        st.caption(
            "Baseline is the same rows in one unpartitioned heap with no indexes. "
            "Optimised is the partitioned table plus the composite index, the "
            "pre-aggregated hourly roll-up, and the HNSW vector index."
        )
    else:
        st.info("Run `python benchmark.py` to generate benchmark_results.csv.")

# --------------------------------------------------------------------------
with tab3:
    st.subheader("One database, three access patterns")
    st.markdown(
        """
| Question an operator asks | Normally needs | Here |
|---|---|---|
| Is this machine behaving abnormally? | a time-series database | hourly roll-up + z-score |
| What else breaks if it fails? | a graph database | `WITH RECURSIVE` over `asset_dependencies` |
| When did this happen before, and what fixed it? | a vector database | `pgvector` HNSW on report embeddings |

Keeping all three in PostgreSQL means one backup, one security model, one
connection, and joins **across** the three — the cascade result and the
similar-failure result can be filtered by the same asset in one query.
"""
    )
    st.caption(
        "Sensor volume, the asset hierarchy and the report text are simulated. "
        "Failure patterns come from the AI4I 2020 predictive maintenance dataset."
    )
