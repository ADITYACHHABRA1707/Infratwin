# InfraTwin

**Predictive maintenance on one PostgreSQL database.** Time-series anomaly
detection, a failure-cascade graph and semantic search over maintenance reports
— three capabilities that normally mean three separate systems — answered by
three queries against one connection.

Built for DBTHON 2026.

```
Operator's question                    Normally needs        Here
────────────────────────────────────────────────────────────────────────────
Is this machine behaving abnormally?   time-series DB        roll-up + z-score
What else breaks if it fails?          graph DB              WITH RECURSIVE
When did this happen before?           vector DB             pgvector HNSW
```

---

## Quick start

```bash
docker compose up -d     # start PostgreSQL (TimescaleDB + pgvector image)
make all                 # dataset → schema → seed → optimise → benchmark → verify
make app                 # open the dashboard
```

`make all` takes about two minutes and ends with `Ready to present.`

No `make`? The same steps:

```bash
pip install -r requirements.txt
python scripts/make_dataset.py
psql "$DATABASE_URL" -f sql/01_core.sql
psql "$DATABASE_URL" -f sql/02_timeseries_portable.sql   # or 02_timeseries_timescale.sql
python seed.py
psql "$DATABASE_URL" -f sql/03_optimise_portable.sql     # or 03_optimise_timescale.sql
python benchmark.py
python verify.py
streamlit run app.py
```

**Right before you present**, run `make demo`. It re-arms the anomaly against the
current clock — see [Presenting](#presenting).

---

## What it does

**1 · Anomaly detection.** Each machine is judged against *its own* history, not
a global threshold: a pump that always runs warm should not alarm, and a motor
that is normally cool should alarm sooner. The baseline comes from the
pre-aggregated hourly roll-up; the last hour is read at full resolution.

**2 · Failure cascade.** `asset_dependencies` is a directed graph. A recursive
CTE walks it upward from the failing machine — motor → pump → line → plant — so
the operator sees the blast radius ordered by how close it is.

**3 · Similar past failures.** Report text is embedded into a `vector(384)`
column. "Temperature climbing with low speed" finds the heat-dissipation
failures from three weeks ago and what fixed them, through an HNSW index.

All three results are joinable, because they are in the same database.

---

## Measured results

Baseline is the identical rows in one unpartitioned heap with no indexes.

| Query | Baseline | Optimised | Speed-up |
|---|---:|---:|---:|
| Avg temp, 1 machine, last 24 h | 236.3 ms | 3.23 ms | **73×** |
| Daily averages, all machines, 30 d | 1253.2 ms | 44.92 ms | **28×** |
| Threshold scan, 1-hour window | 252.9 ms | 57.01 ms | **4.4×** |
| Similar-report search, 20k vectors | 15.5 ms | 2.13 ms | **7.3×** |

Measured on 5,090,501 sensor readings, median of 7 runs after 2 warmup runs.

These are from one run in the reference environment. Timings vary with hardware and load — the point query has measured between 70× and 92× across runs. `make benchmark` regenerates `benchmark_results.csv`, and rebuilding the deck (`node scripts/make_deck.js`) picks up your numbers automatically.

The optimised table costs **551 MB against the baseline's 293 MB** — the indexes
are 88% more storage. That is the trade being made, not a detail to skip past.

---

## Documentation

| | |
|---|---|
| [docs/DBMS_RELATIONS.md](docs/DBMS_RELATIONS.md) | Entities, relationships, normalisation, constraints, indexes, trigger |
| [docs/er_diagram.png](docs/er_diagram.png) | ER diagram, generated from the live catalog |
| [docs/TECH_STACK.md](docs/TECH_STACK.md) | Every technology, and why it over the alternative |
| [sql/queries.sql](sql/queries.sql) | The three showcase queries, fully commented |

---

## Two engines, one schema

The time-series table ships in two forms and `make schema` picks automatically:

- **TimescaleDB** — hypertable, continuous aggregate, compression. Best if
  available.
- **Plain PostgreSQL 16** — native `PARTITION BY RANGE (time)`, one partition per
  day, materialized-view roll-up. **No extension beyond pgvector.**

Column definitions, indexes and every downstream query are identical. Only the
partitioning mechanism differs. Use the portable path when Docker is blocked on a
lab machine, or on a hosted PostgreSQL (Supabase, Neon, RDS) that offers pgvector
but not TimescaleDB.

---

## Data

Failure patterns come from the **AI4I 2020 Predictive Maintenance Dataset** (UCI,
CC BY 4.0) — 10,000 rows, 339 real failures across five documented modes.
Sensor volume, the asset hierarchy and report text are simulated, which the app
states on screen.

`python scripts/make_dataset.py` downloads it. If the download is unreachable it
generates a stand-in from the dataset's **own published failure rules**, landing
at 3.36% failure prevalence against the real file's 3.39%. Generated files are
marked with `data/SYNTHETIC.txt`. For a submission claiming real data, delete
both files and re-run with network access.

**Embeddings** use `all-MiniLM-L6-v2`. If it cannot be downloaded, `seed.py`
falls back to a deterministic lexical embedder — same 384 dimensions, same index,
same queries — which returns the correct failure mode on all five test symptoms.
The demo does not depend on the venue's wifi.

---

## Presenting

Run this immediately before you go up:

```bash
make demo      # re-arms the anomaly against the current clock
make verify    # 28 checks, should print "Ready to present."
```

**Why `make demo` matters.** `seed.py` injects the anomaly into "the last 90
minutes", but the dashboard compares "the last hour" against "everything older
than three hours", both relative to `now()`. As the clock advances, the injected
rows drift out of the detection window and into the baseline they are being
measured against — the z-scores decay and eventually the anomaly disappears.
`make demo` reverts the previous injection exactly and re-applies it at the
current time, in one transaction.

A good demo order: dashboard → anomaly → cascade → similar failures → benchmark
tab → ER diagram.

---

## Verifying the work

```bash
make verify    # 28 runtime checks against the live database
make test      # 34 pytest tests
```

`verify.py` attempts constraint violations and asserts they are rejected, checks
referential integrity, confirms the audit trigger fires on all three operations,
runs all three showcase queries, and reports which scan the planner chose.

The test suite additionally checks the generated dataset against the published
AI4I rules, asserts the embedder's synonym folding works, and inserts a
dependency **cycle** to prove the recursive CTE's depth guard holds.

---

## Project layout

```
infratwin/
├── app.py                    Streamlit dashboard
├── seed.py                   Asset graph, sensor stream, embeddings
├── benchmark.py              Baseline vs optimised, writes benchmark_results.csv
├── verify.py                 28 self-checks
├── Makefile                  make up / all / demo / verify / app
├── docker-compose.yml
├── sql/
│   ├── 01_core.sql                   Entities, constraints, audit trigger
│   ├── 02_timeseries_portable.sql    Native range partitioning
│   ├── 02_timeseries_timescale.sql   Hypertable
│   ├── 03_optimise_portable.sql      Roll-up + HNSW + baseline
│   ├── 03_optimise_timescale.sql     Continuous aggregate + HNSW + baseline
│   └── queries.sql                   The three showcase queries
├── scripts/
│   ├── make_dataset.py       Download or generate AI4I
│   ├── make_er_diagram.py    ER diagram from the live catalog
│   └── refresh_demo.py       Re-arm the demo anomaly
├── docs/
│   ├── DBMS_RELATIONS.md
│   ├── TECH_STACK.md
│   └── er_diagram.svg / .png
└── tests/test_infratwin.py   34 tests
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Port 5433 already in use | Change it in `docker-compose.yml` **and** `DATABASE_URL` |
| `CREATE EXTENSION vector` fails | Image lacks pgvector — use `pgvector/pgvector:pg16` and the portable SQL path |
| `CREATE EXTENSION timescaledb` fails | Expected on a plain image; use the portable path, `make schema` does this automatically |
| Docker blocked on a lab PC | Point `DATABASE_URL` at a hosted PostgreSQL with pgvector (Supabase, Neon) and use the portable path |
| Dashboard shows no anomalies | `make demo` |
| `syntax error at or near ":"` | `psql` does not interpolate `:asset` with `-c`; use `-f` |
| Seed is slow on an old laptop | `DAYS=4 python seed.py` |
| Start completely over | `docker compose down -v`, then `make up && make all` |

---

## Licence

Code: MIT. Dataset: AI4I 2020, CC BY 4.0 — Matzka, S. (2020), UCI Machine
Learning Repository.
