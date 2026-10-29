# ChainTwin — technology choices

| Layer | Choice | Why this, and what it replaces |
|---|---|---|
| Database | **PostgreSQL 16** | One engine for relational, time-series, graph and vector. Keeping them together means one backup, one security model, and joins *across* them: the exposure query joins custody (relational), sensor readings (time-series) and lot flows (graph) in one statement |
| Time series | **Native range partitioning** + materialised roll-up | Day partitions prune; a roll-up answers 30-day questions without re-scanning raw rows. No TimescaleDB feature was needed, so the extension is not required and any hosted PostgreSQL with pgvector works |
| Graph | **`WITH RECURSIVE`** over an edge table | Genealogy is a DAG of ~1,400 nodes; a recursive CTE traverses it in ~36 ms. A graph database would add a system to run and sync for no gain at this size. At millions of edges with deep multi-hop analytics, that trade changes |
| Vector search | **pgvector, HNSW, cosine** | "Find incidents like this one" as a SQL `ORDER BY`, joinable to units and lots |
| Embeddings | `all-MiniLM-L6-v2` (384-d), offline lexical fallback | Real semantic search when the model can be downloaded; deterministic, dependency-free fallback so the demo never depends on wifi |
| Ledger | **Hash chain + Merkle trees + signatures, in PostgreSQL** (`chain.py`) | Shows exactly what is hashed, signed and verified, with no node software to install. Appropriate for a demonstrable prototype; see below for production |
| Hashing | SHA-256 | Standard; used for payloads, transactions, Merkle trees and blocks |
| Signatures | HMAC-SHA256 (demo) | Stands in for per-party asymmetric signatures. Keys are derived from names in the demo — see the README |
| Consensus | Proof of work, difficulty 3 (demo) | Visible and easy to explain. Not what a consortium would run |
| Dashboard | **Streamlit** | The whole UI is Python next to the queries it shows |
| Driver | `psycopg` 3 | Server-side generation (`generate_series`) and `COPY` for bulk loads |
| Runner | `run.py` | Works on Windows without `make` or `psql` |
| Tests | `pytest` | Pure-Python tests for primitives and the simulation; database tests that skip when no database is reachable |

## Why a ledger inside PostgreSQL, and why that is not "cheating"

The properties that matter for tamper-evidence come from the *structure*, not from a
particular piece of software: records are hashed, hashes are chained and Merkle-rooted,
every required party signs, and the head is checkpointed somewhere the database owner does
not control. The append-only trigger is only a convenience — `tamper.py` switches it off
and shows the edit is still caught.

What a real deployment would change:

| Prototype | Production |
|---|---|
| One database, one node | One node **per party**, each holding a full copy and comparing tip hashes |
| HMAC with name-derived keys | Ed25519/ECDSA, private keys in each party's HSM or secure element |
| Proof of work | PoA / QBFT / Raft in a permissioned network (Hyperledger Fabric, Besu) |
| Checkpoint file | Each party's own copy of the head, or periodic anchoring to a public chain |
| Device identity = unit name | Per-device certificates; readings signed at the gateway |

## Alternatives considered

| Instead of | Why not here |
|---|---|
| Separate time-series, graph and vector databases | Three systems to run, secure, back up and synchronise, and no cross-system join. The comparison table in the legacy README still applies |
| Public blockchain (Ethereum) for everything | Cost and latency per transaction, and raw sensor data is both voluminous and commercially sensitive. Anchoring a *root hash* periodically is the sensible use |
| Storing readings on-chain | Does not scale; leaks data to every participant. Merkle digests give the same tamper-evidence |
| ORM | The value is in the SQL; hiding it would hide the point |
