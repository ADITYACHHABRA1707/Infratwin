"""
Incident-report text and embeddings.

Past incidents are the plant's memory: what went wrong, what was done, how it
ended. Each report is COMPOSED from a symptom, a context line, an action and an
outcome, with measured values substituted in. Three fixed strings per kind would
make every report identical and the vector search would return three copies of
the same row; composition gives hundreds of distinct texts per kind, so nearest
neighbours are genuinely different reports.

load_embedder() returns the real all-MiniLM-L6-v2 model when it is installed and
downloadable, and an offline lexical embedder otherwise. Both give 384-dim
L2-normalised vectors, so the schema, the HNSW index and every query are the same.
"""
from __future__ import annotations

import random
import re

import numpy as np

EMBED_DIM = 384

SYMPTOMS = {
    "REEFER_FAULT": [
        "Reefer compressor tripped en route, return air temperature climbed to {temp} C over {mins} minutes",
        "Refrigeration unit failed to hold setpoint, cargo temperature drifted up to {temp} C during the delivery run",
        "Evaporator coil iced up and blocked the airflow, trailer temperature reached {temp} C after {mins} minutes",
        "Driver left the rear door open at the drop, trailer temperature spiked to {temp} C for {mins} minutes",
        "Reefer ran out of fuel mid-route, the trailer warmed to {temp} C before the unit was restarted",
        "Setpoint was entered wrongly at loading, the load sat at {temp} C for {mins} minutes",
    ],
    "COLDROOM_FAULT": [
        "Power cut left the cold room without cooling, temperature rose to {temp} C over {mins} minutes",
        "Defrost cycle stuck on, room temperature held at {temp} C for {mins} minutes",
        "Door left ajar after the evening shift, the room warmed to {temp} C",
        "Condenser fan failure, the cold store climbed slowly to {temp} C across {mins} minutes",
        "Display case overloaded and blocking the air curtain, shelf temperature reached {temp} C",
        "Compressor short-cycling, store temperature drifted to {temp} C over {mins} minutes",
    ],
    "PROCESS_DEVIATION": [
        "Pasteuriser holding temperature dropped to {temp} C for {mins} minutes, below the legal hold temperature",
        "Heat exchanger fouling reduced the pasteurisation temperature to {temp} C during the run",
        "Steam valve stuck partly closed, product left the holding tube at {temp} C",
        "Flow diversion valve failed to divert when temperature fell to {temp} C",
        "Temperature controller drifted, hold temperature read {temp} C for {mins} minutes of the batch",
    ],
    "SUPPLIER_QUALITY": [
        "Incoming raw milk tested positive for antibiotic residue above the limit, lot already unloaded into the silo",
        "Raw milk lot arrived with a bacterial count far above the contract limit at {temp} C receipt temperature",
        "Freezing point test failed on a tanker load, adulteration with added water suspected",
        "Fruit pulp lot failed the pesticide residue screen after it had been used in a batch",
        "Supplier notified us that a lot was contaminated after it had already been delivered and consumed",
        "Lab result on a raw milk lot came back out of specification after the lot was blended into production",
    ],
    "SENSOR_FAULT": [
        "Probe in the cold room read {temp} C while the reference thermometer showed normal, calibration drift suspected",
        "Data logger lost signal for {mins} minutes leaving a gap in the temperature record",
        "Display case sensor stuck at one constant reading for hours, probe failure suspected",
        "Temperature alarm raised by a probe that disagreed with the second sensor by {temp} degrees",
        "Logger battery low, readings jumped to {temp} C and recovered with no product change",
    ],
}

CONTEXT = [
    "Raised by the night shift supervisor",
    "Flagged by the monitoring dashboard",
    "Reported by the driver on arrival",
    "Picked up during the routine round",
    "Escalated by the store manager",
    "Found at the shift handover check",
    "Second occurrence on the same unit that month",
]

ACTIONS = {
    "REEFER_FAULT": [
        "Moved the load to a spare reefer at the nearest depot and held every lot on board for quality review",
        "Recovered the truck, logged the excursion duration for each lot and tested retained samples before release",
        "Held the lots on arrival, ran microbiological tests and replaced the compressor relay",
        "Rejected the load at the receiving dock and raised a claim against the carrier using the temperature record",
    ],
    "COLDROOM_FAULT": [
        "Moved stock to a second cold room, placed lots held while they were checked and repaired the compressor",
        "Restored power and cooling, reviewed time above limit for each lot in the room and released the short exposures",
        "Fixed the door seal, cleared the air curtain and held only the lots stored in the affected bay",
        "Replaced the condenser fan and set a tighter alarm so the next drift is caught earlier",
    ],
    "PROCESS_DEVIATION": [
        "Diverted the affected batch, held every finished lot packed from it and re-ran the product through pasteurisation",
        "Quarantined the batch, traced forward to all finished lots and shipments and descaled the heat exchanger",
        "Stopped the line, held the batch and recalibrated the temperature controller before restart",
        "Held the lots, sent samples for phosphatase testing and replaced the stuck steam valve",
    ],
    "SUPPLIER_QUALITY": [
        "Quarantined the lot, traced forward to every batch that consumed it, held all downstream lots and notified the supplier",
        "Traced the lot through the batches to the retailers, withdrew stock still on shelf and informed the regulator",
        "Placed downstream lots on hold by exposure share, recalled the stock still in the chain and audited the supplier",
        "Blocked the supplier lot, identified the finished lots containing it and issued a targeted withdrawal",
    ],
    "SENSOR_FAULT": [
        "Replaced the probe, recalibrated against a reference and marked the readings for the period as unreliable",
        "Cross-checked the second sensor, found product temperature normal and logged the fault with no product action",
        "Swapped the logger, filled the gap from the backup record and added a redundant probe",
    ],
}

OUTCOMES = {
    "REEFER_FAULT": [
        "{n} lots released after passing tests, {m} destroyed",
        "Load rejected, carrier liable for {n} lots",
        "All {n} lots released, exposure inside the allowed time",
    ],
    "COLDROOM_FAULT": [
        "{n} lots released, {m} written off",
        "Short exposure, all {n} lots released",
        "{m} lots destroyed, {n} released after review",
    ],
    "PROCESS_DEVIATION": [
        "Batch reprocessed, {n} finished lots held and then released",
        "{n} finished lots destroyed, no product reached stores",
        "{n} lots caught before dispatch, none shipped",
    ],
    "SUPPLIER_QUALITY": [
        "{n} lots withdrawn from {m} stores, supplier suspended",
        "Targeted withdrawal of {n} lots, {m} stores notified",
        "{n} lots held, {m} already sold and customers notified",
    ],
    "SENSOR_FAULT": [
        "No product impact, sensor replaced",
        "No product action, readings excluded from the record",
        "No product impact, redundant probe installed",
    ],
}

RANGES = {
    "REEFER_FAULT": {"temp": (8, 15), "mins": (35, 240), "n": (2, 9), "m": (0, 4)},
    "COLDROOM_FAULT": {"temp": (8, 14), "mins": (30, 300), "n": (3, 12), "m": (0, 5)},
    "PROCESS_DEVIATION": {"temp": (68, 71), "mins": (2, 25), "n": (1, 6), "m": (0, 3)},
    "SUPPLIER_QUALITY": {"temp": (5, 9), "mins": (0, 1), "n": (4, 22), "m": (2, 8)},
    "SENSOR_FAULT": {"temp": (9, 16), "mins": (20, 180), "n": (0, 1), "m": (0, 1)},
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "with", "for", "is", "was",
    "were", "be", "been", "no", "not", "it", "its", "this", "that", "from", "by", "as", "all",
    "up", "out", "off", "over", "under", "after", "before", "had", "has", "have", "into", "than",
}

# Domain vocabulary folded onto a canonical term, so an operator typing
# "truck got too warm" matches a report that says "trailer temperature climbed".
SYNONYMS = {
    # temperature rising
    "warm": "temperature", "warmed": "rising", "warmer": "rising", "hot": "temperature",
    "heat": "temperature", "temp": "temperature", "temperatures": "temperature",
    "climbed": "rising", "climbing": "rising", "climb": "rising", "rose": "rising",
    "rise": "rising", "spiked": "rising", "spike": "rising", "drifted": "rising",
    "drift": "rising", "increasing": "rising", "excursion": "rising", "breach": "rising",
    # temperature falling
    "dropped": "falling", "fell": "falling", "falls": "falling", "below": "falling",
    "reduced": "falling", "low": "falling",
    # transport
    "truck": "reefer", "trailer": "reefer", "reefer": "reefer", "lorry": "reefer",
    "refrigeration": "reefer", "driver": "reefer", "route": "reefer", "delivery": "reefer",
    "transit": "reefer", "van": "reefer",
    "compressor": "compressor", "unit": "compressor", "relay": "compressor",
    "evaporator": "airflow", "coil": "airflow", "iced": "airflow", "airflow": "airflow",
    "blocked": "airflow", "curtain": "airflow", "fan": "airflow", "condenser": "airflow",
    "door": "door", "ajar": "door", "open": "door", "rear": "door",
    # storage
    "store": "coldroom", "stores": "coldroom", "room": "coldroom", "cold": "coldroom",
    "display": "coldroom", "shelf": "coldroom", "case": "coldroom", "warehouse": "coldroom",
    "power": "power", "outage": "power", "cut": "power", "electricity": "power",
    "defrost": "defrost", "stuck": "stuck",
    # process
    "pasteuriser": "pasteurisation", "pasteurizer": "pasteurisation",
    "pasteurisation": "pasteurisation", "pasteurization": "pasteurisation",
    "holding": "pasteurisation", "hold": "pasteurisation", "steam": "pasteurisation",
    "valve": "valve", "exchanger": "pasteurisation", "phosphatase": "pasteurisation",
    "controller": "controller", "diversion": "valve", "diverted": "valve",
    # supplier quality
    "antibiotic": "contamination", "residue": "contamination", "contaminated": "contamination",
    "contamination": "contamination", "pesticide": "contamination", "adulteration": "contamination",
    "adulterated": "contamination", "bacterial": "contamination", "bacteria": "contamination",
    "microbiological": "contamination", "pathogen": "contamination", "failed": "failed",
    "positive": "failed", "specification": "failed", "supplier": "supplier", "tanker": "supplier",
    "farm": "supplier", "raw": "raw", "milk": "milk", "pulp": "raw", "fruit": "raw",
    "silo": "raw", "blended": "raw", "consumed": "raw",
    # sensors
    "probe": "sensor", "sensor": "sensor", "logger": "sensor", "calibration": "sensor",
    "reference": "sensor", "thermometer": "sensor", "signal": "sensor", "battery": "sensor",
    "gap": "sensor", "disagreed": "sensor", "constant": "sensor",
    # actions
    "held": "hold", "hold": "hold", "quarantined": "hold", "quarantine": "hold",
    "withdrawn": "recall", "withdrawal": "recall", "withdraw": "recall", "recalled": "recall",
    "recall": "recall", "destroyed": "recall", "rejected": "recall",
    "traced": "trace", "trace": "trace", "forward": "trace", "downstream": "trace",
}


def compose_report(kind: str, rng: random.Random) -> tuple[str, str, str]:
    """One (summary, action_taken, outcome) triple for an incident kind."""
    r = RANGES[kind]
    vals = {k: rng.randint(*r[k]) if k != "temp" else round(rng.uniform(*r[k]), 1)
            for k in ("temp", "mins", "n", "m")}
    summary = f"{rng.choice(SYMPTOMS[kind]).format(**vals)}. {rng.choice(CONTEXT)}."
    return (summary, rng.choice(ACTIONS[kind]), rng.choice(OUTCOMES[kind]).format(**vals))


class VocabEmbedder:
    """Offline fallback embedder: a deterministic, IDF-weighted bag of words over
    the supply-chain vocabulary. Tokens are folded through SYNONYMS onto canonical
    terms, bare numbers are dropped, and each term gets its own dimension where
    there is room so cosine similarity reflects shared vocabulary rather than hash
    collisions. It is lexical, not semantic: it knows the synonyms it was given.
    Install sentence-transformers for real embeddings; nothing else changes."""

    name = "vocab-fallback"

    def __init__(self, corpus: list[str], dim: int = EMBED_DIM) -> None:
        self.dim = dim
        docs = [self._tokens(t) for t in corpus]
        n_docs = max(len(docs), 1)
        df: dict[str, int] = {}
        for toks in docs:
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1
        self.idf = {t: float(np.log((n_docs + 1) / (c + 1)) + 1.0) for t, c in df.items()}
        ordered = sorted(self.idf, key=lambda t: (-self.idf[t], t))
        self.slot = {tok: i % dim for i, tok in enumerate(ordered)}

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [SYNONYMS.get(w, w) for w in re.findall(r"[a-z]+", text.lower())
                if w not in STOPWORDS and len(w) >= 3]

    def _slot_for(self, tok: str) -> int:
        if tok in self.slot:
            return self.slot[tok]
        return (sum(ord(c) * (i + 1) for i, c in enumerate(tok)) * 2654435761) % self.dim

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, int] = {}
        for tok in self._tokens(text):
            counts[tok] = counts.get(tok, 0) + 1
        for tok, tf in counts.items():
            v[self._slot_for(tok)] += float((1.0 + np.log(tf)) * self.idf.get(tok, 1.0))
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def encode(self, texts, normalize_embeddings: bool = True):
        if isinstance(texts, str):
            return self._one(texts)
        return np.vstack([self._one(t) for t in texts])


def build_fallback_corpus() -> list[str]:
    """Every phrase the templates can produce. Built from the templates, not the
    database, so app.py constructs an identical embedder without reading rows back."""
    corpus = []
    for kind, phrases in SYMPTOMS.items():
        filled = {"temp": RANGES[kind]["temp"][0], "mins": RANGES[kind]["mins"][0],
                  "n": RANGES[kind]["n"][0], "m": RANGES[kind]["m"][0]}
        corpus.extend(p.format(**filled) for p in phrases)
        corpus.extend(ACTIONS[kind])
        corpus.extend(o.format(**filled) for o in OUTCOMES[kind])
    corpus.extend(CONTEXT)
    return corpus


def load_embedder(quiet: bool = False):
    """Real model if installed and reachable, offline fallback otherwise."""
    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        say("  sentence-transformers not installed -> using the offline vocab fallback")
        return VocabEmbedder(build_fallback_corpus())
    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        if model.get_sentence_embedding_dimension() != EMBED_DIM:
            raise RuntimeError("model dimension does not match vector(384)")
        say("  using sentence-transformers all-MiniLM-L6-v2")
        model.name = "all-MiniLM-L6-v2"
        return model
    except Exception as exc:
        say(f"  model unavailable ({type(exc).__name__}) -> using the offline vocab fallback")
        return VocabEmbedder(build_fallback_corpus())
