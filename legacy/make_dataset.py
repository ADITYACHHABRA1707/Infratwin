"""
Get data/ai4i2020.csv ready.

Order of preference:
  1. A file already at data/ai4i2020.csv            -> left alone.
  2. Download the real AI4I 2020 dataset from UCI    -> preferred.
  3. Generate a faithful stand-in                    -> only if 1 and 2 fail.

The stand-in is NOT random noise. It reproduces the failure rules published
with the AI4I 2020 dataset, so failure prevalence and the mix of failure modes
land close to the real file. Every generated file carries a sibling marker,
data/SYNTHETIC.txt, so nobody can mistake it for the real dataset.

Published rules (Matzka, S. 2020, AI4I 2020 Predictive Maintenance Dataset,
UCI Machine Learning Repository, CC BY 4.0):
  TWF  tool wear failure      tool fails at a wear value drawn from 200-240 min
  HDF  heat dissipation       (process_temp - air_temp) < 8.6 K and rpm < 1380
  PWF  power failure          power = torque * rpm_in_rad_per_s, outside 3500-9000 W
  OSF  overstrain failure     tool_wear * torque > 11000 / 12000 / 13000 (L/M/H)
  RNF  random failure         0.1% chance, independent of everything else

Usage:  python scripts/make_dataset.py
"""
from __future__ import annotations

import io
import math
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV = DATA / "ai4i2020.csv"
MARKER = DATA / "SYNTHETIC.txt"
URL = ("https://archive.ics.uci.edu/static/public/601/"
       "ai4i+2020+predictive+maintenance+dataset.zip")

N_ROWS = 10_000
SEED = 7

REQUIRED = ["Air temperature [K]", "Process temperature [K]",
            "Rotational speed [rpm]", "Torque [Nm]", "Tool wear [min]",
            "Machine failure", "TWF", "HDF", "PWF", "OSF", "RNF"]


def try_download() -> bool:
    """Fetch and unpack the real dataset. False if the network says no."""
    try:
        import urllib.request
        print(f"trying UCI download ...")
        with urllib.request.urlopen(URL, timeout=60) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            DATA.mkdir(parents=True, exist_ok=True)
            CSV.write_bytes(z.read(name))
        MARKER.unlink(missing_ok=True)
        print(f"  downloaded the real dataset -> {CSV.relative_to(ROOT)}")
        return True
    except Exception as exc:
        print(f"  download unavailable ({type(exc).__name__}). Falling back to the generator.")
        return False


def generate() -> pd.DataFrame:
    """Build a stand-in that follows the published AI4I failure rules."""
    rng = np.random.default_rng(SEED)
    n = N_ROWS

    # Quality variants, same proportions as the real file.
    qtype = rng.choice(["L", "M", "H"], size=n, p=[0.50, 0.30, 0.20])

    # Air temperature: random walk around 300 K, as described in the dataset.
    air = 300.0 + np.cumsum(rng.normal(0, 0.12, n))
    air = 300.0 + (air - air.mean()) / (air.std() + 1e-9) * 2.0

    # Process temperature sits about 10 K above air temperature. The spread of
    # the gap is what drives HDF, so it is tuned to put roughly 3.5% of rows
    # under the 8.6 K threshold.
    process = air + 10.0 + rng.normal(0, 0.78, n)

    torque = np.clip(rng.normal(40.0, 10.0, n), 3.8, 76.6)

    # Rotational speed is DERIVED from a target process power, not drawn
    # independently. power = torque * angular_velocity, so holding power near a
    # target makes torque and rpm negatively correlated -- which is what keeps
    # PWF rare in the real file. Drawing rpm independently makes almost every
    # row a power failure.
    power = rng.normal(6400.0, 1000.0, n)          # watts
    omega = power / torque                          # rad/s
    rpm_f = omega * 60.0 / (2.0 * math.pi)
    rpm = np.clip(rpm_f, 1168, 2886).round().astype(int)

    # Round to the precision that will be WRITTEN before evaluating any rule.
    # Rules applied to full-precision floats and then rounded on the way out
    # leave borderline rows whose flags contradict their own values, so anyone
    # recomputing the rules from the CSV gets different answers.
    air = air.round(1)
    process = process.round(1)
    torque = torque.round(1)

    # Recompute power from the stored integer rpm and rounded torque, so the PWF
    # rule is applied to exactly the values in the file.
    power = torque * (rpm * 2.0 * math.pi / 60.0)

    # Tool wear accumulates and resets at a per-cycle limit drawn from
    # 200-240 min, matching the dataset's description.
    wear = np.zeros(n, dtype=int)
    twf = np.zeros(n, dtype=int)
    w = 0
    limit = float(rng.uniform(200, 240))
    for i in range(n):
        w += int(rng.integers(0, 6))
        if w >= limit:
            # At the limit the tool is replaced 69 times and fails 51 times.
            if rng.random() < 51.0 / 120.0:
                twf[i] = 1
            wear[i] = min(w, 253)
            w = 0
            limit = float(rng.uniform(200, 240))
            continue
        wear[i] = min(w, 253)

    # ---- failure rules -----------------------------------------------------
    hdf = (((process - air) < 8.6) & (rpm < 1380)).astype(int)

    pwf = ((power < 3500) | (power > 9000)).astype(int)

    osf_limit = np.select([qtype == "L", qtype == "M"], [11000, 12000], default=13000)
    osf = ((wear * torque) > osf_limit).astype(int)

    rnf = (rng.random(n) < 0.001).astype(int)

    machine_failure = ((twf | hdf | pwf | osf | rnf) > 0).astype(int)

    # Written from the same rounded arrays the rules were evaluated against.
    return pd.DataFrame({
        "UDI": np.arange(1, n + 1),
        "Product ID": [f"{t}{47000 + i}" for i, t in enumerate(qtype)],
        "Type": qtype,
        "Air temperature [K]": air,
        "Process temperature [K]": process,
        "Rotational speed [rpm]": rpm,
        "Torque [Nm]": torque,
        "Tool wear [min]": wear,
        "Machine failure": machine_failure,
        "TWF": twf, "HDF": hdf, "PWF": pwf, "OSF": osf, "RNF": rnf,
    })


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)

    if CSV.exists():
        df = pd.read_csv(CSV)
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            print(f"{CSV} exists but is missing columns: {missing}")
            print("Delete it and re-run, or point CSV in seed.py at the right file.")
            return 1
        tag = " (synthetic)" if MARKER.exists() else ""
        print(f"{CSV.relative_to(ROOT)} already present{tag}: "
              f"{len(df):,} rows, {int(df['Machine failure'].sum()):,} failures")
        return 0

    if try_download():
        return 0

    df = generate()
    df.to_csv(CSV, index=False)
    MARKER.write_text(
        "data/ai4i2020.csv in this folder was GENERATED by scripts/make_dataset.py\n"
        "because the UCI download was unreachable.\n\n"
        "It follows the failure rules published with the AI4I 2020 dataset, so\n"
        "failure prevalence and the mix of failure modes are representative, but\n"
        "it is NOT the real dataset. For a submission that claims real data, delete\n"
        "this file and data/ai4i2020.csv, then re-run with internet access:\n"
        f"  {URL}\n"
    )

    counts = {c: int(df[c].sum()) for c in ["TWF", "HDF", "PWF", "OSF", "RNF"]}
    total = int(df["Machine failure"].sum())
    print(f"  generated {len(df):,} rows -> {CSV.relative_to(ROOT)}")
    print(f"  failures: {total:,} ({total / len(df) * 100:.2f}%)  by mode: {counts}")
    print(f"  marker written: {MARKER.relative_to(ROOT)}")
    print("  real file reference: 10,000 rows, 339 failures (3.39%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
