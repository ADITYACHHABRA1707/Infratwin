**The short version.** Supply-chain data lives in three places that do not talk to each other, and a blockchain on its own does not join them. The valuable question is the join:

> *This thermometer left its limits at 17:04. Which lots were inside it, what was made from them, where is that now, who had custody, and can every party trust the record?*

### What exists today, and where it stops

| System | Knows | Does not know |
|---|---|---|
| **Traceability / ERP** (lot codes, GS1-style events) | which lot went where, and when | what temperature it experienced on the way |
| **Cold-chain monitoring** (loggers on trucks, rooms, shelves) | the temperature of a *unit* over time | which *lots* were inside that unit |
| **Blockchain pilots** | a shared, tamper-evident record of events | whether the data going in is joined to anything — and storing raw sensor streams on-chain does not scale or stay private |

Regulation is pushing the first column harder: the US FDA's food traceability rule (FSMA 204) requires covered firms to keep key data elements for critical tracking events and hand records over **within 24 hours**; enforcement was pushed to **20 July 2028** ([FDA](https://www.fda.gov/food/food-safety-modernization-act-fsma/fsma-final-rule-requirements-additional-traceability-records-certain-foods), [NC State summary](https://foodbusiness.ces.ncsu.edu/news/fda-fsma-204-food-traceability-rule-july-20-2028/)). Answering inside 24 hours is hard when the temperature record and the lot record are different systems.

### The four concrete failures

1. **An excursion cannot be turned into a lot list.** Sensors attach to units, lots attach to nothing physical. Without a chain-of-custody join, "the reefer went warm" becomes "hold everything that truck carried that week".
2. **Recalls are all-or-nothing.** If you cannot trace which batches consumed a bad raw lot, the only safe scope is everything produced since it arrived. In the seeded scenario that is **155 lots against 24** found by genealogy: about 6× over-recall. *(The baseline is an explicit assumption — see the Recall simulator tab. Real plants will vary.)*
3. **Nobody can settle a dispute.** The carrier, the hub and the store each hold their own log. When product arrives warm, each record is the party's word about itself.
4. **Putting everything on-chain is the wrong answer.** Raw readings are voluminous and commercially sensitive. The workable pattern is to keep data off-chain and anchor **hashes** on-chain.

### What ChainTwin does about each

| Failure | Mechanism | Where |
|---|---|---|
| 1 | `custody` table: lot × unit × time interval. One join turns an excursion window into the exact lots inside | `queries.EXPOSED_LOTS` |
| 2 | Recursive CTE over `lot_flows` with mass-balance **exposure share** (a batch that is 40% bad lot is 0.4 exposed). A time filter avoids condemning batches made *before* an excursion began | `queries.FORWARD` |
| 3 | Hand-offs signed by sender, receiver **and** carrier; each party signs with its own key. In the intended design a database administrator can alter rows but cannot produce a party's signature (**in this demo keys are derived from party names, so the code shows the verification logic, not key secrecy**) | `chain.py` |
| 4 | Each sensor window (trip or day) is reduced to a **Merkle root** and anchored. Raw rows stay in PostgreSQL; re-hashing them must reproduce the root. Change one reading and verification names the window | `chain.anchor_closed_windows` |

The tamper tab demonstrates six attacks, including the strongest one against a hash chain — editing a record and re-mining every later block. It is caught two independent ways: the edited transaction no longer carries a valid signature (the simulated attacker has no party keys), and the chain head no longer matches the checkpoint saved outside the database. The checkpoint is the check that does not depend on key secrecy.

### What this does *not* solve

- **The oracle problem.** A ledger proves a reading was not altered *after* it was recorded. It cannot prove the thermometer told the truth, or that nobody put it in a cup of ice. The research on sensor-to-blockchain pipelines is blunt that this needs authenticated devices, calibration and cross-checks ([Toward Sensor Measurement Reliability in Blockchains](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10747797/)). Here, devices have their own signing identity; secure elements and redundant probes would be the production answer.
- **Demo-grade cryptography.** Party keys are derived from names, signatures are HMAC rather than asymmetric, consensus is a low-difficulty proof of work on a single node. A real consortium would run Hyperledger Fabric or Besu with each party holding its own key and node. The *shape* — what is signed, what is hashed, what is verified — is the part that transfers.
- **Simulated data.** The supply network, volumes, sensor streams and incident text are generated. The pipeline is real; the numbers are not field data.
- **Mixing model.** Exposure share assumes a batch mixes its inputs proportionally.
- **Novelty is unproven.** I did not run a competitive survey of commercial traceability products. Treat "this join is under-served" as a hypothesis to validate with practitioners, not a finding.

### Where it goes next

GS1 EPCIS 2.0 import/export so lots arrive in an industry-standard format · a permissioned chain with per-party nodes · multi-sensor plausibility checks (flag a probe that disagrees with its neighbour) · shelf-life models so a 90-minute excursion reduces remaining life instead of being a binary hold · real incident data (e.g. public recall records) in the vector store.
