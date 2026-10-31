"""
Build the submission report (PDF) from the live project:

  * the data dictionary and constraints come from the database catalog,
  * the commit table comes from `git log`,
  * terminal evidence comes from docs/report/terminal/*.txt (real captured output),
  * screenshots come from docs/report/shots/*.png (real browser captures).

    python docs/report/build_report.py            ->  docs/report/ChainTwin_Report.pdf

Team names, reg. numbers, URLs: edit docs/report/team.json first.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

import psycopg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
SHOTS, TERM = HERE / "shots", HERE / "terminal"
sys.path.insert(0, str(ROOT))
import settings  # noqa: E402

TEAM = json.loads((HERE / "team.json").read_text(encoding="utf-8"))
esc = html.escape


# ----------------------------------------------------------------------------- helpers
def term(name: str, title: str = "", lines: tuple[int, int] | None = None, text: str | None = None) -> str:
    raw = text if text is not None else (TERM / name).read_text(encoding="utf-8", errors="replace")
    rows = raw.rstrip().splitlines()
    if lines:
        rows = rows[lines[0]:lines[1]]
    body = esc("\n".join(r[:175] for r in rows))
    cap = f'<div class="termtitle">{esc(title)}</div>' if title else ""
    return f'<div class="termbox">{cap}<pre class="term">{body}</pre></div>'


def sql_section(n: int) -> str:
    """One numbered block of the captured psql run, without psql's echo-of-\\echo noise."""
    raw = (TERM / "sql_demo.txt").read_text(encoding="utf-8")
    keep = [l for l in raw.splitlines()
            if not l.startswith(("\\echo", "\\pset", "\\set", "Pager usage", "--  ", "-- ===", "-- Joins", "-- constraints",
                                 "-- reaches", "-- Everything", "-- ChainTwin", "--   docker"))]
    txt = "\n".join(keep)
    parts = re.split(r"(?m)^=== (\d+)\. ", txt)
    # parts = [pre, '1', body1, '2', body2, ...]
    for i in range(1, len(parts), 2):
        if int(parts[i]) == n:
            return "=== " + parts[i] + ". " + parts[i + 1].rstrip()
    return ""


def img(name: str, caption: str, width: str = "100%", cls: str = "") -> str:
    p = SHOTS / name
    if not p.exists():
        return f'<p class="missing">[missing screenshot {esc(name)}]</p>'
    return (f'<figure class="{cls}"><img src="{p.as_uri()}" style="width:{width}"/>'
            f'<figcaption>{esc(caption)}</figcaption></figure>')


def table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    th = "".join(f"<th>{h}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'


# ----------------------------------------------------------------------------- data from the DB
def data_dictionary() -> str:
    out = []
    with psycopg.connect(settings.database_url()) as conn, conn.cursor() as cur:
        cur.execute("""SELECT c.relname FROM pg_class c WHERE c.relnamespace='public'::regnamespace
                       AND c.relkind IN ('r','p') AND c.relname !~ '^sensor_readings_p' ORDER BY 1""")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            cur.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                                  pg_get_expr(d.adbin, d.adrelid)
                           FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                           WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", (t,))
            cols = cur.fetchall()
            cur.execute("""SELECT contype, conname, pg_get_constraintdef(oid), conkey FROM pg_constraint
                           WHERE conrelid = %s::regclass AND contype IN ('p','u','f','c')""", (t,))
            cons = cur.fetchall()
            cur.execute("SELECT attnum, attname FROM pg_attribute WHERE attrelid = %s::regclass", (t,))
            names = dict(cur.fetchall())
            per_col: dict[str, list[str]] = {}
            table_level = []
            for kind, name, definition, key in cons:
                label = {"p": "PK", "u": "UNIQUE", "f": "FK", "c": "CHECK"}[kind]
                if kind == "c" and len(key or []) != 1:
                    table_level.append(f"CHECK {definition[6:]}")
                    continue
                if kind in ("p", "u", "f", "c") and key and len(key) == 1:
                    txt = label if kind in ("p", "u") else (
                        "FK " + definition.split("REFERENCES")[1].strip() if kind == "f" else "CHECK " + definition[6:])
                    per_col.setdefault(names[key[0]], []).append(txt)
                else:
                    table_level.append(f"{label} {definition}")
            rows = []
            for n, ty, nullable, default in cols:
                rows.append([f"<b>{esc(n)}</b>", esc(ty), "yes" if nullable else "NOT NULL",
                             esc((default or "")[:34]), esc("; ".join(per_col.get(n, []))[:120])])
            extra = "".join(f"<li>{esc(x[:230])}</li>" for x in table_level)
            out.append(f'<h4>{esc(t)}</h4>' + table(["Column", "Type", "Null?", "Default", "Key / constraint"], rows, "dd")
                       + (f'<ul class="tl">{extra}</ul>' if extra else ""))
    return "\n".join(out)


def commit_table() -> str:
    log = subprocess.run(["git", "log", "--all", "--reverse", "--format=%h|%an|%ad|%s", "--date=short"],
                         cwd=ROOT, capture_output=True, text=True).stdout.strip().splitlines()
    gh = {m["github"].lower(): m for m in TEAM["members"]}
    rows = []
    for line in log:
        h, author, date, msg = line.split("|", 3)
        br = subprocess.run(["git", "branch", "-a", "--contains", h, "--format=%(refname:short)"], cwd=ROOT,
                            capture_output=True, text=True).stdout.split()
        m = gh.get(author.lower())
        who = f"{esc(m['name'])} ({esc(m['reg_no'])})" if m else esc(author)
        rows.append([who, esc(author), h, esc(", ".join(br)), date, esc(msg)])
    return table(["Member", "GitHub user", "Commit ID", "Branch(es)", "Date", "Commit message"], rows, "dd")


# ----------------------------------------------------------------------------- the document
CSS = """
@page { size: A4; margin: 16mm 14mm 16mm 14mm; }
@page :first { margin: 0; }
*{box-sizing:border-box} body{font-family:'Segoe UI',Arial,sans-serif;font-size:10pt;line-height:1.42;color:#1d2433}
h1{font-size:19pt;color:#1e2a5a;border-bottom:3px solid #4338ca;padding-bottom:4px;margin:26px 0 10px;page-break-before:always}
h1.first{page-break-before:avoid}
h2{font-size:13.5pt;color:#2b3a8a;margin:18px 0 6px} h3{font-size:11.5pt;margin:12px 0 4px;color:#33406d} h4{font-size:10pt;margin:12px 0 3px;color:#1e2a5a;font-family:Consolas,monospace}
p{margin:4px 0 7px} ul{margin:3px 0 7px 18px;padding:0} li{margin:1px 0}
table{border-collapse:collapse;width:100%;margin:6px 0 10px;font-size:8.8pt;page-break-inside:auto}
tr{page-break-inside:avoid} th{background:#e8ebfb;color:#1e2a5a;text-align:left} th,td{border:1px solid #c5cbe0;padding:3px 6px;vertical-align:top}
table.dd{font-size:8pt} table.dd td{padding:2px 5px} ul.tl{font-size:8pt;margin:2px 0 4px 16px;font-family:Consolas,monospace;color:#444}
figure{margin:8px 0 12px;page-break-inside:avoid;text-align:center} figure img{border:1px solid #cfd4e6;max-width:100%}
figcaption{font-size:8.6pt;color:#555;margin-top:3px;font-style:italic}
.pair{display:flex;gap:8px;justify-content:center;align-items:flex-start} .pair figure{flex:1;min-width:0}
.termbox{margin:6px 0 10px;page-break-inside:avoid;border:1px solid #2a2f45;border-radius:4px;overflow:hidden}
.termtitle{background:#2a2f45;color:#c8cdf0;font:8pt Consolas,monospace;padding:2px 8px}
pre.term{margin:0;background:#12151f;color:#d7dcf5;font:7.3pt/1.28 Consolas,'Courier New',monospace;padding:6px 8px;white-space:pre-wrap;word-break:break-all}
code{font-family:Consolas,monospace;background:#eef0fa;padding:0 3px;border-radius:2px;font-size:9pt}
.note{background:#fff7e0;border-left:4px solid #e0a800;padding:6px 10px;margin:8px 0;font-size:9.4pt}
.ok{color:#0a7a3d;font-weight:600} .todo{color:#b42318;font-weight:600} .part{color:#a15c00;font-weight:600}
.cover{height:292mm;overflow:hidden;padding:30mm 22mm;background:linear-gradient(160deg,#1e2a5a 0%,#2f3d94 55%,#4338ca 100%);color:#fff;page-break-after:always}
.cover h1{border:0;color:#fff;font-size:30pt;margin:0 0 6px;page-break-before:avoid} .cover .sub{font-size:13pt;opacity:.9;margin-bottom:26px}
.cover table{background:#fff;color:#1d2433;font-size:10pt;margin-top:14px} .cover td,.cover th{padding:6px 9px}
.cover .meta{font-size:10.5pt;line-height:1.7;margin-top:12px}
.gantt td.b{background:#4338ca;border-color:#4338ca} .gantt td.d{background:#9aa4e8;border-color:#9aa4e8} .gantt th,.gantt td{text-align:center;font-size:8pt;padding:3px 2px}
.gantt td.l{text-align:left;width:34%;font-size:8.6pt} .gantt td.e{background:#fafbff}
.landscape img{width:100%}
"""


def gantt() -> str:
    weeks = [f"W{i}" for i in range(1, 11)]
    plan = [("Domain choice, problem statement, proposal", 1, 1), ("ER diagram, relational schema, normalisation", 1, 3),
            ("Schema, constraints, indexes, seed data", 2, 4), ("Views, procedure, triggers, transactions", 4, 5),
            ("Blockchain ledger and verification", 4, 6), ("ORM layer, authentication, RBAC, REST API", 5, 7),
            ("Dashboard, CRUD, search, pagination, validation", 6, 8), ("Backup/restore, Docker, CI/CD", 8, 9),
            ("Deployment, testing, report, demo", 9, 10)]
    head = "<tr><th>Task</th>" + "".join(f"<th>{w}</th>" for w in weeks) + "</tr>"
    body = ""
    for name, a, b in plan:
        body += f'<tr><td class="l">{esc(name)}</td>' + "".join(
            f'<td class="{"b" if a <= i <= b else "e"}"></td>' for i in range(1, 11)) + "</tr>"
    return f'<table class="gantt">{head}{body}</table>'


def build_html() -> str:
    m = TEAM["members"]
    team_rows = "".join(f"<tr><td>{i + 1}</td><td>{esc(x['name'])}</td><td>{esc(x['reg_no'])}</td><td>{esc(x['github'])}</td></tr>"
                        for i, x in enumerate(m))
    resp_rows = [[esc(x["name"]), esc(x["role"])] for x in m]
    P: list[str] = []

    # ------------------------------------------------------------------ cover
    P.append(f"""<div class="cover"><h1>{esc(TEAM['project_title'].split(':')[0])}</h1>
<div class="sub">{esc(TEAM['project_title'].split(':',1)[1].strip().capitalize())}</div>
<div class="meta"><b>Team {esc(TEAM['team_name'])}</b><br><b>{esc(TEAM['course'])}</b><br>School of Computer Science and Engineering, VIT Vellore<br>
Faculty: {esc(TEAM['faculty'])} &nbsp;|&nbsp; Class numbers: {esc(TEAM['class_numbers'])}<br>
Submission for {esc(TEAM['assessments'])}: one report per team, uploaded by every member.</div>
<table><tr><th>#</th><th>Team member</th><th>Reg. No.</th><th>GitHub username</th></tr>{team_rows}</table>
<table><tr><th colspan=2>Links</th></tr>
<tr><td>GitHub repository</td><td>{esc(TEAM['github_repo_url'])}</td></tr>
<tr><td>Live URL</td><td>{esc(TEAM['live_url'])}</td></tr>
<tr><td>Docker Hub image</td><td>{esc(TEAM['docker_hub_url'])}</td></tr></table></div>""")

    # ------------------------------------------------------------------ contents + URLs
    P.append("""<h1 class="first">Contents and resource links</h1>
<table><tr><th>Section</th><th>Marks</th><th>Where in this report</th></tr>
<tr><td>1. Problem statement and project planning</td><td>2</td><td>Section 1</td></tr>
<tr><td>2. Database design (ER, schema, keys, constraints, normalisation)</td><td>6</td><td>Section 2</td></tr>
<tr><td>3. Database implementation (CRUD, joins, views, aggregates, transactions, procedure, trigger, ORM)</td><td>8</td><td>Section 3</td></tr>
<tr><td>4. Application development</td><td>4</td><td>Section 4</td></tr>
<tr><td>5. Authentication, authorisation and database security</td><td>5</td><td>Section 5</td></tr>
<tr><td>6. Professional development practices</td><td>5</td><td>Section 6</td></tr>
<tr><td>Execution evidence (inputs, processing, outputs)</td><td>-</td><td>Section 7</td></tr>
<tr><td>Improvements</td><td>-</td><td>Section 8</td></tr>
<tr><td>Requirement coverage and open items</td><td>-</td><td>Section 9</td></tr></table>
<h2>URLs, tools, datasets and APIs used</h2>""" + table(["Resource", "Link / detail"], [
        ["Project repository", esc(TEAM["github_repo_url"])],
        ["Live deployment", esc(TEAM["live_url"])],
        ["Docker Hub image", esc(TEAM["docker_hub_url"])],
        ["PostgreSQL (RDBMS)", "https://www.postgresql.org/ , image timescale/timescaledb-ha:pg16 (ships pgvector)"],
        ["pgvector (vector search)", "https://github.com/pgvector/pgvector"],
        ["SQLAlchemy 2.0 (ORM)", "https://www.sqlalchemy.org/"],
        ["Streamlit (dashboard framework)", "https://streamlit.io/"],
        ["FastAPI + Uvicorn (REST API)", "https://fastapi.tiangolo.com/"],
        ["bcrypt / PyJWT (hashing, tokens)", "https://pypi.org/project/bcrypt/ , https://pyjwt.readthedocs.io/"],
        ["sentence-transformers all-MiniLM-L6-v2 (optional embeddings)", "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"],
        ["Mermaid (ER diagram rendering)", "https://mermaid.js.org/"],
        ["Playwright (screenshots)", "https://playwright.dev/python/"],
        ["Dataset", "None external. The dairy network, quantities, 681k sensor readings and incident texts are <b>simulated</b> by seed.py (the original InfraTwin project used the public AI4I 2020 dataset; ChainTwin does not)."],
    ]))

    # ------------------------------------------------------------------ 1
    P.append(f"""<h1>1. Problem statement and project planning</h1>
<h2>1.1 Project title</h2><p><b>{esc(TEAM['project_title'])}</b></p>
<h2>1.2 Problem statement</h2>
<p>A dairy sells yoghurt made from milk that came from three co-operatives, passed through a cold store, two pasteurisers and a
packing line, then travelled in reefer trucks to hubs and stores. When a thermometer drifts or a lab result fails, the business must
answer, within minutes: <i>which lots were affected, where are they now, who held them, and can the records be trusted?</i>
Today these facts live in separate systems (sensor logs, ERP, lab sheets, paper hand-offs), so the answer is slow and a recall
over-reaches: everything produced since the bad lot arrived is pulled. Records also pass between companies that do not fully trust each other.</p>
<p>This project stores sensors, lot genealogy and chain of custody in one relational database, answers those questions with SQL
(joins, recursive CTEs, window aggregates, vector search), and anchors hashes and signatures of the records on a hash-chained ledger so
later edits are detectable. Access is secured with authentication and role-based authorisation.</p>
<h2>1.3 Objectives</h2><ul>
<li>Design a normalised (3NF) relational schema of 19 tables with keys, constraints and indexes, derived from an ER diagram.</li>
<li>Trace any lot forward (recall scope with exposure share) and backward (provenance) using a recursive CTE over a lot-flow edge table.</li>
<li>Detect abnormal units against their own history and map an excursion to the exact lots it touched through a custody join.</li>
<li>Make records tamper-evident: signed, hash-chained ledger plus a verifier that re-derives the database and compares it.</li>
<li>Secure the system: bcrypt passwords, JWT sessions, a single Users table with RBAC, parameterised ORM queries, secrets in <code>.env</code>.</li>
<li>Apply professional practice: ORM, tests, backup/restore with native tools, Docker, CI, version control.</li></ul>
<h2>1.4 Scope</h2>
<p><b>In scope:</b> the dairy scenario (30 simulated days, ~1,400 lots, ~410 shipments, ~680,000 readings); the dashboard (control tower, recall simulator,
provenance, ledger, registry, users); a REST API; three roles (ADMIN, MANAGER, AUDITOR).
<b>Out of scope:</b> real IoT ingestion; a multi-node or permissioned blockchain; asymmetric per-party keys (the ledger uses HMAC signatures and one node,
which demonstrates what is signed and verified, not key secrecy); real production data.</p>
<h2>1.5 Team members and responsibilities</h2>""" + table(["Member", "Responsibility"], resp_rows) + """
<h2>1.6 Technology stack</h2>""" + table(["Layer", "Technology"], [
        ["Database", "PostgreSQL 16 (partitioning, materialised view, PL/pgSQL, pgvector HNSW)"],
        ["ORM", "SQLAlchemy 2.0 (psycopg 3 driver); raw SQL only for recursive CTEs, vector search, the ledger and the stored procedure"],
        ["Backend / API", "Python 3.12, FastAPI + Uvicorn (REST), service layer shared with the UI"],
        ["Frontend", "Streamlit (responsive web UI)"],
        ["Authentication", "Email + password, bcrypt hashes, signed JWT (HS256, 60 min), re-validated against the database on every request"],
        ["Authorisation", "RBAC on one <code>users</code> table: ADMIN / MANAGER / AUDITOR"],
        ["Ledger", "SHA-256 hash chain, Merkle roots, proof of work, HMAC signatures"],
        ["Ops", "Docker (Dockerfile, Compose), GitHub Actions CI, pg_dump / pg_restore, python-dotenv"],
        ["Testing", "pytest (72 tests) and verify.py (72 self-checks)"]]) + """
<p class="note">The course reference table suggests React + Express/Spring + Prisma/Hibernate. This project uses a Python stack (Streamlit + FastAPI + SQLAlchemy),
which the brief permits ("any free ORM supported by the backend framework"). Authentication is JWT/session-based (an allowed option); Google/GitHub OAuth was not used.</p>
<h2>1.7 Major deliverables</h2><ul>
<li>ER diagram, relational schema, data dictionary, normalisation analysis (Section 2)</li>
<li>PostgreSQL database with 19 tables, 4 views + 1 materialised view, 1 stored procedure, 6 application triggers, 33+ indexes (Section 3)</li>
<li>Web application with login, role-based menus, dashboard, CRUD, search, filters, pagination, validation (Section 4)</li>
<li>REST API with role-based access and interactive docs (Section 5)</li>
<li>Backup/restore scripts, Dockerfile, Compose file, CI workflow, tests, README (Section 6)</li></ul>
<h2>1.8 Project timeline (Gantt chart)</h2>""" + gantt())

    # ------------------------------------------------------------------ 2
    P.append(f"""<h1>2. Database design</h2></h1>""".replace("</h2></h1>", "</h1>"))
    P.append("""<h2>2.1 Entities and relationships</h2>
<p><b>Primary entities (6+):</b> Party, Product, Unit (sensored asset), Lot, Shipment, User. <b>Supporting entities:</b> Role, Custody,
LotFlow, QualityEvent, Incident, IncidentReport, SensorReading, Block, LedgerTx, TelemetryWindow, AuditLog (plus two scaffolding tables, report_bench and demo_injection, used only for the vector benchmark and the demo).</p>""" + table(
        ["Relationship", "Cardinality", "Implemented as"], [
            ["Party <b>operates</b> Unit", "1 : N", "units.party_id (FK, NOT NULL)"],
            ["Party <b>holds</b> Lot; Product <b>describes</b> Lot", "1 : N each", "lots.party_id, lots.product_id"],
            ["Party is origin / destination / carrier of Shipment", "1 : N (three FKs)", "shipments.origin_party, dest_party, carrier_party"],
            ["Unit <b>carries</b> Shipment", "1 : N", "shipments.unit_id"],
            ["Lot <b>was made from</b> Lot (genealogy)", "M : N, with quantity", "<b>lot_flows</b>(from_lot, to_lot) - associative table, composite PK"],
            ["Lot <b>sat in</b> Unit during an interval", "M : N, with time interval", "<b>custody</b>(lot_id, unit_id, from_time, to_time)"],
            ["Unit <b>emits</b> SensorReading", "1 : N", "sensor_readings.unit_id (day-partitioned)"],
            ["Lot <b>tested by</b> QualityEvent", "1 : N", "quality_events.lot_id"],
            ["Incident <b>described by</b> IncidentReport", "1 : 1", "incident_reports.incident_id UNIQUE"],
            ["Role <b>grants</b> User permissions", "1 : N", "users.role_id (single Users table)"],
            ["Party <b>employs</b> User", "1 : N (optional)", "users.party_id (nullable)"],
            ["Block <b>seals</b> LedgerTx; LedgerTx <b>anchors</b> TelemetryWindow", "1 : N; 1 : 1", "ledger_tx.height; telemetry_windows.tx_hash"]]))
    P.append(img("er_overview.png", "Figure 2.1  ER diagram - overview of all 19 entities and their relationships (crow's-foot notation, generated from the live database catalog)."))
    P.append('<h2>2.2 ER diagram with attributes</h2>' + img("er_core.png", "Figure 2.2  Core business entities with primary keys (PK), foreign keys (FK) and unique keys (UK).", "92%"))
    P.append(img("er_security_ledger.png", "Figure 2.3  Security (roles, users, audit_logs), time-series and ledger entities.", "96%"))
    P.append("""<h2>2.3 Relational schema (derived from the ER diagram)</h2>
<p>Underlined names are primary keys; <i>-&gt;</i> marks a foreign key.</p><pre class="term" style="background:#f4f5fb;color:#1d2433;border:1px solid #c5cbe0">
roles(<u>role_id</u>, role_name UNIQUE, description)
users(<u>user_id</u>, email UNIQUE, full_name, password_hash, role_id -&gt; roles, party_id -&gt; parties NULL, is_active, created_at, updated_at, last_login_at)
parties(<u>party_id</u>, name UNIQUE, party_type, city)
products(<u>product_id</u>, sku UNIQUE, name, category, temp_min, temp_max, shelf_life_days)
units(<u>unit_id</u>, name UNIQUE, unit_type, party_id -&gt; parties, setpoint, temp_min, temp_max)
lots(<u>lot_id</u>, lot_code UNIQUE, product_id -&gt; products, party_id -&gt; parties, lot_type, quantity, created_at, expires_at, status)
shipments(<u>shipment_id</u>, shipment_code UNIQUE, origin_party -&gt; parties, dest_party -&gt; parties, carrier_party -&gt; parties, unit_id -&gt; units, depart_time, arrive_time)
lot_flows(<u>from_lot -&gt; lots, to_lot -&gt; lots</u>, flow_type, quantity, shipment_id -&gt; shipments NULL)
custody(<u>custody_id</u>, lot_id -&gt; lots, unit_id -&gt; units, shipment_id -&gt; shipments NULL, from_time, to_time NULL)
sensor_readings(time, unit_id -&gt; units, temp_c, humidity)            -- partitioned by day, no surrogate key by design
quality_events(<u>event_id</u>, lot_id -&gt; lots, test_name, result, measured, limit_value, tested_at, note)
incidents(<u>incident_id</u>, kind, unit_id -&gt; units NULL, lot_id -&gt; lots NULL, occurred_at)
incident_reports(<u>report_id</u>, incident_id -&gt; incidents UNIQUE, summary, action_taken, outcome, embedding vector(384))
audit_logs(<u>log_id</u>, entity, action, row_pk, log_time, details)
blocks(<u>height</u>, prev_hash, merkle_root, nonce, difficulty, ts_ms, block_hash UNIQUE, tx_count)
ledger_tx(<u>tx_id</u>, height -&gt; blocks, tx_index, tx_type, subject, ts_ms, payload, payload_hash, tx_hash UNIQUE, signatures)
telemetry_windows(<u>window_id</u>, unit_id -&gt; units, window_start, window_end, shipment_id -&gt; shipments NULL, tx_hash -&gt; ledger_tx)
report_bench(<u>id</u>, embedding)      demo_injection(<u>unit_id -&gt; units</u>, from_time, to_time, delta)
</pre>
<p class="note"><b>Single Users table.</b> Every person who signs in is a row in <code>users</code>; permissions come from the <code>role_id</code> column.
There are no Admin / Manager / Auditor tables. Parties (companies) are a separate business entity, not accounts.</p>""")
    P.append("<h2>2.4 Data dictionary</h2><p>Generated from the PostgreSQL catalog (information_schema / pg_constraint), so it matches the running database exactly.</p>" + data_dictionary())
    P.append("""<h2>2.5 Keys and constraints (summary)</h2>""" + table(["Kind", "Where used"], [
        ["PRIMARY KEY", "every table except the partitioned time series (which has the composite access path <code>(unit_id, time DESC)</code>); composite PKs on lot_flows (from_lot, to_lot) and demo_injection"],
        ["FOREIGN KEY", "all references in the schema above; <code>ON DELETE CASCADE</code> where the child has no meaning alone (flows, custody, readings, reports, quality events), <code>SET NULL</code> for users.party_id"],
        ["NOT NULL", "every business-critical column (codes, quantities, timestamps, role_id, password_hash)"],
        ["UNIQUE", "lot_code, sku, party name, unit name, shipment_code, users.email, roles.role_name, block_hash, tx_hash, incident_reports.incident_id"],
        ["CHECK", "quantity &gt; 0; lot_type / status / party_type / unit_type / flow_type / result / kind enumerations; temp_min &lt; temp_max; arrive_time &gt; depart_time; to_time &gt;= from_time; from_lot &lt;&gt; to_lot; users e-mail format; non-blank names"],
        ["DEFAULT", "lots.status = 'OK'; users.is_active = TRUE; created_at / updated_at / log_time = now()"]]))
    P.append("""<h2>2.6 Normalisation (to 3NF)</h2>
<p>For each table every non-key attribute depends on the key, the whole key, and nothing but the key.</p>""" + table(
        ["Table", "Candidate key", "Functional dependencies (non-trivial)", "Form"], [
            ["parties, products, units, roles", "surrogate id (also natural: name / sku)", "id -&gt; all attributes; name -&gt; id (and sku -&gt; id)", "3NF (BCNF)"],
            ["users", "user_id, email", "user_id -&gt; email, name, hash, role_id, party_id, flags. Role <i>description</i> lives in roles, not users (no role_id -&gt; description inside users)", "3NF (BCNF)"],
            ["lots", "lot_id, lot_code", "lot_id -&gt; product_id, party_id, type, quantity, dates, status. Product <i>name/temperature</i> are in products, holder <i>name/city</i> in parties", "3NF (BCNF)"],
            ["lot_flows", "(from_lot, to_lot)", "(from_lot, to_lot) -&gt; flow_type, quantity, shipment_id. Attribute depends on the <i>whole</i> pair (quantity of A that went into B)", "3NF (BCNF) - M:N resolved"],
            ["custody", "custody_id", "custody_id -&gt; lot, unit, shipment, interval. Unit limits are in units, not repeated here", "3NF (BCNF)"],
            ["shipments", "shipment_id, shipment_code", "shipment_id -&gt; three party FKs, unit, times. Party and vehicle names are fetched by join (see v_shipment_overview)", "3NF (BCNF)"],
            ["quality_events, incidents, incident_reports", "event_id / incident_id / report_id", "each id -&gt; its attributes; incident_id UNIQUE gives 1:1 with its report", "3NF (BCNF)"],
            ["blocks, ledger_tx, telemetry_windows", "height / tx_id / window_id", "ledger_tx.payload deliberately duplicates business data: it is a <b>signed copy</b> to compare against, not a second source of truth (documented exception)", "3NF, one documented denormalisation"],
            ["audit_logs", "log_id", "log_id -&gt; entity, action, row, time, details", "3NF (BCNF)"]]) + """
<p><b>Derived values are not stored.</b> The exposure share of a flow is the view <code>lot_flow_share</code> (quantity / SUM(quantity) OVER (PARTITION BY to_lot)),
so it can never drift from <code>lot_flows</code>. Aggregates are in views, not columns.</p>
<h2>2.7 Indexes and the query each serves</h2>""" + table(["Index", "Serves"], [
        ["<code>idx_readings_unit_time (unit_id, time DESC)</code> + day partitions", "every sensor query: one unit over a window; partition pruning (44 of 47 partitions removed in the plan below)"],
        ["<code>readings_hourly</code> (materialised view) + unique (bucket, unit_id)", "per-unit anomaly baseline; allows REFRESH CONCURRENTLY"],
        ["<code>idx_custody_unit_time (unit_id, from_time, to_time)</code>, <code>idx_custody_lot</code>", "exposure join: lots inside a unit during an excursion"],
        ["lot_flows PK (from_lot, to_lot), <code>idx_flows_to (to_lot)</code>", "forward and backward traversal of the recursive CTE"],
        ["<code>idx_lots_type_created (lot_type, created_at DESC)</code>, <code>idx_lots_party</code>", "paginated lot lists, filters by holder"],
        ["<code>idx_reports_hnsw</code> (HNSW, cosine)", "similar-incident vector search"],
        ["<code>idx_tx_subject</code>, <code>idx_tx_type</code>, <code>idx_windows_unit</code>", "ledger lookups per lot / type / unit"],
        ["<code>idx_users_role</code>, <code>idx_users_party</code>, <code>users_email_key</code>", "role filtering and login by e-mail"]]))

    # ------------------------------------------------------------------ 3
    P.append("""<h1>3. Database implementation</h1>
<h2>3.1 Database creation</h2>
<p>The schema is applied by <code>python run.py schema</code> from four ordered SQL files; <code>python run.py seed</code> simulates and loads the data;
<code>python run.py optimise</code> builds the roll-up, HNSW index and the un-optimised baseline copy.</p>""" + table(["File", "Content"], [
        ["sql/01_core.sql", "parties, products, units, lots, lot_flows (+ view lot_flow_share), shipments, custody, quality_events, incidents, incident_reports, audit_logs, trigger trg_lot_audit"],
        ["sql/02_timeseries.sql", "sensor_readings partitioned by day + (unit_id, time DESC) index"],
        ["sql/03_optimise.sql", "readings_hourly materialised view, HNSW index, baseline copy for the benchmark"],
        ["sql/04_ledger.sql", "blocks, ledger_tx, telemetry_windows, append-only triggers"],
        ["sql/05_security.sql", "roles, users, views, stored procedure sp_change_user_role, three users triggers"],
        ["sql/demo_queries.sql", "the joins, aggregates, transaction, procedure, trigger and constraint demonstrations shown below"]]) +
             term("tables.txt", "docker exec chaintwin-db psql -U postgres -c \"\\dt\"  (partitions of sensor_readings hidden)") +
             term("rowcounts.txt", "row counts per table") +
             term("d_users.txt", "\\d users  - columns, CHECK constraints, indexes, FK, triggers"))
    P.append("<h2>3.2 CRUD operations through the ORM</h2><p>All reads and writes go through SQLAlchemy models (<code>models.py</code>) and the service layer (<code>services.py</code>). "
             "Raw SQL is used only where the brief allows: recursive CTEs, vector search, the ledger and the stored procedure call. The run below shows each ORM call, the SQL it emitted, and the result (rolled back afterwards).</p>"
             + term("orm_demo.txt", "python scripts/orm_demo.py", (0, 40)))
    P.append(term("orm_demo.txt", "python scripts/orm_demo.py (continued)", (40, 80)))
    P.append("<h3>CRUD in the web UI</h3>" + img("13_create_raw_lot.png", "Create: a RAW lot is registered through the ORM and anchored on the ledger in the same transaction.", "80%")
             + '<div class="pair">' + img("15_product_created.png", "Create: product added (success message).") + img("17_product_deleted.png", "Delete: product removed.") + "</div>")
    P.append("<h2>3.3 Joins</h2><p>Three-table join; a view that joins five tables; recursive CTE over the genealogy graph.</p>" + term("", "sql/demo_queries.sql  sections 1, 2 and 6", text="\n\n".join(sql_section(i) for i in (1, 2, 6))))
    P.append("<h2>3.4 Aggregate queries</h2>" + term("", "sections 3 and 4  (GROUP BY, HAVING, FILTER)", text="\n\n".join(sql_section(i) for i in (3, 4)))
             + "<p>The ORM equivalents (<code>func.count</code>, <code>func.sum</code>, <code>group_by</code>, conditional <code>case</code>) feed the Analytics page:</p>" + img("19_analytics.png", "Analytics tab: aggregates built with the ORM.", "88%"))
    P.append("<h2>3.5 Views</h2><ul><li><code>lot_flow_share</code> - exposure share of every genealogy edge (window function)</li><li><code>v_lot_status_summary</code>, <code>v_party_inventory</code>, <code>v_shipment_overview</code> - reporting views</li><li><code>readings_hourly</code> - materialised hourly roll-up of 680k readings</li></ul>"
             + term("", "section 5", text=sql_section(5)) + term("views.txt", "catalog: views and materialised views"))
    P.append("<h2>3.6 Transactions</h2><p>Section 7 below changes a lot's status inside <code>BEGIN ... ROLLBACK</code>, shows the audit trigger firing, then proves the change is gone. "
             "In the application, <code>services.set_lot_status</code> updates the lots and appends the signed STATUS_CHANGE block to the ledger in <b>one</b> transaction, so the database and the ledger can never disagree; "
             "<code>create_raw_lot</code> does the same for intake. Constraint failures are isolated with SAVEPOINTs so a rejected row does not poison the session.</p>" + term("", "section 7", text=sql_section(7)))
    P.append("<h2>3.7 Stored procedure</h2><p><code>sp_change_user_role(actor, target, role)</code> (PL/pgSQL <code>CREATE PROCEDURE</code>) re-checks inside the database that the actor is an active ADMIN, validates the role, and updates the user; "
             "the audit trigger records it. Even if the application is bypassed or lies about the caller's role, the database refuses.</p>" + term("", "section 8  (CALL succeeds for ADMIN, is refused for MANAGER, rejects an unknown role)", text=sql_section(8)) + term("triggers.txt", "procedure and triggers in the catalog"))
    P.append("<h2>3.8 Triggers</h2>" + table(["Trigger", "Table", "Does"], [
        ["trg_lot_audit", "lots", "AFTER INSERT/UPDATE/DELETE: writes lot status changes to audit_logs"],
        ["trg_users_audit", "users", "AFTER: records new users and role / active-flag changes"],
        ["trg_users_touch", "users", "BEFORE UPDATE: maintains updated_at"],
        ["trg_users_keep_admin", "users", "BEFORE UPDATE/DELETE: the last active ADMIN cannot be demoted, disabled or deleted"],
        ["trg_blocks_immutable, trg_tx_immutable", "blocks, ledger_tx", "BEFORE UPDATE/DELETE: the ledger is append-only"]]) + term("", "section 9", text=sql_section(9))
             + img("23_last_admin_protected.png", "The trigger's refusal surfaced in the UI when an ADMIN tries to demote the only administrator.", "70%"))
    P.append("<h2>3.9 Constraints rejecting bad data</h2>" + term("", "section 10", text=sql_section(10)))
    P.append("<h2>3.10 Indexes and query plans</h2>" + term("", "sections 11 and 12  (EXPLAIN ANALYZE, index inventory)", text="\n\n".join(sql_section(i) for i in (11, 12)))
             + "<p>The sensor query prunes 44 of 47 day-partitions and uses the <code>(unit_id, time)</code> index. Measured speed-ups from <code>python run.py benchmark</code> (681k readings, baseline = one unindexed heap):</p>"
             + table(["Query", "Baseline", "Optimised", "Speed-up"], [["Avg temp, 1 unit, last 24 h", "499 ms", "4.7 ms", "105x"], ["Daily averages, all units, 30 d", "537 ms", "13.9 ms", "39x"], ["Readings above 8 C, 1-hour window", "478 ms", "7.2 ms", "67x"]])
             + img("24_benchmark.png", "Benchmark tab in the application.", "80%"))
    P.append("<h2>3.11 ORM integration summary</h2>" + table(["Concern", "How"], [
        ["Models", "models.py: 12 mapped classes incl. User and Role with relationships (lazy='joined' where the UI needs the related row)"],
        ["Sessions / transactions", "db.py: <code>session_scope()</code> commits on success, rolls back on any error"],
        ["Queries", "services.py: search (ilike), filters, pagination (LIMIT/OFFSET with a count query), joins, group-by aggregates"],
        ["Authorisation", "auth.py + services.py: every write names a permission and checks it first (ORM-based user/role lookup)"],
        ["Raw SQL (allowed exceptions)", "recursive CTEs and vector search (queries.py), ledger hashing/appending (chain.py), <code>CALL sp_change_user_role</code>"]]))

    # ------------------------------------------------------------------ 4
    P.append("<h1>4. Application development</h1><p>A Streamlit web application (framework-based) with a FastAPI back end sharing one service layer. Features required by the brief and where they appear:</p>"
             + table(["Feature", "Evidence"], [["Responsive UI", "Fig 4.1 (desktop) and Fig 4.2 (390 px phone)"], ["Login page", "Fig 4.3, Section 5"], ["Dashboard", "Fig 4.4-4.7"],
                                              ["CRUD", "Fig 3.x and 4.8-4.10"], ["Search / filtering / pagination", "Fig 4.8-4.9"], ["Form validation", "Fig 4.11-4.12"], ["Exception handling", "Fig 4.12-4.13"]])
             + '<div class="pair">' + img("04_dashboard_admin.png", "Fig 4.1  Dashboard on desktop: KPIs, ledger status, role-based tabs, anomaly table from the time-series layer.", "100%")
             + '<div style="width:34%">' + img("32_mobile_dashboard.png", "Fig 4.2  Same page at 390 px: layout stacks.", "100%") + "</div></div>")
    P.append(img("01_login.png", "Fig 4.3  Login page.", "80%") + img("06_control_tower_full.png", "Fig 4.4  Control tower: abnormal units, 72-hour excursion log, investigation chart, exposed lots, similar past incidents (vector search), downstream trace, hold/release actions.", "100%"))
    P.append(img("07_recall_simulator.png", "Fig 4.5  Recall simulator: genealogy recall scope vs. recalling everything since the lot arrived.", "100%"))
    P.append('<div class="pair">' + img("08_provenance.png", "Fig 4.6  Provenance: farm-to-shelf history with temperatures and ledger records.") + "</div>" + img("09_ledger_verify_tamper.png", "Fig 4.7  Ledger: verification and a live tampering attempt that is caught and rolled back.", "100%"))
    P.append(img("10_registry_lots.png", "Fig 4.8  Registry - Lots: listing with pagination, create form and status form.", "92%")
             + '<div class="pair">' + img("11_registry_search_filter.png", "Fig 4.9a  Search 'RM-' and type filter RAW (90 matches).") + img("12_registry_pagination.png", "Fig 4.9b  Pagination: page 2 of 5.") + "</div>"
             + img("18_parties.png", "Fig 4.10  Parties: search, type filter, create, city update, delete.", "80%"))
    P.append("<h3>Form validation and exception handling</h3><p>Validation exists at three layers: browser-side inputs (types, ranges), the service layer (clear messages), and the database (CHECK / UNIQUE / FK). "
             "Database errors are translated to messages a user can act on; unexpected errors are logged and shown without internals.</p>"
             + '<div class="pair">' + img("14_validation_error.png", "Fig 4.11  Invalid SKU rejected by validation.") + img("16_duplicate_error.png", "Fig 4.12  Duplicate SKU: UNIQUE violation translated to 'Already exists'.") + "</div>"
             + '<div class="pair">' + img("02_login_failed.png", "Fig 4.13a  Wrong password: generic message (no user enumeration).") + img("03_login_validation.png", "Fig 4.13b  Empty fields.") + "</div>")

    # ------------------------------------------------------------------ 5
    P.append("""<h1>5. Authentication, authorisation and database security</h1>
<h2>5.1 Authentication (JWT + session)</h2><ul>
<li><b>Login:</b> email + password; the user is looked up through the ORM, the password is verified with bcrypt, and a signed JWT (HS256, 60 min, claims sub / email / role / iat / exp) is issued. Unknown user and wrong password return the same message and take similar time (dummy hash).</li>
<li><b>Secure session/token handling:</b> the token is kept only in the server-side Streamlit session (UI) or the Authorization header (API); on every request it is verified and the user row is re-read, so a disabled account or changed role applies immediately. Tampered, expired, wrong-key and <code>alg=none</code> tokens are rejected (unit-tested). After 5 failed attempts a session is locked.</li>
<li><b>Logout:</b> the Sign-out button discards the token; <code>POST /auth/logout</code> for API clients.</li>
<li><b>Retrieval of authenticated user information:</b> sidebar <i>Session</i> panel and <code>GET /auth/me</code>.</li></ul>""" + '<div class="pair">' + img("05_session_info.png", "Fig 5.1  Signed in as ADMIN: user info decoded from the session token (sidebar).") + "</div>" + img("29_api_docs.png", "Fig 5.2  REST API interactive documentation (FastAPI /docs): auth, lots, registry, users, analytics, ledger.", "92%"))
    P.append(term("api_demo.txt", "API calls: login, /auth/me, role-based access, validation, tampered token", (0, 62)))
    P.append("<h2>5.2 Role-based authorisation (RBAC) on a single Users table</h2>" + table(["Permission", "ADMIN", "MANAGER", "AUDITOR"], [
        ["view dashboards, provenance, ledger verification, registry (read)", "yes", "yes", "yes"], ["create / edit / delete products and parties; register raw lots", "yes", "yes", "no"],
        ["place holds / release lots (signed on the ledger)", "yes", "yes", "no"], ["Users page, create users, change roles (stored procedure)", "yes", "no", "no"], ["run tamper attacks in the Ledger tab", "yes", "no", "no"]])
             + "<p>Three layers enforce it: <b>menu</b> (tabs come from <code>auth.visible_pages</code>), <b>page / form</b> (write forms only for roles that hold the permission) and <b>API / service</b> (every write calls <code>principal.require(permission)</code>; REST routes use <code>Depends(needs(permission))</code>; the role-change procedure re-checks in the database).</p>"
             + '<div class="pair">' + img("25_dashboard_manager.png", "Fig 5.3  MANAGER menu: no Users tab.") + img("26_dashboard_auditor.png", "Fig 5.4  AUDITOR menu: no Users tab.") + "</div>"
             + img("21_users_admin.png", "Fig 5.5  ADMIN-only Users page: one table of people, role column, search, pagination.", "80%")
             + '<div class="pair">' + img("27_registry_auditor_readonly.png", "Fig 5.6  AUDITOR: Registry is read-only, write forms replaced by notices.") + img("28_ledger_auditor_no_attacks.png", "Fig 5.7  AUDITOR: attack button disabled.") + "</div>")
    P.append("<h2>5.3 Database security</h2>" + table(["Control", "Implementation / evidence"], [
        ["Password hashing", "bcrypt (cost 12) with per-password salt; password rules (8+ chars, upper, lower, digit); only <code>password_hash</code> is stored (below)"],
        ["SQL-injection prevention", "all ORM queries use bound parameters; the injection attempt in Section 3.2 is treated as a literal string. Raw SQL uses named / positional parameters, never string formatting of user input"],
        ["Secrets management", "<code>DATABASE_URL</code>, <code>JWT_SECRET</code>, <code>POSTGRES_PASSWORD</code>, <code>SEED_PASSWORD</code> are read from <code>.env</code> via python-dotenv (<code>settings.py</code>); <code>.env</code> is in <code>.gitignore</code>; only <code>.env.example</code> with placeholders is committed; docker-compose uses <code>${VAR}</code> substitution; CI uses GitHub secrets"],
        ["Least privilege & integrity", "role CHECK constraint; last-admin trigger; append-only ledger triggers; audit_logs written by triggers, not by the app"],
        ["Input validation", "Pydantic models on the API (422 on bad input), service-layer checks, database constraints"]])
             + term("password_hashes.txt", "SELECT ... left(password_hash, 29) FROM users  -  bcrypt hashes, never plaintext")
             + img("22_weak_password.png", "Fig 5.8  Weak password rejected when creating a user.", "80%"))

    # ------------------------------------------------------------------ 6
    P.append("""<h1>6. Professional development practices</h1>
<h2>6.1 GitHub: commits, branches, .gitignore</h2>
<p>Repository: """ + esc(TEAM["github_repo_url"]) + """ (must be set to <b>Public</b> for review; collaborators = all team members).</p>
<p class="note"><b>Why the history is short.</b> InfraTwin began as a <b>hackathon project</b>, built and pushed in one sitting, so the original build exists on GitHub as a single commit of the whole project
(<code>91d0acc</code>, on <code>main</code>). The work added afterwards for this course submission (ORM layer, authentication and RBAC, REST API, stored procedure and triggers, backup/restore, Docker, CI, tests, report)
was committed in separate, themed commits on the <code>dev</code> branch, listed below. Contributions are verifiable from the commit author on GitHub.</p>
<h3>Individual commits (generated from <code>git log</code> at build time)</h3>""" + commit_table() + term("branches.txt", "git branch -a") +
             """<p class="note">Rebuild this report after committing and pushing (<code>python docs/report/build_report.py</code>): the table is read from the repository, so it always matches the GitHub history.
Each member's commits are attributed by the GitHub username in <code>team.json</code>.</p>
<h3>.gitignore and secrets</h3><p><code>.gitignore</code> excludes <code>.env</code>, <code>.env.*</code> (except <code>.env.example</code>), <code>*.pem</code>, <code>*.key</code>, <code>backups/</code>, <code>*.dump</code>, virtual environments and the ledger checkpoint.
No credential appears in source: every module reads configuration through <code>settings.py</code>.</p>
<h2>6.2 Database backup and restore (native tools)</h2><p><code>python run.py backup</code> runs <code>pg_dump -Fc</code> inside the database container; <code>python run.py restore</code> runs <code>pg_restore</code> into a <b>separate</b> database (<code>chaintwin_restored</code>) and compares row counts with the live database, so a restore drill never overwrites live data.</p>"""
             + term("backup.txt", "python run.py backup") + term("restore.txt", "python run.py restore  -  every table matches"))
    P.append("<h2>6.3 Docker</h2><p><code>Dockerfile</code> (python:3.12-slim, non-root user, HEALTHCHECK, secrets injected at run time) and <code>docker-compose.yml</code> (db + optional app + api profile). The image was built and run locally; the container connected to the database and served the login page and dashboard.</p>"
             + term("docker_build.txt", "docker build -t chaintwin:latest .", (-14, None)) + term("docker_ps.txt", "docker ps / docker images")
             + img("30_docker_container_app.png", "Fig 6.1  The dashboard served from the Docker container (host port 8502).", "80%")
             + f'<p class="note"><b>Docker Hub:</b> {esc(TEAM["docker_hub_url"])}. To publish: <code>docker tag chaintwin:latest &lt;user&gt;/chaintwin:latest</code> then <code>docker push &lt;user&gt;/chaintwin:latest</code>.</p>')
    P.append("<h2>6.4 Cloud deployment</h2><p class=\"note\">Live URL: " + esc(TEAM["live_url"]) + ". This is the hackathon-era dashboard, deployed from <code>main</code> on Streamlit Community Cloud. The login / RBAC build on <code>dev</code> goes live when <code>dev</code> is merged into <code>main</code> and the cloud database has the new schema (<code>python run.py security</code>, then <code>users</code>) and the app secrets <code>DATABASE_URL</code> and <code>JWT_SECRET</code>. The container image and compose file are ready for Render / Railway / Koyeb with a managed PostgreSQL that supports the <code>vector</code> extension (e.g. Supabase, Neon, Railway); set <code>DATABASE_URL</code> and <code>JWT_SECRET</code> as platform variables, then run <code>python run.py schema</code>, <code>seed</code>, <code>users</code> once.</p>"
             "<h2>6.5 CI/CD</h2><p><code>.github/workflows/ci.yml</code>: on push/PR it starts a pgvector PostgreSQL service, builds the schema, seeds a small world (DAYS=8), creates users, runs pytest and builds the Docker image. Secrets come from GitHub Actions secrets. (Written and committed with the project; its first run happens on GitHub after the push.)</p>")

    # ------------------------------------------------------------------ 7
    P.append("<h1>7. Execution evidence: inputs, processing, outputs</h1>"
             "<h2>7.1 Run order</h2>" + term("", "commands", text="python run.py up        # start PostgreSQL container\npython run.py schema    # create 19 tables, views, triggers, procedure\npython run.py seed      # simulate 30 days: 1,387 lots, 681k readings, ledger blocks\npython run.py users     # admin / manager / auditor accounts (single users table)\npython run.py optimise  # roll-up, HNSW index, baseline copy\npython run.py verify    # 72 self-checks incl. six live tampering attempts\npython run.py app       # dashboard :8501        python run.py api   # REST API :8010")
             + "<h2>7.2 Automated checks</h2>" + term("verify.txt", "python run.py verify", (0, 80)) + term("pytest.txt", "python -m pytest tests/ -v   (72 passed)", (-48, None)))
    P.append("<h2>7.3 Worked scenario: an excursion becomes a hold, recorded on the ledger</h2><ol>"
             "<li><b>Input:</b> three units drift (cold store +4.5 C, pasteuriser line B -3 C, display case +6 C) - Fig 4.1 anomaly table.</li>"
             "<li><b>Processing:</b> each unit's last hour is compared with its own hourly baseline (z-score); the custody join maps the excursion to lots; the recursive CTE follows the lots downstream (Fig 4.4).</li>"
             "<li><b>Output:</b> exposed and downstream lots with exposure share; the MANAGER places them on HOLD; the ORM update and a signed STATUS_CHANGE block commit together.</li>"
             "<li><b>Trust check:</b> the Ledger tab verifies the chain, then a tampering attempt (shrink a quantity, un-recall a lot, rewrite history and re-mine) is detected (Fig 4.7) and rolled back.</li></ol>"
             + img("09_ledger_verify_tamper.png", "Ledger verification and a caught attack.", "92%"))

    # ------------------------------------------------------------------ 8
    P.append("""<h1>8. Improvements</h1>
<p>No review has yet happened on this version of the report. After the first review, list each modification here with its screenshot, commit date and commit ID, for example:</p>""" + table(
        ["Improvement", "Screenshot", "Commit date", "Commit ID"], [["(none yet)", "-", "-", "-"]]))

    # ------------------------------------------------------------------ 9
    S = lambda c, t: f'<span class="{c}">{t}</span>'
    P.append("<h1>9. Requirement coverage and open items</h1><p>An honest checklist against the brief. Items marked <span class=\"todo\">ACTION</span> need something only the team can do (accounts, names, pushes).</p>" + table(
        ["Requirement", "Status", "Note"], [
            ["ER diagram, relational schema, data dictionary", S("ok", "Done"), "Section 2"],
            ["&gt;= 4 entities, &gt;= 3 relationships, &gt;= 8 tables, PK/FK/constraints, 3NF, indexes", S("ok", "Done"), "6 primary entities, 12 relationships, 19 tables"],
            ["Joins, aggregates, views, transactions, procedure, trigger, indexes", S("ok", "Done"), "Section 3"],
            ["ORM for database interaction", S("ok", "Done"), "SQLAlchemy; raw SQL only where permitted"],
            ["Responsive UI, login, dashboard, CRUD, search, filter, pagination, validation, exceptions", S("ok", "Done"), "Section 4"],
            ["Authentication (JWT/session), login, logout, token handling, current user", S("ok", "Done"), "OAuth (Google/GitHub) not implemented"],
            ["RBAC: &gt;= 2 roles, menu/page/API access, single Users table", S("ok", "Done"), "3 roles"],
            ["bcrypt, SQL-injection prevention, secrets in .env", S("ok", "Done"), "Section 5.3"],
            ["Backup / restore with native tools", S("ok", "Done"), "pg_dump / pg_restore"],
            ["Docker image", S("ok", "Done"), "Built and run locally"],
            ["Docker Hub", S("todo", "ACTION"), "Needs your Docker Hub login: tag and push"],
            ["Cloud deployment, live URL", S("part", "Live (earlier build)"), "Streamlit Community Cloud; redeploy after merging dev, see 6.4"],
            ["GitHub: public repo, collaborators, branches main + dev, &gt;= 10 meaningful commits, PRs", S("part", "Mostly done"), "main + dev, themed commits on dev (Section 6.1). Still to do on github.com: set the repo Public, add Tanmay as collaborator, open a PR dev into main"],
            ["Team names / reg. numbers / per-member commits", S("part", "Names done"), "Tanmay's commits must come from his own GitHub account; add his username to team.json and rebuild"],
            ["CI/CD (bonus)", S("part", "Written, not yet run"), "Runs when pushed to GitHub; add the three CI secrets"],
            ["Custom domain, Grafana/Prometheus (bonus)", S("todo", "Not done"), "-"],
            ["Other best practice (bonus): automated tests, audit logging, performance optimisation", S("ok", "Done"), "72 tests, trigger audit log, 39-105x speed-ups"]]))
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(P)}</body></html>"


def main() -> int:
    out_html = HERE / "ChainTwin_Report.html"
    out_html.write_text(build_html(), encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page()
        pg.goto(out_html.as_uri())
        pg.wait_for_timeout(1500)
        pdf = HERE / "ChainTwin_Report.pdf"
        pg.pdf(path=str(pdf), format="A4", print_background=True, prefer_css_page_size=True,
               display_header_footer=True, margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
               header_template="<span></span>",
               footer_template="<div style='font-size:8px;width:100%;text-align:center;color:#666'>ChainTwin - Database Systems BCSE307L &nbsp;|&nbsp; page <span class='pageNumber'></span> of <span class='totalPages'></span></div>")
        b.close()
    print("wrote", pdf, f"({pdf.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
