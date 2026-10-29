"""
Generate the InfraTwin ER diagram as SVG (and PNG if a converter is present).

    python scripts/make_er_diagram.py              # from the baked spec
    python scripts/make_er_diagram.py --from-db    # read the live catalog instead

The baked spec was verified against a live database; --from-db regenerates it
from pg_catalog so the diagram cannot silently drift from the schema.

Output: docs/er_diagram.svg, docs/er_diagram.png
"""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"

# ---------------------------------------------------------------------------
# Palette: dark slate text on white, one accent per architectural layer.
# ---------------------------------------------------------------------------
INK = "#1a1f36"
MUTED = "#6b7280"
LINE = "#9aa3b2"
BG = "#ffffff"

LAYERS = {
    "core":      {"accent": "#4338ca", "soft": "#eef2ff", "label": "Relational core"},
    "graph":     {"accent": "#0e7490", "soft": "#ecfeff", "label": "Graph (recursive)"},
    "timeseries": {"accent": "#b45309", "soft": "#fffbeb", "label": "Time-series"},
    "vector":    {"accent": "#9d174d", "soft": "#fdf2f8", "label": "Vector search"},
    "audit":     {"accent": "#15803d", "soft": "#f0fdf4", "label": "Audit"},
    "scaffold":  {"accent": "#6b7280", "soft": "#f9fafb", "label": "Demo / benchmark only"},
}

ROW_H = 21
HEAD_H = 34
NOTE_H = 17
PAD = 11
CHAR_W = 6.45          # approximate advance width of the 11px monospace used

# ---------------------------------------------------------------------------
# Baked spec: (column, type, flags) where flags is any of PK FK UQ NN
# ---------------------------------------------------------------------------
TABLES = {
    "assets": {
        "layer": "core", "x": 640, "y": 92, "pos": "Every physical thing in the plant",
        "cols": [
            ("asset_id", "serial", "PK"),
            ("name", "text", "UQ NN"),
            ("asset_type", "text", "NN"),
            ("location", "text", ""),
            ("install_date", "date", "NN"),
        ],
        "notes": ["CHECK asset_type IN (Plant, Line, Pump, Motor)",
                  "CHECK name is not blank"],
    },
    "asset_dependencies": {
        "layer": "graph", "x": 88, "y": 300, "pos": "Directed edge: who is affected",
        "cols": [
            ("asset_id", "int", "PK FK"),
            ("dependent_id", "int", "PK FK"),
            ("dependency_type", "text", "NN"),
        ],
        "notes": ["CHECK dependency_type IN (feeds, powers, cools)",
                  "CHECK asset_id <> dependent_id",
                  "Self-referencing M:N on assets"],
    },
    "sensor_readings": {
        "layer": "timeseries", "x": 560, "y": 330, "pos": "5.09M rows, partitioned by day",
        "cols": [
            ("time", "timestamptz", "NN"),
            ("asset_id", "int", "FK NN"),
            ("air_temp", "real", ""),
            ("process_temp", "real", ""),
            ("rpm", "int", ""),
            ("torque", "real", ""),
            ("tool_wear", "int", ""),
        ],
        "notes": ["PARTITION BY RANGE (time), 1 day per partition",
                  "(TimescaleDB: hypertable, same shape)",
                  "INDEX (asset_id, time DESC)"],
    },
    "readings_hourly": {
        "layer": "timeseries", "x": 560, "y": 648, "pos": "Pre-aggregated roll-up",
        "kind": "view",
        "cols": [
            ("bucket", "timestamptz", "UQ"),
            ("asset_id", "int", "UQ"),
            ("avg_temp", "double", ""),
            ("avg_torque", "double", ""),
            ("avg_rpm", "double", ""),
            ("max_wear", "int", ""),
        ],
        "notes": ["MATERIALIZED VIEW over sensor_readings",
                  "10 MB stands in for 551 MB"],
    },
    "failure_events": {
        "layer": "core", "x": 1176, "y": 300, "pos": "A confirmed failure",
        "cols": [
            ("event_id", "serial", "PK"),
            ("asset_id", "int", "FK NN"),
            ("event_time", "timestamptz", "NN"),
            ("failure_type", "text", "NN"),
        ],
        "notes": ["CHECK failure_type IN (TWF, HDF, PWF, OSF, RNF)",
                  "AI4I 2020 failure codes"],
    },
    "maintenance_reports": {
        "layer": "vector", "x": 1176, "y": 554, "pos": "What the technician wrote",
        "cols": [
            ("report_id", "serial", "PK"),
            ("event_id", "int", "FK UQ NN"),
            ("summary", "text", "NN"),
            ("action_taken", "text", "NN"),
            ("embedding", "vector(384)", ""),
        ],
        "notes": ["HNSW index on embedding (cosine)",
                  "UNIQUE event_id makes this 1:1"],
    },
    "audit_logs": {
        "layer": "audit", "x": 1176, "y": 830, "pos": "Append-only, written by trigger",
        "cols": [
            ("log_id", "bigserial", "PK"),
            ("entity", "text", "NN"),
            ("action", "text", "NN"),
            ("row_pk", "int", ""),
            ("log_time", "timestamptz", "NN"),
            ("details", "text", ""),
        ],
        "notes": ["Filled by trg_report_audit",
                  "CHECK action IN (INSERT, UPDATE, DELETE)"],
    },
    "report_bench": {
        "layer": "scaffold", "x": 88, "y": 648, "pos": "Benchmark scaffolding",
        "dashed": True,
        "cols": [
            ("id", "serial", "PK"),
            ("embedding", "vector(384)", "NN"),
        ],
        "notes": ["20,000 random unit vectors",
                  "Exact scan vs HNSW crossover"],
    },
    "demo_injection": {
        "layer": "scaffold", "x": 88, "y": 836, "pos": "Demo scaffolding",
        "dashed": True,
        "cols": [
            ("asset_id", "int", "PK FK"),
            ("from_time", "timestamptz", "NN"),
        ],
        "notes": ["Records the injected anomaly", "so it can be undone exactly"],
    },
}

# (from_table, to_table, from_card, to_card, label, route)
# cards: "1" one, "N" many, "01" zero-or-one
RELATIONS = [
    ("asset_dependencies", "assets", "N", "1", "asset_id → assets", "left-top"),
    ("asset_dependencies", "assets", "N", "1", "dependent_id → assets", "left-top2"),
    ("sensor_readings", "assets", "N", "1", "measures", "up"),
    ("failure_events", "assets", "N", "1", "occurs on", "right-top"),
    ("maintenance_reports", "failure_events", "1", "1", "documents", "up"),
    ("readings_hourly", "sensor_readings", "1", "N", "aggregates", "up"),
    ("audit_logs", "maintenance_reports", "N", "1", "trigger", "up-dashed"),
]


def text_w(s: str, char_w: float = CHAR_W) -> float:
    return len(s) * char_w


def box_size(name: str, spec: dict) -> tuple[float, float]:
    cols = spec["cols"]
    notes = spec.get("notes", [])
    widest_col = max(
        (text_w(f"{c}  {t}") + text_w(f) + 26 for c, t, f in cols), default=160)
    widest_note = max((text_w(n, 5.6) for n in notes), default=0)
    w = max(276, widest_col + 2 * PAD, widest_note + 2 * PAD,
            text_w(name, 9.0) + 2 * PAD)
    h = HEAD_H + 16 + len(cols) * ROW_H + PAD
    if notes:
        h += 6 + len(notes) * NOTE_H + 4
    return w, h


def flag_chip(f: str) -> tuple[str, str]:
    return {
        "PK": ("#4338ca", "PK"),
        "FK": ("#0e7490", "FK"),
        "UQ": ("#b45309", "UQ"),
        "NN": ("#5B6677", "NN"),
    }[f]


def build_svg() -> str:
    W, H = 1700, 1330
    out: list[str] = []
    a = out.append

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
      f'viewBox="0 0 {W} {H}" font-family="Inter, Segoe UI, Helvetica, Arial, sans-serif">')
    a('<defs>')
    a('<style>'
      '.tname{font-size:15px;font-weight:700;letter-spacing:.2px}'
      '.tsub{font-size:10.5px}'
      '.col{font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}'
      '.typ{font-size:10px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}'
      '.note{font-size:9.6px}'
      '.chip{font-size:8px;font-weight:700;font-family:ui-monospace,Menlo,monospace}'
      '.rel{font-size:10px;font-weight:600}'
      '.lg{font-size:11px}'
      '</style>')
    a('<marker id="crow" viewBox="0 0 12 12" refX="11" refY="6" markerWidth="11" '
      f'markerHeight="11" orient="auto"><path d="M11,6 L0,0 M11,6 L0,12 M11,6 L0,6" '
      f'fill="none" stroke="{LINE}" stroke-width="1.5"/></marker>')
    a('<marker id="one" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="11" '
      f'markerHeight="11" orient="auto"><path d="M6,1 L6,11" fill="none" '
      f'stroke="{LINE}" stroke-width="1.8"/></marker>')
    a('</defs>')
    a(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

    # ---- title ----
    a(f'<text x="48" y="50" font-size="27" font-weight="800" fill="{INK}">'
      f'InfraTwin &#8212; Database Relations</text>')
    a(f'<text x="48" y="74" font-size="12.5" fill="{MUTED}">'
      f'PostgreSQL 16 &#183; 3NF relational core &#183; time-series partitioning &#183; '
      f'self-referencing dependency graph &#183; pgvector 384-dim embeddings</text>')

    geom: dict[str, tuple[float, float, float, float]] = {}
    for name, spec in TABLES.items():
        w, h = box_size(name, spec)
        geom[name] = (spec["x"], spec["y"], w, h)

    # ---- relations first, so boxes paint over the line ends ----
    for frm, to, c_from, c_to, label, route in RELATIONS:
        fx, fy, fw, fh = geom[frm]
        tx, ty, tw, th = geom[to]
        dashed = "up-dashed" in route
        stroke = f'stroke="{LINE}" stroke-width="1.6"' + (
            ' stroke-dasharray="5 4"' if dashed else "")

        if route in ("left-top", "left-top2"):
            second = route == "left-top2"
            sx = fx + (fw * 0.34 if not second else fw * 0.66)
            sy = fy
            ex = tx + (30 if not second else 74)
            ey = ty + th
            midy = (sy + ey) / 2 + (0 if not second else 34)
            d = f"M{sx},{sy} C{sx},{midy} {ex},{midy} {ex},{ey}"
            lx = sx + (ex - sx) * 0.52
            ly = midy - 7
        elif route == "right-top":
            sx, sy = fx + fw / 2 + 30, fy
            ex, ey = tx + tw - 40, ty + th
            midy = (sy + ey) / 2
            d = f"M{sx},{sy} C{sx},{midy} {ex},{midy} {ex},{ey}"
            lx, ly = (sx + ex) / 2 - 20, midy - 6
        else:
            sx, sy = fx + fw / 2, fy
            ex, ey = tx + tw / 2, ty + th
            if abs(sx - ex) < 1:
                d = f"M{sx},{sy} L{ex},{ey}"
            else:
                midy = (sy + ey) / 2
                d = f"M{sx},{sy} C{sx},{midy} {ex},{midy} {ex},{ey}"
            lx, ly = (sx + ex) / 2 + 8, (sy + ey) / 2 - 4

        m_start = "crow" if c_from == "N" else "one"
        m_end = "crow" if c_to == "N" else "one"
        a(f'<path d="{d}" fill="none" {stroke} marker-start="url(#{m_start})" '
          f'marker-end="url(#{m_end})"/>')
        a(f'<rect x="{lx - 4}" y="{ly - 11}" width="{text_w(label, 5.9) + 10}" '
          f'height="15" rx="3" fill="{BG}" opacity="0.92"/>')
        a(f'<text x="{lx}" y="{ly}" class="rel" fill="{MUTED}">{html.escape(label)}</text>')

    # ---- boxes ----
    for name, spec in TABLES.items():
        x, y, w, h = geom[name]
        lay = LAYERS[spec["layer"]]
        accent, soft = lay["accent"], lay["soft"]
        dash = ' stroke-dasharray="6 4"' if spec.get("dashed") else ""

        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{BG}" '
          f'stroke="{accent}" stroke-width="1.7"{dash}/>')
        a(f'<path d="M{x},{y + HEAD_H} L{x},{y + 9} Q{x},{y} {x + 9},{y} '
          f'L{x + w - 9},{y} Q{x + w},{y} {x + w},{y + 9} L{x + w},{y + HEAD_H} Z" '
          f'fill="{accent}"/>')

        kind = spec.get("kind", "table")
        tag = "VIEW" if kind == "view" else ("DEMO" if spec.get("dashed") else "")
        a(f'<text x="{x + PAD}" y="{y + 22}" class="tname" fill="#ffffff">'
          f'{html.escape(name)}</text>')
        if tag:
            tw_ = text_w(tag, 6.2) + 12
            a(f'<rect x="{x + w - tw_ - PAD}" y="{y + 9}" width="{tw_}" height="16" '
              f'rx="8" fill="#ffffff" opacity="0.26"/>')
            a(f'<text x="{x + w - tw_ - PAD + 6}" y="{y + 21}" class="chip" '
              f'fill="#ffffff">{tag}</text>')

        a(f'<text x="{x + PAD}" y="{y + HEAD_H + 13}" class="tsub" fill="{MUTED}">'
          f'{html.escape(spec["pos"])}</text>')

        ry = y + HEAD_H + 16
        for i, (col, typ, flags) in enumerate(spec["cols"]):
            cy = ry + i * ROW_H + 14
            if i % 2 == 1:
                a(f'<rect x="{x + 1.5}" y="{cy - 14}" width="{w - 3}" height="{ROW_H}" '
                  f'fill="{soft}"/>')
            bold = ' font-weight="700"' if "PK" in flags else ""
            a(f'<text x="{x + PAD}" y="{cy}" class="col" fill="{INK}"{bold}>'
              f'{html.escape(col)}</text>')
            a(f'<text x="{x + PAD + text_w(col) + 10}" y="{cy}" class="typ" '
              f'fill="{MUTED}">{html.escape(typ)}</text>')

            cx = x + w - PAD
            for f in reversed(flags.split()):
                if not f:
                    continue
                fc, ft = flag_chip(f)
                cw = text_w(ft, 6.0) + 9
                cx -= cw + 3
                a(f'<rect x="{cx}" y="{cy - 10}" width="{cw}" height="13" rx="3" '
                  f'fill="{fc}" opacity="0.14"/>')
                a(f'<text x="{cx + 4.5}" y="{cy - 0.5}" class="chip" fill="{fc}">{ft}</text>')

        notes = spec.get("notes", [])
        if notes:
            ny = ry + len(spec["cols"]) * ROW_H + 8
            a(f'<line x1="{x + PAD}" y1="{ny - 4}" x2="{x + w - PAD}" y2="{ny - 4}" '
              f'stroke="{accent}" stroke-width="0.8" opacity="0.3"/>')
            for j, n in enumerate(notes):
                a(f'<text x="{x + PAD}" y="{ny + 10 + j * NOTE_H}" class="note" '
                  f'fill="{MUTED}">{html.escape(n)}</text>')

    # ---- legend ----
    lx, ly = 48, 1150
    a(f'<rect x="{lx}" y="{ly}" width="1604" height="152" rx="10" fill="#fcfcfd" '
      f'stroke="#e5e7eb"/>')
    a(f'<text x="{lx + 18}" y="{ly + 25}" font-size="12.5" font-weight="700" '
      f'fill="{INK}">Legend</text>')

    cx = lx + 18
    for key in ["core", "graph", "timeseries", "vector", "audit", "scaffold"]:
        lay = LAYERS[key]
        a(f'<rect x="{cx}" y="{ly + 38}" width="13" height="13" rx="3" '
          f'fill="{lay["accent"]}"/>')
        a(f'<text x="{cx + 20}" y="{ly + 49}" class="lg" fill="{INK}">'
          f'{lay["label"]}</text>')
        cx += 42 + text_w(lay["label"], 6.1)

    cx = lx + 18
    for f, desc in [("PK", "primary key"), ("FK", "foreign key"),
                    ("UQ", "unique"), ("NN", "not null")]:
        fc, _ = flag_chip(f)
        a(f'<rect x="{cx}" y="{ly + 68}" width="23" height="14" rx="3" fill="{fc}" '
          f'opacity="0.14"/>')
        a(f'<text x="{cx + 5}" y="{ly + 78.5}" class="chip" fill="{fc}">{f}</text>')
        a(f'<text x="{cx + 30}" y="{ly + 79}" class="lg" fill="{MUTED}">{desc}</text>')
        cx += 46 + text_w(desc, 6.1)

    a(f'<text x="{lx + 18}" y="{ly + 107}" class="lg" fill="{MUTED}">'
      f'Crow\'s foot = many &#183; single bar = one &#183; dashed line = written by trigger, '
      f'not by a foreign key &#183; dashed box = scaffolding, not part of the domain model'
      f'</text>')
    a(f'<text x="{lx + 18}" y="{ly + 131}" class="lg" fill="{MUTED}">'
      f'Normal form: 3NF throughout. assets is the single source of asset identity; '
      f'the hierarchy is an edge table, not a parent_id column, so one asset can have '
      f'many dependents and the graph is traversable in both directions.</text>')

    a('</svg>')
    return "\n".join(out)


def load_from_db() -> None:
    """Rewrite TABLES column lists from the live catalog."""
    import psycopg
    db = os.getenv("DATABASE_URL",
                   "postgresql://postgres:infra@localhost:5433/postgres")
    conn = psycopg.connect(db)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attnotnull,
               coalesce((SELECT true FROM pg_constraint k WHERE k.conrelid=c.oid
                         AND k.contype='p' AND a.attnum = ANY(k.conkey)), false),
               coalesce((SELECT true FROM pg_constraint k WHERE k.conrelid=c.oid
                         AND k.contype='f' AND a.attnum = ANY(k.conkey)), false),
               coalesce((SELECT true FROM pg_constraint k WHERE k.conrelid=c.oid
                         AND k.contype='u' AND a.attnum = ANY(k.conkey)), false)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname='public' AND c.relkind IN ('r','p','m')
          AND c.relname = ANY(%s)
        ORDER BY c.relname, a.attnum""", (list(TABLES),))

    found: dict[str, list] = {}
    for rel, col, typ, nn, pk, fk, uq in cur.fetchall():
        flags = " ".join(f for f, on in
                         [("PK", pk), ("FK", fk), ("UQ", uq), ("NN", nn and not pk)] if on)
        found.setdefault(rel, []).append((col, typ.replace("double precision", "double"),
                                          flags))
    for rel, cols in found.items():
        TABLES[rel]["cols"] = cols
    print(f"  refreshed {len(found)} tables from the live catalog")
    conn.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    if "--from-db" in sys.argv:
        try:
            load_from_db()
        except Exception as exc:
            print(f"  could not read the catalog ({type(exc).__name__}: {exc})")
            print("  falling back to the baked spec")

    svg_path = OUT / "er_diagram.svg"
    svg_path.write_text(build_svg(), encoding="utf-8")
    print(f"wrote {svg_path.relative_to(ROOT)}")

    slide_path = OUT / "er_diagram_slide.svg"
    slide_path.write_text(build_slide_svg(), encoding="utf-8")
    print(f"wrote {slide_path.relative_to(ROOT)}")

    png_path = OUT / "er_diagram.png"
    for cmd in (
        ["rsvg-convert", "-w", "2400", "-o", str(png_path), str(svg_path)],
        ["inkscape", str(svg_path), "--export-type=png", "--export-width=2400",
         f"--export-filename={png_path}"],
        ["cairosvg", str(svg_path), "-o", str(png_path), "--output-width", "2400"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                print(f"wrote {png_path.relative_to(ROOT)}")
                return 0
            except subprocess.CalledProcessError as exc:
                print(f"  {cmd[0]} failed: {exc.stderr.decode()[:200]}")
    print("  no SVG->PNG converter found; the SVG opens in any browser")
    return 0




# ---------------------------------------------------------------------------
# Slide variant: wide aspect (2100x1000), domain tables only, one note each.
# A projected diagram has to be readable from the back of a room, so the
# scaffolding tables and the secondary notes are dropped rather than shrunk.
# ---------------------------------------------------------------------------
SLIDE_POS = {
    "asset_dependencies":  (52, 150),
    "assets":              (695, 56),
    "failure_events":      (1330, 150),
    "maintenance_reports": (1955, 56),
    "sensor_readings":     (695, 400),
    "readings_hourly":     (695, 772),
    "audit_logs":          (1955, 430),
}

SLIDE_RELS = [
    ("asset_dependencies", "assets", "right", "asset_id + dependent_id"),
    ("failure_events", "assets", "left", "occurs on"),
    ("sensor_readings", "assets", "up", "measures"),
    ("readings_hourly", "sensor_readings", "up", "aggregates"),
    ("maintenance_reports", "failure_events", "left", "documents 1:1"),
    ("audit_logs", "maintenance_reports", "up-dashed", "trigger"),
]


def build_slide_svg() -> str:
    W, H = 2400, 1150
    keep = list(SLIDE_POS)
    out: list[str] = []
    a = out.append

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
      f'viewBox="0 0 {W} {H}" font-family="Inter, Segoe UI, Helvetica, Arial, sans-serif">')
    a('<defs><style>'
      '.tname{font-size:24px;font-weight:700}'
      '.col{font-size:18px;font-family:ui-monospace,Menlo,Consolas,monospace}'
      '.typ{font-size:14.5px;font-family:ui-monospace,Menlo,Consolas,monospace}'
      '.note{font-size:14.5px}.chip{font-size:11px;font-weight:700;font-family:ui-monospace,Menlo,monospace}'
      '.rel{font-size:15px;font-weight:600}.lg{font-size:15px}'
      '</style>'
      f'<marker id="crow2" viewBox="0 0 12 12" refX="11" refY="6" markerWidth="12" '
      f'markerHeight="12" orient="auto"><path d="M11,6 L0,0 M11,6 L0,12 M11,6 L0,6" '
      f'fill="none" stroke="{LINE}" stroke-width="1.7"/></marker>'
      f'<marker id="one2" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="12" '
      f'markerHeight="12" orient="auto"><path d="M6,1 L6,11" fill="none" '
      f'stroke="{LINE}" stroke-width="2"/></marker></defs>')
    a(f'<rect width="{W}" height="{H}" fill="{BG}"/>')

    ROW, HD, NT, P = 32, 46, 25, 15

    def size(name):
        spec = TABLES[name]
        cols = spec["cols"]
        widest = max(text_w(f"{c}  {t}", 10.8) + 46 for c, t, _ in cols)
        note = spec.get("notes", [""])[0]
        w = max(330, widest + 2 * P, text_w(note, 8.2) + 2 * P,
                text_w(name, 14.2) + 2 * P)
        h = HD + 8 + len(cols) * ROW + P + (6 + NT if note else 0)
        return w, h

    geom = {}
    for n in keep:
        x, y = SLIDE_POS[n]
        w, h = size(n)
        geom[n] = (x, y, w, h)

    # relations under the boxes
    for frm, to, route, label in SLIDE_RELS:
        fx, fy, fw, fh = geom[frm]
        tx, ty, tw, th = geom[to]
        dashed = route.endswith("dashed")
        stroke = (f'stroke="{LINE}" stroke-width="1.9"'
                  + (' stroke-dasharray="6 5"' if dashed else ""))
        if route == "right":
            sx, sy = fx + fw, fy + fh / 2
            ex, ey = tx, ty + th / 2
            d = f"M{sx},{sy} C{sx + 46},{sy} {ex - 46},{ey} {ex},{ey}"
            lx, ly = (sx + ex) / 2 - text_w(label, 8.6) / 2, (sy + ey) / 2 - 11
        elif route.startswith("left"):
            sx, sy = fx, fy + fh / 2
            ex, ey = tx + tw, ty + th / 2
            d = f"M{sx},{sy} C{sx - 46},{sy} {ex + 46},{ey} {ex},{ey}"
            lx, ly = (sx + ex) / 2 - text_w(label, 8.6) / 2, (sy + ey) / 2 - 11
        else:
            sx, sy = fx + fw / 2, fy
            ex, ey = tx + tw / 2, ty + th
            midy = (sy + ey) / 2
            d = (f"M{sx},{sy} L{ex},{ey}" if abs(sx - ex) < 1
                 else f"M{sx},{sy} C{sx},{midy} {ex},{midy} {ex},{ey}")
            lx, ly = (sx + ex) / 2 + 12, (sy + ey) / 2 + 4
        a(f'<path d="{d}" fill="none" {stroke} marker-start="url(#crow2)" '
          f'marker-end="url(#one2)"/>')
        a(f'<rect x="{lx - 8}" y="{ly - 16}" width="{text_w(label, 8.6) + 16}" '
          f'height="22" rx="5" fill="{BG}" opacity="0.95"/>')
        a(f'<text x="{lx}" y="{ly}" class="rel" fill="{MUTED}">{html.escape(label)}</text>')

    for n in keep:
        spec = TABLES[n]
        x, y, w, h = geom[n]
        lay = LAYERS[spec["layer"]]
        accent, soft = lay["accent"], lay["soft"]
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{BG}" '
          f'stroke="{accent}" stroke-width="2"/>')
        a(f'<path d="M{x},{y + HD} L{x},{y + 10} Q{x},{y} {x + 10},{y} '
          f'L{x + w - 10},{y} Q{x + w},{y} {x + w},{y + 10} L{x + w},{y + HD} Z" '
          f'fill="{accent}"/>')
        a(f'<text x="{x + P}" y="{y + 31}" class="tname" fill="#ffffff">'
          f'{html.escape(n)}</text>')
        if spec.get("kind") == "view":
            a(f'<rect x="{x + w - 68}" y="{y + 12}" width="54" height="21" rx="10" '
              f'fill="#ffffff" opacity="0.28"/>')
            a(f'<text x="{x + w - 61}" y="{y + 27}" class="chip" fill="#ffffff">VIEW</text>')

        ry = y + HD + 8
        for i, (col, typ, flags) in enumerate(spec["cols"]):
            cy = ry + i * ROW + 22
            if i % 2 == 1:
                a(f'<rect x="{x + 2}" y="{cy - 22}" width="{w - 4}" height="{ROW}" '
                  f'fill="{soft}"/>')
            bold = ' font-weight="700"' if "PK" in flags else ""
            a(f'<text x="{x + P}" y="{cy}" class="col" fill="{INK}"{bold}>'
              f'{html.escape(col)}</text>')
            a(f'<text x="{x + P + text_w(col, 10.8) + 14}" y="{cy}" class="typ" '
              f'fill="{MUTED}">{html.escape(typ)}</text>')
            cx = x + w - P
            for f in reversed(flags.split()):
                if not f:
                    continue
                fc, ft = flag_chip(f)
                cw = text_w(ft, 8.4) + 13
                cx -= cw + 5
                a(f'<rect x="{cx}" y="{cy - 15}" width="{cw}" height="20" rx="5" '
                  f'fill="{fc}" opacity="0.15"/>')
                a(f'<text x="{cx + 6.5}" y="{cy - 1}" class="chip" fill="{fc}">{ft}</text>')

        note = spec.get("notes", [""])[0] if spec.get("notes") else ""
        if note:
            ny = ry + len(spec["cols"]) * ROW + 14
            a(f'<line x1="{x + P}" y1="{ny - 5}" x2="{x + w - P}" y2="{ny - 5}" '
              f'stroke="{accent}" stroke-width="0.9" opacity="0.32"/>')
            a(f'<text x="{x + P}" y="{ny + 15}" class="note" fill="{MUTED}">'
              f'{html.escape(note)}</text>')

    # compact one-line legend
    cx, cy = 52, 1105
    for key in ["core", "graph", "timeseries", "vector", "audit"]:
        lay = LAYERS[key]
        a(f'<rect x="{cx}" y="{cy - 13}" width="17" height="17" rx="4" '
          f'fill="{lay["accent"]}"/>')
        a(f'<text x="{cx + 25}" y="{cy}" class="lg" fill="{INK}">{lay["label"]}</text>')
        cx += 56 + text_w(lay["label"], 8.2)
    a(f'<text x="{cx + 20}" y="{cy}" class="lg" fill="{MUTED}">'
      f'crow\'s foot = many &#183; bar = one &#183; dashed = written by trigger &#183; '
      f'3NF &#183; 2 scaffolding tables omitted</text>')

    a('</svg>')
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
