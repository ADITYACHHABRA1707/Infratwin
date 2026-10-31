# ChainTwin

**A lot-level digital twin of a supply chain, with a tamper-evident blockchain ledger.**
Sensors, lot genealogy and chain of custody live in one PostgreSQL database; a ledger
anchors hashes and signatures of all of it so no party can quietly rewrite the record.

> Evolved from *InfraTwin* (predictive maintenance on plant machinery). The original is
> preserved in [legacy/](legacy/). What was reused and what is new is listed at the end.

```
The question                                         Answered by
─────────────────────────────────────────────────────────────────────────────────
Which unit is behaving abnormally right now?         time-series roll-up + z-score
Which lots were inside it while it did?             custody join (lot × unit × time)
What else is affected, and how much of each lot?     recursive CTE + mass-balance share
Have we seen this before, what did we do?            pgvector HNSW over incident reports
Where did this yoghurt come from, and how warm?      backward CTE + per-leg temperatures
Can I trust any of the above?                        hash-chained, signed ledger
```

The scenario is a dairy: three milk co-ops and a pulp supplier feed a plant (cold store →
two pasteuriser lines → packing), which ships to two distribution hubs and eight stores.
30 days are simulated: ~1,400 lots, ~410 shipments, ~680,000 sensor readings.

---

## Quick start

```bash
pip install -r requirements.txt
python run.py all        # database → schema → seed → optimise → benchmark → verify
python run.py app        # open the dashboard
```

`run.py` replaces `make` and `psql`, so it works on Windows as-is (`make` still works
where installed — the Makefile just calls `run.py`). Seeding takes a few minutes; the
slow parts are generating the sensor stream and loading the embedding model.

Right before you present: `python run.py demo` re-arms the live anomalies against the
current clock (see [Presenting](#presenting)).

| Command | Does |
|---|---|
| `up` / `down` / `clean` | start the container / stop it / delete it **and its data** |
| `schema` `seed` `optimise` | build, load, index |
| `anchor` | write settled sensor windows and hand-offs to the ledger |
| `verify` | ~70 self-checks, including six live tampering attempts |
| `tamper` | the six attacks, printed (all rolled back) |
| `test` | pytest suite (40 tests) |
| `demo` | re-arm the live anomalies |
| `benchmark` | baseline vs optimised → `benchmark_results.csv` |

## Accounts, roles and the REST API

Sign-in uses email + password (bcrypt) and a signed JWT. All people live in one `users` table;
`role_id` decides what they may do.

| Role | May do |
|---|---|
| ADMIN | everything, including the Users page, role changes and the tamper demo |
| MANAGER | manage products/parties, register raw lots, place holds and releases |
| AUDITOR | read-only: dashboards, provenance, ledger verification |

```bash
cp .env.example .env        # then set real values: POSTGRES_PASSWORD, DATABASE_URL, JWT_SECRET, SEED_PASSWORD
python run.py all           # ... includes `users`, which creates admin@ / manager@ / auditor@chaintwin.local
python run.py app           # dashboard on :8501
python run.py api           # REST API on :8010, interactive docs at /docs
python run.py backup        # pg_dump -> backups/
python run.py restore       # pg_restore into a separate database and compare row counts
```

`.env` is git-ignored; no secret is written in source. Database access in the app goes through
SQLAlchemy (`models.py`, `services.py`); raw SQL is limited to recursive CTEs, vector search, the
ledger and the stored procedure. Docker: `docker build -t chaintwin .`, or
`docker compose --profile app up -d`.

The submission report is built with `python docs/report/build_report.py` (see `docs/report/`).

---

## What the dashboard shows

**Control tower.** Units behaving abnormally now (each judged against *its own* history),
and every out-of-spec episode in the last 72 hours. Pick one and the page walks the chain
of consequence: lots that were inside the unit → what was made from them and where it is
now → who operated the unit → similar past incidents and what was done → one click to place
every affected lot on hold, written to the ledger in the same transaction.

**Recall simulator.** A lab result fails on a raw milk lot, reported five days late. The
genealogy graph returns the exact recall scope with each lot's *exposure share*, split into
"still in the chain, pull it back" and "already sold, notify customers", compared with the
only scope available without genealogy.

**Provenance.** Pick a lot on a store shelf: what it was made from (by share), every custody
leg with its temperature range, and the ledger records behind it.

**Ledger.** Blocks, transaction types, two verifiers, and a "try to break it" panel.

### Measured on the seeded data

| | |
|---|---|
| Recall scope by genealogy | **24 lots** (10 still in the chain) |
| Without genealogy (everything made since the lot arrived) | **155 lots** (81 in the chain) |
| Forward trace over 1,387 lots | ~36 ms |
| Verify the chain | ~0.2–0.3 s for 32 blocks / 2,834 transactions |
| Verify the database against the ledger | ~10 s: re-hashes ~669,000 readings |

The "without genealogy" figure rests on a stated assumption (see
[docs/GAP_ANALYSIS.md](docs/GAP_ANALYSIS.md)); it is a comparison, not a prediction.

Query timings from one run on a laptop (PostgreSQL 18, 681k readings; the vector row could
not be measured in that environment — `python run.py benchmark` produces it on your
database):

| Query | Baseline | Optimised | Speed-up |
|---|---:|---:|---:|
| Avg temp, 1 unit, last 24 h | 499 ms | 4.7 ms | 105× |
| Daily averages, all units, 30 d | 537 ms | 13.9 ms | 39× |
| Readings above 8 °C, 1-hour window | 478 ms | 7.2 ms | 67× |

The partitioned, indexed table is about twice the size of the plain heap (69 MB vs 34 MB).
That is the trade being made.

---

## How the ledger works

Business data stays in the relational tables. The ledger stores hashes and signatures:

```
Block   height │ prev_hash │ merkle_root │ nonce │ ts   ──SHA-256──▶ block_hash (starts with 000)
Tx      type │ subject │ canonical payload │ signatures by every required party
```

| Transaction | Signed by | Proves |
|---|---|---|
| `LOT_REGISTERED` | the holder | a lot exists, with its quantity and *inputs* (genealogy) |
| `CUSTODY_TRANSFER` | sender, receiver **and** carrier | a hand-off happened |
| `TELEMETRY_DIGEST` | the device | Merkle root of a trip's or day's readings |
| `QUALITY_RESULT` | the plant lab | a lab result |
| `STATUS_CHANGE` | the plant | hold / recall / release |

Two verifiers, because there are two ways to cheat:

* `verify_chain` — is the ledger consistent with itself? Block links, proof of work, Merkle
  roots, payload and transaction hashes, every signature, and the chain head against a
  checkpoint saved *outside* the database.
* `verify_offchain` — does the database still match what was signed? Re-derives every lot
  record and hand-off, and re-hashes the raw sensor readings of every anchored window.

Readings are anchored once their window has been closed for 6 hours (a completed UTC day
for fixed units, a completed trip for reefers), so open windows are never half-signed.

`python run.py tamper` plays six attackers with full SQL access — shrink a lot's quantity,
delete the link between a raw lot and a batch, un-recall a lot, rewrite a trip's
temperatures, edit a signed transaction with the append-only trigger switched off, and
**rewrite history and re-mine every later block**. All six are caught; all are rolled back.

### What the ledger does not do

* It cannot prove a thermometer was honest, only that its reading was not changed later
  (the *oracle problem*).
* **Keys are derived from party names, signatures are HMAC, and there is one node.** That
  demonstrates *what* is signed and verified, not key secrecy — anyone with this source
  could forge a signature. A real consortium needs per-party private keys (ideally in
  hardware), asymmetric signatures and a permissioned chain such as Hyperledger Fabric or
  Besu. The checkpoint is the one check that does not rely on key secrecy.
* Proof of work is here because it is easy to see. Permissioned chains use PoA/BFT.

---

## Data: what is real, what is simulated

Everything is simulated: the network, quantities, sensor streams and incident text. The
pipeline, schema, queries and cryptography are real and tested. The earlier project used
the real AI4I 2020 failure dataset; there is no equivalent open dataset for lot-level
supply-chain telemetry, so none is claimed.

Embeddings use `all-MiniLM-L6-v2` when `sentence-transformers` is installed and the model
is downloadable, and an offline lexical embedder otherwise (same 384 dimensions, same
index, same queries). The dashboard names which one it used.

The plant clock is anchored so "now" is always 18:30 on the last simulated day: trucks have
just left, an evening batch is on the pasteuriser, the morning's deliveries are on shelves.
There is always live material to show, at any time of day.

---

## Presenting

```bash
python run.py demo      # re-arm the live anomalies
python run.py verify    # should end "Ready to present."
```

**Why `demo` matters.** The dashboard compares "the last hour" with "everything older than
three hours", both relative to `now()`. The injected anomalies are in the 90 minutes before
seeding; as the clock moves they drift into the baseline they are measured against and the
z-scores decay. `demo` reverts the previous injection, tops the fixed units up to the
current minute, and injects again. It never edits a reading that is inside an anchored
window — that would correctly fail ledger verification. The in-transit truck is part of the
seeded snapshot only; a trip's readings stop when the trip does.

Suggested order: Control tower → pick the cold-room episode from 40 h ago (reaches four
stores) → Recall simulator → Provenance → Ledger → run the *rewrite history* attack.

---

## Project layout

```
infratwin/
├── app.py              Streamlit dashboard
├── chain.py            The ledger: hashing, Merkle, mining, signing, anchoring, verification
├── tamper.py           Six attack scenarios, run inside rolled-back transactions
├── actions.py          Status changes written to DB + ledger atomically
├── queries.py          The showcase SQL, shared by app, verify, benchmark, tests
├── seed.py             Simulates the supply chain; loads it; writes the ledger
├── embedding.py        Incident-report text, embedder, offline fallback
├── benchmark.py        Baseline vs optimised
├── verify.py           ~70 self-checks
├── run.py              Task runner (no make or psql needed)
├── sql/
│   ├── 01_core.sql          Parties, products, units, lots, flows, custody, incidents
│   ├── 02_timeseries.sql    Day-partitioned sensor_readings
│   ├── 03_optimise.sql      Hourly roll-up, HNSW, un-optimised baseline
│   └── 04_ledger.sql        Blocks, transactions, sensor windows, append-only guard
├── scripts/            anchor.py · refresh_demo.py · tamper_demo.py
├── tests/              test_chaintwin.py
├── docs/               GAP_ANALYSIS.md · DBMS_RELATIONS.md · TECH_STACK.md
└── legacy/             the original InfraTwin (machinery) project, untouched
```

## From InfraTwin to ChainTwin

| | InfraTwin | ChainTwin |
|---|---|---|
| Node | machine (`assets`) | lot (`lots`), with a custody table linking lots to sensored units |
| Graph | motor → pump → line → plant | raw → batch → finished → hub stock → store stock, weighted by quantity |
| Anomaly | per-machine z-score | per-unit z-score, two-sided (a pasteuriser dropping is the hazard) |
| Cascade | "machines affected" | "lots affected, and how much of each", time-aware |
| Vector search | past machine failures | past supply-chain incidents |
| Trust | audit trigger | hash-chained, multi-party-signed ledger + sensor anchoring |
| Engine | TimescaleDB *or* partitioning | partitioning only (no TimescaleDB feature was needed) |
| Data | AI4I failure patterns | fully simulated |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Docker accepts commands but nothing starts | Restart Docker Desktop (the engine, not this project, is stuck) |
| `container name "/chaintwin-db" is already in use` | `docker rm chaintwin-db`, then `python run.py up` |
| Port 5434 in use | Change it in `docker-compose.yml` **and** `DATABASE_URL` |
| `CREATE EXTENSION vector` fails | Use an image with pgvector, e.g. `pgvector/pgvector:pg16` |
| Dashboard shows no anomalies | `python run.py demo` |
| Verify reports a telemetry mismatch right after editing readings | Working as designed: the window was anchored |
| Seed is slow | Set the `DAYS` environment variable, e.g. `DAYS=10` (minimum 8), before `python run.py seed` |
| Start over | `python run.py clean && python run.py all` |

## Licence

Code: MIT.
