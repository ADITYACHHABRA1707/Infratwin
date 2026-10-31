"""
Generate the ER diagrams straight from the live database catalog, so they cannot drift
from the schema. Mermaid renders them in a headless browser.

    python docs/report/make_er.py <path-to>/mermaid.min.js

Writes docs/report/shots/er_overview.png, er_core.png, er_security_ledger.png and the
Mermaid sources next to them (docs/report/er_*.mmd).
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
import settings  # noqa: E402

OUT = Path(__file__).resolve().parent
SHOTS = OUT / "shots"

CORE = ["parties", "products", "units", "lots", "lot_flows", "shipments", "custody",
        "quality_events", "incidents", "incident_reports", "report_bench", "demo_injection"]
SECURITY_LEDGER = ["roles", "users", "audit_logs", "parties", "units", "shipments", "sensor_readings",
                   "blocks", "ledger_tx", "telemetry_windows"]
ALL = sorted(set(CORE + SECURITY_LEDGER))

TYPE_MAP = {"integer": "int", "bigint": "bigint", "text": "text", "real": "real", "numeric": "numeric",
            "boolean": "bool", "timestamp with time zone": "timestamptz", "USER-DEFINED": "vector(384)"}


def catalog(cur):
    cur.execute("""
        SELECT c.table_name, c.column_name, c.data_type, c.is_nullable
        FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND c.table_name = ANY(%s)
        ORDER BY c.table_name, c.ordinal_position""", (ALL,))
    cols: dict[str, list] = {}
    for t, c, d, n in cur.fetchall():
        cols.setdefault(t, []).append((c, TYPE_MAP.get(d, d), n == "YES"))
    cur.execute("""
        SELECT conrelid::regclass::text, contype, conkey, conname, confrelid::regclass::text,
               (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(conkey) WITH ORDINALITY k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k.attnum)
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace AND contype IN ('p', 'u', 'f')""")
    pk, uk, fk = {}, {}, []
    for rel, kind, _, name, ref, attrs in cur.fetchall():
        if rel.startswith("sensor_readings_") or rel not in ALL:
            continue
        if kind == "p":
            pk[rel] = set(attrs)
        elif kind == "u":
            uk.setdefault(rel, []).append(set(attrs))
        elif kind == "f" and ref in ALL:
            fk.append((rel, attrs, ref))
    return cols, pk, uk, fk


def mermaid(tables: list[str], cols, pk, uk, fk, attributes: bool = True) -> str:
    fkcols = {(t, a) for t, attrs, _ in fk for a in attrs}
    lines = ["erDiagram"]
    if attributes:
        for t in tables:
            lines.append(f"  {t} {{")
            for name, typ, _ in cols.get(t, []):
                tags = []
                if name in pk.get(t, set()):
                    tags.append("PK")
                if (t, name) in fkcols:
                    tags.append("FK")
                if any(u == {name} for u in uk.get(t, [])) and "PK" not in tags:
                    tags.append("UK")
                lines.append(f"    {typ.replace(' ', '_')} {name}" + (f" {','.join(tags)}" if tags else ""))
            lines.append("  }")
    seen = set()
    for child, attrs, parent in fk:
        if child not in tables or parent not in tables or (child, tuple(attrs), parent) in seen:
            continue
        seen.add((child, tuple(attrs), parent))
        nullable = any(n for c, _, n in cols[child] if c in attrs)
        one_to_one = any(u == set(attrs) for u in uk.get(child, [])) or pk.get(child) == set(attrs) and len(attrs) == 1
        left = "|o" if nullable else "||"
        right = "o|" if one_to_one else "o{"
        label = "_".join(attrs).replace("_id", "") or "fk"
        lines.append(f'  {parent} {left}--{right} {child} : "{label}"')
    return "\n".join(lines)


HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#fff;font-family:Segoe UI,Arial}} #g{{padding:24px;display:inline-block}}
</style></head><body><div id="g"><pre class="mermaid">{src}</pre></div>
<script src="{lib}"></script>
<script>mermaid.initialize({{startOnLoad:false,theme:'neutral',er:{{useMaxWidth:false}},
 themeVariables:{{fontSize:'{fs}px'}}}});
mermaid.run({{querySelector:'.mermaid'}}).then(()=>document.body.setAttribute('data-done','1'));</script>
</body></html>"""


def render(page, lib: str, name: str, src: str, fs: int) -> None:
    (OUT / f"{name}.mmd").write_text(src, encoding="utf-8")
    html = OUT / f"{name}.html"
    html.write_text(HTML.format(src=src, lib=Path(lib).as_uri(), fs=fs), encoding="utf-8")
    page.goto(html.as_uri())
    page.wait_for_selector("body[data-done]", timeout=60000)
    page.locator("#g").screenshot(path=str(SHOTS / f"{name}.png"))
    html.unlink()
    print("  wrote", name)


def main() -> int:
    lib = sys.argv[1]
    with psycopg.connect(settings.database_url()) as conn, conn.cursor() as cur:
        cols, pk, uk, fk = catalog(cur)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        page = b.new_page(viewport={"width": 2400, "height": 1600}, device_scale_factor=1.6)
        render(page, lib, "er_overview", mermaid(ALL, cols, pk, uk, fk, attributes=False), 16)
        render(page, lib, "er_core", mermaid(CORE, cols, pk, uk, fk), 14)
        render(page, lib, "er_security_ledger", mermaid(SECURITY_LEDGER, cols, pk, uk, fk), 14)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
