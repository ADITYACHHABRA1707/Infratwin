"""
InfraTwin seed.

Builds the asset graph, simulates the sensor stream, turns AI4I failure rows
into failure_events + maintenance_reports, and embeds the report text.

Run once, after sql/01_core.sql and sql/02_timeseries_*.sql:

    python seed.py

Scale knobs (environment variables):
    DAYS              days of sensor history            default 35
    READING_INTERVAL  one reading per ...               default '1 minute'
    BENCH_VECTORS     rows in report_bench              default 20000
    DATABASE_URL      connection string                 default localhost:5433

A laptop that is struggling can run a tenth of the data with:
    DAYS=4 python seed.py
"""
from __future__ import annotations

import os
import random
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
from pgvector.psycopg import register_vector

ROOT = Path(__file__).resolve().parent
DB = os.getenv("DATABASE_URL", "postgresql://postgres:infra@localhost:5433/postgres")
CSV = ROOT / "data" / "ai4i2020.csv"
DAYS = int(os.getenv("DAYS", "35"))
INTERVAL = os.getenv("READING_INTERVAL", "1 minute")
BENCH_VECTORS = int(os.getenv("BENCH_VECTORS", "20000"))
EMBED_DIM = 384

random.seed(7)
np.random.seed(7)

# Report text per failure mode, written the way a technician writes: the symptom
# first, then what was done.
#
# Each report is COMPOSED from a symptom phrase, a context line and an action
# phrase, with measured values substituted in. That matters: three fixed strings
# per mode would make every report in a mode identical, and the vector search
# would return three copies of the same row and look broken. Composition gives
# a few hundred distinct texts per mode, so nearest-neighbour results are
# genuinely different reports.
SYMPTOMS = {
    "TWF": [
        "Cutting tool worn past its limit at {wear} minutes, rough surface finish and torque up to {torque} Nm",
        "Tool wear counter hit {wear} minutes, vibration on the spindle increasing through the shift",
        "Dull tool at {wear} minutes causing slow cutting and visible surface defects on the part",
        "Tool edge broke down near {wear} minutes of wear, chatter marks across the finished face",
        "Progressive flank wear at {wear} minutes, cutting force climbing and torque reading {torque} Nm",
        "Insert chipped after {wear} minutes, dimensional drift on the last batch of parts",
    ],
    "HDF": [
        "Process temperature reached {temp} K while rotational speed stayed low at {rpm} rpm",
        "Heat dissipation failure, coolant could not carry the heat away and temperature held at {temp} K",
        "Overheating after a cooling fan blockage, process temperature {temp} K against a {rpm} rpm spindle",
        "Temperature climbed steadily to {temp} K, coolant flow too low at {rpm} rpm to remove the heat",
        "Thermal trip at {temp} K, the gap between air and process temperature collapsed below 8.6 K",
        "Heat build-up with the spindle turning at only {rpm} rpm, process side reading {temp} K",
    ],
    "PWF": [
        "Power failure, torque {torque} Nm against {rpm} rpm put process power outside the safe band",
        "Sudden power fluctuation, the motor drew abnormal current at {rpm} rpm",
        "Motor output unstable, speed fell to {rpm} rpm while torque spiked to {torque} Nm",
        "Drive tripped on power, {torque} Nm at {rpm} rpm is below the minimum process power",
        "Supply dip left the motor at {rpm} rpm, torque demand {torque} Nm could not be met",
        "Process power excursion, {torque} Nm and {rpm} rpm together exceeded the drive rating",
    ],
    "OSF": [
        "Overstrain failure, torque {torque} Nm under heavy load with the tool already at {wear} minutes",
        "Grinding noise from the spindle, torque {torque} Nm above the allowed limit for this product type",
        "Mechanical overload, the machine stalled at {torque} Nm with {wear} minutes of tool wear",
        "Wear and torque product exceeded the limit, {wear} minutes at {torque} Nm",
        "Spindle bound up under {torque} Nm, worn tool at {wear} minutes was the contributing factor",
        "Feed too aggressive for a tool at {wear} minutes, torque peaked at {torque} Nm before the stall",
    ],
    "RNF": [
        "Random shutdown with no clear cause, all readings normal at {temp} K and {rpm} rpm",
        "Unexplained stop, the controller logged a transient error with nothing abnormal in the trace",
        "Intermittent trip with no abnormal readings beforehand, {rpm} rpm and {torque} Nm at the time",
        "Machine stopped without a fault code, process temperature {temp} K was within range",
        "Spurious trip during a steady run at {rpm} rpm, no repeat on restart",
        "One-off dropout, sensors read normal before and after at {temp} K",
    ],
}

ACTIONS = {
    "TWF": [
        "Replaced the worn tool and reset the tool wear counter",
        "Swapped in a new tool and recalibrated the spindle",
        "Changed the tool insert and checked alignment on the holder",
        "Fitted a fresh cutter and shortened the replacement interval",
        "Replaced the tool and added an inspection step at 180 minutes of wear",
        "Changed the insert, dressed the holder and re-zeroed the offsets",
    ],
    "HDF": [
        "Cleaned the heat exchanger, refilled coolant and cleared the airflow path",
        "Replaced the cooling fan and flushed the coolant line",
        "Repaired the coolant pump and checked the hoses for leaks",
        "Cleared the blocked filter, topped up coolant and verified the flow switch",
        "Replaced the thermostat, bled the circuit and confirmed the temperature gap recovered",
        "Descaled the exchanger and raised the minimum spindle speed to help heat removal",
    ],
    "PWF": [
        "Replaced the faulty motor drive and tested the supply voltage",
        "Tightened the wiring connections and replaced the damaged contactor",
        "Rewound the motor and rebalanced the load",
        "Replaced the drive module and re-tuned the current limits",
        "Fixed the loose terminal, checked phase balance and reset the overload",
        "Swapped the contactor and corrected the supply to the drive cabinet",
    ],
    "OSF": [
        "Reduced the load, replaced the worn bearing and rechecked alignment",
        "Replaced the spindle bearing and lubricated the assembly",
        "Cleared the jam, inspected the gearbox and lowered the feed rate",
        "Replaced the bearing pair, re-shimmed the housing and reduced the depth of cut",
        "Freed the spindle, replaced the tool and capped the torque in the program",
        "Rebuilt the spindle head and lowered the feed to keep torque within limit",
    ],
    "RNF": [
        "Inspected wiring and sensors, restarted the machine, no fault found",
        "Updated the controller firmware and monitored for a repeat",
        "Replaced a loose connector and logged the event",
        "Reseated the IO card, cleared the fault log and left the machine on watch",
        "Checked earthing and shielding, no cause identified, event logged for trending",
        "Restarted and added extra logging to catch the next occurrence",
    ],
}

CONTEXT = [
    "Reported by the night shift operator",
    "Flagged during the routine round",
    "Picked up by the line supervisor",
    "Raised from the control room alarm",
    "Noticed by the operator at shift handover",
    "Found during planned maintenance",
    "Escalated after a second occurrence the same week",
]

# Plausible ranges per mode for the substituted values, so the numbers in the
# text agree with the failure mode being described.
VALUE_RANGES = {
    "TWF": {"wear": (195, 253), "torque": (38, 52), "temp": (308, 312), "rpm": (1350, 1700)},
    "HDF": {"wear": (40, 180), "torque": (42, 60), "temp": (313, 320), "rpm": (1180, 1375)},
    "PWF": {"wear": (20, 200), "torque": (55, 74), "temp": (308, 313), "rpm": (1170, 1300)},
    "OSF": {"wear": (200, 253), "torque": (58, 76), "temp": (309, 314), "rpm": (1250, 1500)},
    "RNF": {"wear": (10, 220), "torque": (32, 48), "temp": (308, 311), "rpm": (1400, 1800)},
}


def compose_report(ftype: str, rng: random.Random) -> tuple[str, str]:
    """Build one (summary, action_taken) pair for a failure mode."""
    r = VALUE_RANGES[ftype]
    values = {
        "wear": rng.randint(*r["wear"]),
        "torque": round(rng.uniform(*r["torque"]), 1),
        "temp": round(rng.uniform(*r["temp"]), 1),
        "rpm": rng.randint(*r["rpm"]),
    }
    symptom = rng.choice(SYMPTOMS[ftype]).format(**values)
    context = rng.choice(CONTEXT)
    action = rng.choice(ACTIONS[ftype])
    return f"{symptom}. {context}.", action

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "with", "for",
    "is", "was", "were", "be", "been", "no", "not", "it", "its", "this", "that",
    "from", "by", "as", "all", "up", "out", "off", "over", "under", "after",
}


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
# Maintenance vocabulary folded onto a canonical term, so an operator typing
# "overheating" matches a report that says "process temperature climbed". This is
# what lets the offline embedder behave sensibly without a language model.
SYNONYMS = {
    # thermal
    "overheating": "temperature", "overheat": "temperature", "hot": "temperature",
    "heat": "temperature", "thermal": "temperature", "temp": "temperature",
    "temperatures": "temperature", "warm": "temperature", "climbing": "rising",
    "climbed": "rising", "rose": "rising", "rise": "rising", "increasing": "rising",
    "build": "rising", "trip": "tripped", "tripping": "tripped",
    # cooling circuit
    "cooling": "coolant", "coolant": "coolant", "fan": "coolant",
    "exchanger": "coolant", "airflow": "coolant", "air": "coolant",
    "dissipation": "coolant", "flow": "coolant", "hoses": "coolant",
    "hose": "coolant", "descaled": "coolant", "filter": "coolant",
    # speed
    "rpm": "speed", "rotational": "speed", "spindle": "spindle", "slow": "low",
    "speeds": "speed",
    # tool wear
    "worn": "wear", "wear": "wear", "dull": "wear", "blunt": "wear",
    "insert": "tool", "cutter": "tool", "tool": "tool", "flank": "wear",
    "chipped": "wear", "edge": "wear",
    # power / electrical
    "current": "power", "voltage": "power", "supply": "power", "drive": "power",
    "power": "power", "electrical": "power", "contactor": "power",
    "wiring": "power", "terminal": "power", "phase": "power", "amps": "power",
    "fluctuation": "power", "dip": "power", "motor": "motor",
    # mechanical
    "grinding": "vibration", "noise": "vibration", "vibration": "vibration",
    "chatter": "vibration", "rattle": "vibration", "bearing": "bearing",
    "bearings": "bearing", "stall": "overload", "stalled": "overload",
    "jam": "overload", "jammed": "overload", "bound": "overload",
    "overload": "overload", "overstrain": "overload", "strain": "overload",
    "torque": "torque", "load": "load", "feed": "feed", "gearbox": "gearbox",
}


class VocabEmbedder:
    """Offline fallback embedder.

    A deterministic bag-of-words model over the maintenance vocabulary:

      * tokens are folded through SYNONYMS onto canonical terms, so
        "overheating" and "temperature climbing" land on the same dimension
      * bare numbers are dropped, because "1401 rpm" carries no similarity
        signal and crowds out the words that do
      * terms are weighted by inverse document frequency over the report
        corpus, so "the machine" counts for little and "coolant" counts
        for a lot
      * each term gets its own dimension where there is room, so cosine
        similarity reflects shared vocabulary rather than hash collisions

    It is lexical, not semantic: it knows the synonyms it was given and no
    others. Install sentence-transformers and run with network access to get
    real embeddings -- the schema, the index and every query are unchanged,
    which is the point.
    """

    name = "vocab-fallback"

    def __init__(self, corpus: list[str], dim: int = EMBED_DIM) -> None:
        self.dim = dim
        docs = [self._tokens(t) for t in corpus]
        n_docs = max(len(docs), 1)

        df: dict[str, int] = {}
        for toks in docs:
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1

        # Rarer term -> higher weight.
        self.idf = {
            tok: float(np.log((n_docs + 1) / (count + 1)) + 1.0)
            for tok, count in df.items()
        }
        # Stable dimension per term: most informative terms get their own slot.
        ordered = sorted(self.idf, key=lambda t: (-self.idf[t], t))
        self.slot = {tok: i % dim for i, tok in enumerate(ordered)}
        self.vocab_size = len(ordered)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        out = []
        for w in re.findall(r"[a-z]+", text.lower()):
            if w in STOPWORDS or len(w) < 3:
                continue
            out.append(SYNONYMS.get(w, w))
        return out

    def _slot_for(self, tok: str) -> int:
        if tok in self.slot:
            return self.slot[tok]
        # Unseen query word: deterministic slot so it is at least consistent.
        return (sum(ord(c) * (i + 1) for i, c in enumerate(tok)) * 2654435761) % self.dim

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, int] = {}
        for tok in self._tokens(text):
            counts[tok] = counts.get(tok, 0) + 1
        for tok, tf in counts.items():
            weight = (1.0 + np.log(tf)) * self.idf.get(tok, 1.0)
            v[self._slot_for(tok)] += float(weight)
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def encode(self, texts, normalize_embeddings: bool = True):
        if isinstance(texts, str):
            return self._one(texts)
        return np.vstack([self._one(t) for t in texts])


def build_fallback_corpus() -> list[str]:
    """Every phrase the templates can produce, used to fit the fallback's IDF.

    Built from the templates rather than the database so app.py can construct an
    identical embedder at query time without reading the corpus back.
    """
    corpus = []
    for ftype, phrases in SYMPTOMS.items():
        r = VALUE_RANGES[ftype]
        filled = {
            "wear": r["wear"][0], "torque": r["torque"][0],
            "temp": r["temp"][0], "rpm": r["rpm"][0],
        }
        for p in phrases:
            corpus.append(p.format(**filled))
        corpus.extend(ACTIONS[ftype])
    corpus.extend(CONTEXT)
    return corpus


def load_embedder(quiet: bool = False):
    """Real model if it is installed and reachable, fallback otherwise.

    Both return 384-dim L2-normalised vectors, so the schema, the HNSW index and
    every query are identical either way.
    """
    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        say("  sentence-transformers not installed -> using the offline vocab fallback")
        say("  (pip install sentence-transformers for real semantic search)")
        return VocabEmbedder(build_fallback_corpus())

    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        dim = model.get_sentence_embedding_dimension()
        if dim != EMBED_DIM:
            raise RuntimeError(
                f"model dimension {dim} does not match vector({EMBED_DIM}) in the schema"
            )
        say("  using sentence-transformers all-MiniLM-L6-v2")
        model.name = "all-MiniLM-L6-v2"
        return model
    except Exception as exc:
        say(f"  model unavailable ({type(exc).__name__}: {exc})")
        say("  -> using the offline vocab fallback so the demo still runs")
        return VocabEmbedder(build_fallback_corpus())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def detect_engine(cur) -> str:
    cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
    return "timescaledb" if cur.fetchone()[0] else "postgres"


def step(label: str):
    print(f"\n[{label}]")
    return time.perf_counter()


def done(t0: float, msg: str = "") -> None:
    print(f"  {msg}  ({time.perf_counter() - t0:.1f}s)" if msg
          else f"  ({time.perf_counter() - t0:.1f}s)")


# ---------------------------------------------------------------------------
def main() -> int:
    if not CSV.exists():
        print(f"missing {CSV}")
        print("run:  python scripts/make_dataset.py")
        return 1

    df = pd.read_csv(CSV)
    needed = ["Machine failure", "TWF", "HDF", "PWF", "OSF"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"{CSV} is missing columns {missing}")
        print("Column names must match the AI4I 2020 dataset. Check with:")
        print("  python -c \"import pandas as pd;"
              f" print(pd.read_csv(r'{CSV}').columns.tolist())\"")
        return 1

    conn = psycopg.connect(DB, autocommit=False)
    register_vector(conn)
    cur = conn.cursor()

    engine = detect_engine(cur)
    print(f"connected: {DB.rsplit('@', 1)[-1]}   time-series engine: {engine}")

    # -- reset -------------------------------------------------------------
    t = step("1/6 clearing previous run")
    cur.execute(
        "TRUNCATE assets, asset_dependencies, sensor_readings, failure_events, "
        "maintenance_reports, report_bench, audit_logs, demo_injection "
        "RESTART IDENTITY CASCADE"
    )
    done(t)

    # -- asset graph -------------------------------------------------------
    # Motor --powers--> Pump --feeds--> Line --feeds--> Plant
    # Edge direction is (asset_id -> dependent_id) = "dependent depends on asset",
    # so a motor failure walks upward to the plant.
    t = step("2/6 building the asset graph")

    def add_asset(name: str, typ: str, loc: str | None = None) -> int:
        cur.execute(
            "INSERT INTO assets(name, asset_type, location) VALUES (%s,%s,%s) "
            "RETURNING asset_id", (name, typ, loc))
        return cur.fetchone()[0]

    deps: list[tuple[int, int, str]] = []
    plant = add_asset("Plant-1", "Plant", "Vellore")
    motors: list[int] = []
    for l in range(1, 5):
        line = add_asset(f"Line-{l}", "Line", "Vellore")
        deps.append((line, plant, "feeds"))
        for p in range(1, 7):
            pump = add_asset(f"L{l}-Pump-{p}", "Pump", "Vellore")
            deps.append((pump, line, "feeds"))
            for m in range(1, 4):
                motor = add_asset(f"L{l}-P{p}-Motor-{m}", "Motor", "Vellore")
                deps.append((motor, pump, "powers"))
                motors.append(motor)

    cur.executemany("INSERT INTO asset_dependencies VALUES (%s,%s,%s)", deps)
    conn.commit()
    cur.execute("SELECT count(*) FROM assets")
    n_assets = cur.fetchone()[0]
    done(t, f"{n_assets} assets, {len(deps)} dependency edges "
            f"(1 plant / 4 lines / 24 pumps / {len(motors)} motors)")

    # -- sensor stream -----------------------------------------------------
    # Generated server-side: generate_series CROSS JOIN assets. Keeping this in
    # SQL avoids millions of Python round-trips and is the difference between
    # seconds and an hour.
    expected = n_assets * DAYS * (86400 // _interval_seconds(INTERVAL))
    t = step(f"3/6 simulating sensor readings (~{expected:,} rows, this is the slow part)")
    cur.execute(
        """
        INSERT INTO sensor_readings (time, asset_id, air_temp, process_temp, rpm, torque, tool_wear)
        SELECT t, a.asset_id,
               298 + 2*sin(extract(epoch FROM t)/86400*2*pi()) + random()*2,
               308 + random()*2.5,
               (1400 + random()*300)::int,
               30 + random()*20,
               (floor(extract(epoch FROM t)/3600)::bigint %% 250)::int
        FROM generate_series(now() - make_interval(days => %s), now(), %s::interval) AS t
        CROSS JOIN assets a
        """,
        (DAYS, INTERVAL),
    )
    conn.commit()
    cur.execute("SELECT count(*) FROM sensor_readings")
    n_readings = cur.fetchone()[0]
    done(t, f"{n_readings:,} readings over {DAYS} days")

    # -- inject the demo anomaly ------------------------------------------
    # Three motors run hot and slow for the last 3 hours. The z-score query
    # compares the last hour against each asset's own earlier history, so the
    # baseline window must stay clean -- hence "last 3 hours" only.
    t = step("4/6 injecting a demo anomaly")
    demo = random.sample(motors, 3)
    cur.execute(
        """UPDATE sensor_readings
              SET process_temp = process_temp + 9,
                  torque       = torque + 18,
                  rpm          = rpm - 150
            WHERE asset_id = ANY(%s) AND time > now() - interval '90 minutes'""",
        (demo,),
    )
    # Record it so scripts/refresh_demo.py can undo exactly this before
    # re-injecting, instead of stacking a second rise on the same rows.
    cur.execute("DELETE FROM demo_injection")
    cur.executemany(
        "INSERT INTO demo_injection(asset_id, from_time) "
        "VALUES (%s, now() - interval '90 minutes')",
        [(a,) for a in demo],
    )
    conn.commit()
    cur.execute("SELECT name FROM assets WHERE asset_id = ANY(%s) ORDER BY name", (demo,))
    names = [r[0] for r in cur.fetchall()]
    done(t, f"anomaly on {', '.join(names)}")

    # -- failures + reports ------------------------------------------------
    t = step("5/6 loading failures and writing reports")
    fails = df[df["Machine failure"] == 1]
    modes = ["TWF", "HDF", "PWF", "OSF"]

    event_rows = []
    for _, r in fails.iterrows():
        ftype = next((c for c in modes if r.get(c, 0) == 1), "RNF")
        event_rows.append((random.choice(motors), ftype))

    cur.executemany(
        "INSERT INTO failure_events(asset_id, event_time, failure_type) "
        "VALUES (%s, now() - random() * interval '34 days' - interval '4 hours', %s)",
        event_rows,
    )
    conn.commit()

    cur.execute("SELECT event_id, failure_type FROM failure_events ORDER BY event_id")
    events = cur.fetchall()

    rng = random.Random(7)
    reports = []
    for event_id, ftype in events:
        summary, action = compose_report(ftype, rng)
        reports.append((event_id, summary, action))

    uniq = len({s for _, s, _ in reports})
    print(f"  composed {len(reports)} reports, {uniq} distinct symptom texts")

    print("  embedding report text ...")
    embedder = load_embedder()
    vectors = embedder.encode([f"{s}. {a}" for _, s, a in reports],
                              normalize_embeddings=True)
    vectors = np.asarray(vectors, dtype=np.float32)

    cur.executemany(
        "INSERT INTO maintenance_reports(event_id, summary, action_taken, embedding) "
        "VALUES (%s,%s,%s,%s)",
        [(e, s, a, v) for (e, s, a), v in zip(reports, vectors)],
    )
    conn.commit()
    done(t, f"{len(events)} failure events, {len(reports)} reports embedded "
            f"({getattr(embedder, 'name', 'unknown')})")

    # -- benchmark vectors -------------------------------------------------
    t = step("6/6 generating benchmark vectors")
    vecs = np.random.randn(BENCH_VECTORS, EMBED_DIM).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    with cur.copy("COPY report_bench (embedding) FROM STDIN") as copy:
        for v in vecs:
            copy.write_row([v])
    conn.commit()
    done(t, f"{BENCH_VECTORS:,} random unit vectors in report_bench")

    # -- audit trail sanity ------------------------------------------------
    cur.execute("SELECT count(*) FROM audit_logs")
    n_audit = cur.fetchone()[0]

    print("\nseed complete.")
    print(f"  assets {n_assets} | readings {n_readings:,} | failures {len(events)} "
          f"| reports {len(reports)} | audit rows {n_audit}")
    nxt = ("sql/03_optimise_timescale.sql" if engine == "timescaledb"
           else "sql/03_optimise_portable.sql")
    print(f"  next:  psql -f {nxt}      (or: make optimise)")

    conn.close()
    return 0


def _interval_seconds(spec: str) -> int:
    """Rough seconds for an interval string, used only to size the progress note."""
    m = re.match(r"\s*(\d+)?\s*(second|minute|hour)s?\s*$", spec, re.I)
    if not m:
        return 60
    n = int(m.group(1) or 1)
    return n * {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]


if __name__ == "__main__":
    sys.exit(main())
