# InfraTwin — Technology Stack

Every component, what it does here, and why it was chosen over the obvious
alternative.

---

## The one-line version

**One PostgreSQL database doing the work of three** — a time-series store, a
graph store and a vector store — so an operator's three questions are answered
by three queries against one connection, and can be joined to each other.

---

## Stack

| Layer | Technology | Version | Role |
|---|---|---|---|
| Database | **PostgreSQL** | 16 | Everything |
| Time-series | **TimescaleDB** hypertable *or* native `PARTITION BY RANGE` | 2.x / built-in | Partitioning + roll-up |
| Vector search | **pgvector** | 0.8.0 | `vector(384)` column, HNSW index |
| Embeddings | **sentence-transformers** `all-MiniLM-L6-v2` | 3.3.1 | Report text → 384-dim vector |
| App layer | **Python** | 3.10+ | Seed, benchmark, verification |
| DB driver | **psycopg** | 3.2 | Server-side binding, `COPY`, pipeline |
| Data | **pandas**, **numpy** | 2.x | Dataset handling, vector maths |
| Dashboard | **Streamlit** | 1.40 | The demo UI |
| Packaging | **Docker Compose** | — | One-command database |
| Tests | **pytest** | 8.x | 34 tests |
| Dataset | **AI4I 2020** (UCI, CC BY 4.0) | — | Real failure patterns |

---

## Why each choice

### PostgreSQL as the only datastore

The alternative is the conventional stack: InfluxDB or Timescale for telemetry,
Neo4j for the asset graph, Pinecone or Milvus for embeddings.

| | Three systems | One PostgreSQL |
|---|---|---|
| Operational surface | 3 deployments, 3 backups, 3 auth models | 1 of each |
| Cross-domain query | application-side joins | one SQL statement |
| Consistency | eventual, across systems | ACID, one transaction |
| Cost at this scale | 3 services | 1 container |
| Ceiling | higher per domain | real, and reached later than people assume |

The cross-domain join is the part that is genuinely hard to replicate. "Show me
every machine downstream of the failing pump that has *also* had a
heat-dissipation failure in the last 30 days" is one query here. Across three
systems it is three round trips and a join written by hand in application code.

**Where this choice stops being right:** a dedicated time-series engine will
beat this past roughly 10⁹ rows per table, and a dedicated vector database wins
on billion-scale ANN with sharding and quantisation. At a plant's scale — a few
thousand machines, a few years of minute-resolution telemetry — PostgreSQL is
not the bottleneck, and the integration tax of three systems is paid daily.

### TimescaleDB *or* native partitioning

Both paths ship, and `make schema` detects which is available.

- **TimescaleDB** adds automatic chunk management, incrementally-refreshed
  continuous aggregates, and columnar compression.
- **Native `PARTITION BY RANGE`** needs no extension at all, which matters when
  Docker is blocked on a lab machine or the target is a hosted PostgreSQL that
  offers `pgvector` but not TimescaleDB.

The column definitions, indexes and **every downstream query are identical**.
Only the partitioning mechanism differs. The portable path is the one tested
end-to-end in this repository's development; the TimescaleDB path uses the same
`readings_hourly` contract.

### pgvector over a dedicated vector database

A separate vector database means the embedding lives away from the failure
record, so every similarity result needs a second lookup to recover which asset,
which date, which failure type. Here it is `JOIN failure_events USING
(event_id)` in the same statement.

**HNSW over IVFFlat**: HNSW gives better recall at the same latency and needs no
training step on the data distribution. IVFFlat builds faster and uses less
memory, which matters above roughly a million vectors.

### all-MiniLM-L6-v2

384 dimensions, ~80 MB, runs on CPU in milliseconds. The 768- and 1024-dim
models score a few points better on retrieval benchmarks and cost twice the
storage and index time for text this short and this domain-specific.

**Offline fallback.** If the model cannot be downloaded — no network, blocked
Hugging Face, a conference wifi that will not cooperate — `seed.py` falls back to
a deterministic lexical embedder: synonym-folded bag of words, IDF-weighted, L2
normalised, same 384 dimensions. It is lexical rather than semantic, and it
returns the correct failure mode on all five test symptoms. **The schema, the
HNSW index and every query are unchanged either way**, which is the point: the
demo cannot be taken down by the venue's network.

### psycopg 3

Server-side parameter binding, native `COPY` support (used for the 20,000
benchmark vectors), and the `pgvector` adapter registers against it directly so
numpy arrays pass straight through.

### Streamlit

Three panels and two charts. Flask or FastAPI would mean writing a frontend; the
demo is 190 lines instead.

### AI4I 2020 dataset

10,000 rows, 339 real failures across five documented modes, CC BY 4.0. Using a
published dataset means the failure *patterns* are real even though the sensor
volume and the asset hierarchy are simulated — which the app states on screen
rather than leaving to be discovered.

`scripts/make_dataset.py` downloads it. If the download is blocked it generates
a stand-in **from the dataset's own published failure rules** (temperature gap
< 8.6 K and rpm < 1380 for HDF; process power outside 3500–9000 W for PWF; and so
on), landing at 3.36% failure prevalence against the real file's 3.39%. Any
generated file is marked with `data/SYNTHETIC.txt` so it cannot be mistaken for
the real thing.

---

## What was built, in numbers

| | |
|---|---|
| Sensor readings | 5,090,501 |
| Assets / dependency edges | 101 / 100 |
| Failure events / reports embedded | 339 / 339 |
| Benchmark vectors | 20,000 |
| Tables / materialized views / triggers | 9 / 1 / 1 |
| CHECK + UNIQUE constraints | 12 |
| Indexes (2 of them HNSW) | 17 |
| Tests | 34, all passing |
| Self-checks in `verify.py` | 28, all passing |
| Full pipeline, cold start | ~2 minutes |

---

## Measured results

Baseline is the identical rows in one unpartitioned heap with no indexes.

| Query | Baseline | Optimised | Speed-up |
|---|---:|---:|---:|
| Avg temp, 1 machine, last 24 h | 236.3 ms | 3.23 ms | **73×** |
| Daily averages, all machines, 30 d | 1253.2 ms | 44.92 ms | **28×** |
| Threshold scan, 1-hour window | 252.9 ms | 57.01 ms | **4.4×** |
| Similar-report search, 20k vectors | 15.5 ms | 2.13 ms | **7.3×** |

**The trade-off, stated plainly:** the optimised table occupies **551 MB against
the baseline's 293 MB**. The indexes cost 88% more storage. That is the deal —
disk is cheap, the operator waiting on a dashboard is not. The roll-up is the
opposite kind of win: 10 MB answers the 30-day question that would otherwise
scan 551 MB.

---

## Running it

```bash
docker compose up -d          # or: make up
make all                      # dataset → schema → seed → optimise → benchmark → verify
make demo                     # re-arm the demo anomaly immediately before presenting
make app                      # Streamlit dashboard
```

`make verify` runs 28 checks and prints `Ready to present.` when the demo will work.
