/**
 * Build the InfraTwin judging deck.
 *   node scripts/make_deck.js
 * Output: InfraTwin_DBTHON_2026.pptx
 */
const pptxgen = require("pptxgenjs");
const path = require("path");
const fs = require("fs");
const ROOT = path.resolve(__dirname, "..");

// ---------------------------------------------------------------- benchmark
// Numbers come from benchmark_results.csv so the deck always shows what was
// actually measured on the machine that will present it. The fallback is the
// reference run, used only if the benchmark has not been run yet.
const SHORT = {
  "Avg temp, 1 machine, last 24h": "Avg temp,\n1 machine, 24 h",
  "Daily averages, all machines, 30 days": "Daily averages,\nall machines, 30 d",
  "Readings above threshold, 1-hour window": "Threshold scan,\n1-hour window",
};
function loadBenchmark() {
  const csv = path.join(ROOT, "benchmark_results.csv");
  const fallback = [
    ["Avg temp, 1 machine, last 24 h", 236.3, 3.23, 73.2],
    ["Daily averages, all machines, 30 d", 1253.2, 44.92, 27.9],
    ["Threshold scan, 1-hour window", 252.9, 57.01, 4.4],
    ["Similar-report search, 20k vectors", 15.5, 2.13, 7.3],
  ];
  if (!fs.existsSync(csv)) {
    console.warn("benchmark_results.csv not found - using the reference run");
    return fallback;
  }
  const lines = fs.readFileSync(csv, "utf8").trim().split(/\r?\n/).slice(1);
  const rows = lines.map((l) => {
    const m = l.match(/^"?(.*?)"?,([\d.]+),([\d.]+),([\d.]+)$/);
    if (!m) return null;
    let name = m[1];
    if (/Similar-report/.test(name)) name = "Similar-report search, 20k vectors";
    return [name, parseFloat(m[2]), parseFloat(m[3]), parseFloat(m[4])];
  }).filter(Boolean);
  return rows.length === 4 ? rows : fallback;
}
const BENCH = loadBenchmark();
const TOP = Math.max(...BENCH.map((r) => r[3]));
const TOPX = (Number.isInteger(TOP) ? TOP : Math.round(TOP)) + "\u00d7";
function chartLabel(name) {
  if (SHORT[name]) return SHORT[name];
  if (/Avg temp/.test(name)) return "Avg temp,\n1 machine, 24 h";
  if (/Daily averages/.test(name)) return "Daily averages,\nall machines, 30 d";
  if (/Threshold/.test(name)) return "Threshold scan,\n1-hour window";
  return "Similar-report search,\n20k vectors";
}

// Industrial heat + data: graphite dominant, amber as the sharp accent (the
// signal being detected is temperature), one colour per architectural layer.
const THEME = {
  name: "InfraTwin",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "11151C", lt1: "FFFFFF",
    dk2: "1E2630", lt2: "F2F4F6",
    accent1: "E07A1F",  // amber  - anomaly / heat
    accent2: "0E7490",  // teal   - time-series
    accent3: "9D174D",  // magenta- vector
    accent4: "15803D",  // green  - audit / pass
    accent5: "4338CA",  // indigo - relational core
    accent6: "67718A",  // muted
    hlink: "0E7490", folHlink: "9D174D",
  },
};

const AMBER = THEME.colors.accent1;
const TEAL = THEME.colors.accent2;
const MAG = THEME.colors.accent3;
const GREEN = THEME.colors.accent4;
const INDIGO = THEME.colors.accent5;
const MUTED = THEME.colors.accent6;
const INK = THEME.colors.dk1;
const SLATE = THEME.colors.dk2;
const SURF = THEME.colors.lt2;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";            // 13.3 x 7.5
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "InfraTwin team";
pres.company = "DBTHON 2026";
pres.title = "InfraTwin - predictive maintenance on one PostgreSQL database";
const C = pres.SchemeColor;

const M = 0.55;                          // page margin
const W = 13.3;

/* ----------------------------------------------------------------- layouts */
pres.defineSlideMaster({
  title: "DARK",
  background: { color: SLATE },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 1.0, y: 2.25, w: 11.3, h: 1.5,
        fontSize: 48, bold: true, color: C.background1, fontFace: THEME.headFontFace,
        align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "body", type: "body", x: 1.0, y: 3.85, w: 10.4, h: 1.5,
        fontSize: 17, color: "C8D0DA", align: "left", margin: 0 }, text: "" } },
  ],
});

pres.defineSlideMaster({
  title: "SECTION",
  background: { color: SLATE },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 1.0, y: 2.9, w: 11.3, h: 1.1,
        fontSize: 40, bold: true, color: C.background1, fontFace: THEME.headFontFace,
        align: "left", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "body", type: "body", x: 1.0, y: 4.05, w: 10.4, h: 0.9,
        fontSize: 16, color: "C8D0DA", align: "left", margin: 0 }, text: "" } },
  ],
  slideNumber: { x: 12.5, y: 7.0, fontSize: 10, color: "63708A" },
});

pres.defineSlideMaster({
  title: "CONTENT",
  background: { color: C.background1 },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: M, y: 0.34, w: 12.2, h: 0.82,
        fontSize: 30, bold: true, color: C.text1, fontFace: THEME.headFontFace,
        align: "left", valign: "middle", margin: 0 }, text: "" } },
    { placeholder: { options: { name: "kicker", type: "body", x: M, y: 1.18, w: 12.2, h: 0.34,
        fontSize: 13.5, color: MUTED, align: "left", margin: 0 }, text: "" } },
  ],
  slideNumber: { x: 12.55, y: 7.0, fontSize: 10, color: MUTED },
});

/* ----------------------------------------------------------------- helpers */
const add = (name, section) => pres.addSlide({ masterName: name, sectionTitle: section });

function head(s, title, kicker) {
  s.addText(title, { placeholder: "title" });
  if (kicker) s.addText(kicker, { placeholder: "kicker" });
}

function card(s, o) {
  s.addShape(pres.ShapeType.roundRect, {
    x: o.x, y: o.y, w: o.w, h: o.h, rectRadius: 0.1,
    fill: { color: o.fill || SURF },
    line: o.line ? { color: o.line, width: 1 } : { color: o.fill || SURF, width: 0 },
    shadow: o.shadow === false ? undefined
      : { type: "outer", color: "9AA3B2", blur: 7, offset: 1.4, angle: 90, opacity: 0.16 },
    objectName: o.name || "card",
  });
}

// The repeated motif: a filled circle carrying a number or short glyph.
function chip(s, x, y, label, color, d) {
  const dia = d || 0.46;
  s.addShape(pres.ShapeType.ellipse, {
    x, y, w: dia, h: dia, fill: { color },
    line: { color, width: 0 }, objectName: "chip-" + label,
  });
  s.addText(String(label), {
    x, y, w: dia, h: dia, align: "center", valign: "middle",
    fontSize: dia > 0.5 ? 15 : 13, bold: true, color: "FFFFFF",
    margin: 0, isTextBox: true,
  });
}

function code(s, o) {
  card(s, { x: o.x, y: o.y, w: o.w, h: o.h, fill: "11151C", shadow: false, name: "code" });
  s.addText(o.text, {
    x: o.x + 0.16, y: o.y + 0.11, w: o.w - 0.32, h: o.h - 0.22,
    fontSize: o.size || 11, fontFace: "Courier New", color: "D7DEE8",
    align: "left", valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.08,
  });
}

function stat(s, o) {
  s.addText(o.value, {
    x: o.x, y: o.y, w: o.w, h: 0.78, fontSize: o.size || 44, bold: true,
    color: o.color || AMBER, align: "left", valign: "bottom", margin: 0,
    isTextBox: true, fontFace: THEME.headFontFace,
  });
  s.addText(o.label, {
    x: o.x, y: o.y + 0.78, w: o.w, h: 0.44, fontSize: 11.5, color: MUTED,
    align: "left", valign: "top", margin: 0, isTextBox: true,
  });
}

function body(s, o) {
  s.addText(o.text, {
    x: o.x, y: o.y, w: o.w, h: o.h, fontSize: o.size || 14.5,
    color: o.color || C.text1, align: "left", valign: o.valign || "top",
    margin: 0, isTextBox: true, lineSpacingMultiple: o.ls || 1.22,
  });
}

/* ================================================================ 1  TITLE */
pres.addSection({ title: "Opening" });
{
  const s = add("DARK", "Opening");
  s.addShape(pres.ShapeType.rect, { x: 0, y: 0, w: W, h: 7.5, fill: { color: SLATE },
    line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "bg" });

  // three layer dots as the motif, introduced here
  [[TEAL, 0], [INDIGO, 0.42], [MAG, 0.84]].forEach(([c, dx], i) => {
    s.addShape(pres.ShapeType.ellipse, { x: 1.0 + dx, y: 1.5, w: 0.3, h: 0.3,
      fill: { color: c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "dot" + i });
  });

  s.addText("InfraTwin", { placeholder: "title" });
  s.addText(
    "Predictive maintenance on one PostgreSQL database.\n" +
    "Time-series anomaly detection, failure-cascade graph and semantic search — " +
    "three capabilities that normally mean three systems.",
    { placeholder: "body" });

  s.addText("DBTHON 2026", {
    x: 1.0, y: 5.75, w: 4, h: 0.3, fontSize: 12.5, bold: true, color: AMBER,
    charSpacing: 2, margin: 0, isTextBox: true });
  s.addText("5,090,501 sensor readings  ·  PostgreSQL 16  ·  pgvector  ·  " + TOPX + " faster than the unoptimised baseline", {
    x: 1.0, y: 6.1, w: 11.3, h: 0.3, fontSize: 12, color: "8A94A6",
    margin: 0, isTextBox: true });

  s.addNotes("One database doing the work of three. Everything in this deck was measured on a live database, not estimated.");
}

/* ============================================================== 2  PROBLEM */
pres.addSection({ title: "Problem" });
{
  const s = add("CONTENT", "Problem");
  head(s, "Three questions, three systems",
       "Unplanned downtime costs a mid-size plant an estimated $50k per hour. The diagnosis is slow because the answers are scattered.");

  const qs = [
    { n: "1", c: TEAL, q: "Is this machine behaving abnormally?",
      need: "Needs a time-series database",
      detail: "Compare the last hour against this machine's own history — not a global threshold." },
    { n: "2", c: INDIGO, q: "What else breaks if it fails?",
      need: "Needs a graph database",
      detail: "Walk the dependency chain upward: motor → pump → line → plant." },
    { n: "3", c: MAG, q: "When did this happen before, and what fixed it?",
      need: "Needs a vector database",
      detail: "Search years of free-text maintenance reports by meaning, not keyword." },
  ];
  qs.forEach((q, i) => {
    const x = M + i * 4.17;
    card(s, { x, y: 1.72, w: 3.86, h: 3.0, fill: SURF, name: "q" + i });
    chip(s, x + 0.28, y_(1.98), q.n, q.c, 0.5);
    s.addText(q.q, { x: x + 0.28, y: 2.62, w: 3.3, h: 0.86, fontSize: 16, bold: true,
      color: INK, valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.05 });
    s.addText(q.need, { x: x + 0.28, y: 3.52, w: 3.3, h: 0.3, fontSize: 11.5, bold: true,
      color: q.c, margin: 0, isTextBox: true });
    s.addText(q.detail, { x: x + 0.28, y: 3.86, w: 3.3, h: 0.72, fontSize: 12, color: MUTED,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });
  });
  function y_(v) { return v; }

  card(s, { x: M, y: 5.0, w: 12.2, h: 1.62, fill: "1E2630", shadow: false, name: "sting" });
  s.addText("Three databases means three deployments, three backups, three security models — and the one question that matters most cannot be asked at all.",
    { x: M + 0.42, y: 5.2, w: 11.4, h: 0.5, fontSize: 16, bold: true, color: "FFFFFF",
      margin: 0, isTextBox: true });
  s.addText("“Show me every machine downstream of the failing pump that has also had a heat-dissipation failure this month.”  That is one join across all three.",
    { x: M + 0.42, y: 5.78, w: 11.4, h: 0.6, fontSize: 13.5, color: "B9C2CF", italic: true,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  s.addNotes("Lead with the cross-domain question. That is the one a three-system stack cannot answer in a single query, and it is the reason for the whole design.");
}

/* =============================================================== 3  INSIGHT */
{
  const s = add("CONTENT", "Problem");
  head(s, "So we put all three in one database",
       "PostgreSQL 16 with pgvector. The capabilities are extensions of one engine, not three services.");

  card(s, { x: M, y: 1.75, w: 5.9, h: 4.35, fill: "F7F3F0", name: "before" });
  s.addText("The conventional stack", { x: M + 0.35, y: 2.0, w: 5.2, h: 0.36,
    fontSize: 15, bold: true, color: "8A6A52", margin: 0, isTextBox: true });
  [["InfluxDB / Timescale", "telemetry"], ["Neo4j", "asset graph"],
   ["Pinecone / Milvus", "embeddings"], ["PostgreSQL", "everything else"]].forEach(([a, b], i) => {
    s.addText([{ text: a, options: { bold: true, color: INK } },
               { text: "   " + b, options: { color: MUTED } }],
      { x: M + 0.35, y: 2.48 + i * 0.44, w: 5.2, h: 0.36, fontSize: 13.5,
        margin: 0, isTextBox: true });
  });
  s.addText("4 deployments  ·  4 backups  ·  4 auth models\ncross-domain joins written by hand in application code\nno transaction spans them",
    { x: M + 0.35, y: 4.42, w: 5.2, h: 1.3, fontSize: 13, color: "8A6A52",
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.3 });

  card(s, { x: 6.85, y: 1.75, w: 5.9, h: 4.35, fill: "EEF4F5", line: TEAL, name: "after" });
  s.addText("InfraTwin", { x: 7.2, y: 2.0, w: 5.2, h: 0.36, fontSize: 15, bold: true,
    color: TEAL, margin: 0, isTextBox: true });
  [["Partitioned tables", "telemetry", TEAL],
   ["WITH RECURSIVE", "asset graph", INDIGO],
   ["pgvector + HNSW", "embeddings", MAG],
   ["PostgreSQL", "everything else", GREEN]].forEach(([a, b, c], i) => {
    s.addShape(pres.ShapeType.ellipse, { x: 7.2, y: 2.56 + i * 0.44, w: 0.15, h: 0.15,
      fill: { color: c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "d" + i });
    s.addText([{ text: a, options: { bold: true, color: INK } },
               { text: "   " + b, options: { color: MUTED } }],
      { x: 7.45, y: 2.48 + i * 0.44, w: 5.0, h: 0.36, fontSize: 13.5,
        margin: 0, isTextBox: true });
  });
  s.addText("1 deployment  ·  1 backup  ·  1 security model\ncross-domain joins are ordinary SQL\none ACID transaction covers all of it",
    { x: 7.2, y: 4.42, w: 5.2, h: 1.3, fontSize: 13, color: TEAL,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.3 });

  s.addText("Where this stops being right: a dedicated time-series engine wins past ~10⁹ rows per table, and a dedicated vector database wins at billion-scale ANN. At plant scale, PostgreSQL is not the bottleneck — the integration tax is.",
    { x: M, y: 6.3, w: 12.2, h: 0.62, fontSize: 12, color: MUTED, italic: true,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  s.addNotes("Be ready for 'why not just use the right tool for each job'. The answer is the honest line at the bottom: we know where the ceiling is, and we are far below it.");
}

/* ========================================================== 4  ARCHITECTURE */
pres.addSection({ title: "Architecture" });
{
  const s = add("CONTENT", "Architecture");
  head(s, "How it fits together",
       "One connection. Three query patterns. The results are joinable because they share a database.");

  // sources
  card(s, { x: M, y: 1.95, w: 2.5, h: 3.5, fill: SURF, name: "src" });
  s.addText("SOURCES", { x: M + 0.22, y: 2.12, w: 2.1, h: 0.28, fontSize: 10.5, bold: true,
    color: MUTED, charSpacing: 1.4, margin: 0, isTextBox: true });
  [["Sensor stream", "1 reading/min\n101 machines"],
   ["Asset registry", "plant hierarchy"],
   ["AI4I 2020", "339 real failures"],
   ["Technician notes", "free text"]].forEach(([a, b], i) => {
    s.addText(a, { x: M + 0.22, y: 2.5 + i * 0.73, w: 2.1, h: 0.26, fontSize: 12.5,
      bold: true, color: INK, margin: 0, isTextBox: true });
    s.addText(b, { x: M + 0.22, y: 2.76 + i * 0.73, w: 2.1, h: 0.42, fontSize: 10.5,
      color: MUTED, margin: 0, isTextBox: true, lineSpacingMultiple: 1.1 });
  });

  // database
  card(s, { x: 3.42, y: 1.95, w: 4.6, h: 3.5, fill: "1E2630", shadow: false, name: "db" });
  s.addText("PostgreSQL 16", { x: 3.64, y: 2.12, w: 4.2, h: 0.34, fontSize: 16, bold: true,
    color: "FFFFFF", margin: 0, isTextBox: true });
  s.addText("one database  ·  one connection", { x: 3.64, y: 2.46, w: 4.2, h: 0.26,
    fontSize: 10.5, color: "8A94A6", margin: 0, isTextBox: true });
  [["sensor_readings", "partitioned by day · 5.09M rows", TEAL],
   ["readings_hourly", "pre-aggregated roll-up", TEAL],
   ["asset_dependencies", "directed graph, self-referencing", INDIGO],
   ["maintenance_reports", "vector(384) + HNSW index", MAG],
   ["audit_logs", "append-only, trigger-written", GREEN]].forEach(([a, b, c], i) => {
    const yy = 2.88 + i * 0.49;
    s.addShape(pres.ShapeType.roundRect, { x: 3.64, y: yy, w: 4.16, h: 0.42, rectRadius: 0.06,
      fill: { color: "2A3542" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "t" + i });
    s.addShape(pres.ShapeType.ellipse, { x: 3.78, y: yy + 0.145, w: 0.13, h: 0.13,
      fill: { color: c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "td" + i });
    s.addText([{ text: a, options: { bold: true, color: "FFFFFF", fontFace: "Courier New" } },
               { text: "   " + b, options: { color: "9AA6B6" } }],
      { x: 4.0, y: yy, w: 3.7, h: 0.42, fontSize: 10, valign: "middle",
        margin: 0, isTextBox: true });
  });

  // queries
  card(s, { x: 8.44, y: 1.95, w: 4.31, h: 3.5, fill: SURF, name: "q" });
  s.addText("THREE QUERIES", { x: 8.66, y: 2.12, w: 3.9, h: 0.28, fontSize: 10.5, bold: true,
    color: MUTED, charSpacing: 1.4, margin: 0, isTextBox: true });
  [["1", TEAL, "Anomaly detection", "roll-up + per-asset z-score"],
   ["2", INDIGO, "Failure cascade", "WITH RECURSIVE, depth-bounded"],
   ["3", MAG, "Similar failures", "HNSW nearest neighbour"]].forEach(([n, c, a, b], i) => {
    const yy = 2.58 + i * 0.95;
    chip(s, 8.66, yy, n, c, 0.38);
    s.addText(a, { x: 9.16, y: yy - 0.03, w: 3.4, h: 0.28, fontSize: 13, bold: true,
      color: INK, margin: 0, isTextBox: true });
    s.addText(b, { x: 9.16, y: yy + 0.25, w: 3.4, h: 0.28, fontSize: 10.5, color: MUTED,
      margin: 0, isTextBox: true });
  });
  s.addText("Streamlit dashboard reads all three", { x: 8.66, y: 5.02, w: 3.9, h: 0.28,
    fontSize: 11, italic: true, color: MUTED, margin: 0, isTextBox: true });

  [[3.07, "→"], [8.08, "→"]].forEach(([x, g], i) => {
    s.addText(g, { x, y: 3.5, w: 0.4, h: 0.4, fontSize: 20, color: MUTED,
      align: "center", margin: 0, isTextBox: true, objectName: "arrow" + i });
  });

  card(s, { x: M, y: 5.68, w: 12.2, h: 0.92, fill: "FDF6EE", shadow: false, name: "note" });
  s.addText([
    { text: "The payoff:  ", options: { bold: true, color: INK } },
    { text: "because all three live in one database, the cascade result and the similar-failure result can be filtered by the same asset in a single statement. Across three systems that is three round trips and a join written by hand.",
      options: { color: "7A5A3A" } }],
    { x: M + 0.35, y: 5.86, w: 11.5, h: 0.6, fontSize: 13, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.15 });

  s.addNotes("Walk left to right: sources, one database, three queries. Land on the payoff box.");
}

/* ============================================================= 5  TECH STACK */
{
  const s = add("CONTENT", "Architecture");
  head(s, "Technology stack",
       "Every choice, and what it was chosen over.");

  const rows = [
    ["Database", "PostgreSQL 16", "Everything. One engine, one backup, one security model.", INDIGO],
    ["Time-series", "TimescaleDB hypertable  or  native PARTITION BY RANGE", "Both ship. Detected at setup — the portable path needs no extension.", TEAL],
    ["Vector search", "pgvector 0.8 · HNSW, cosine", "Embedding sits beside the failure record, so similarity results join directly.", MAG],
    ["Embeddings", "sentence-transformers  all-MiniLM-L6-v2", "384-dim, CPU, ~80 MB. Offline lexical fallback if the model cannot be fetched.", MAG],
    ["Application", "Python 3 · psycopg 3 · pandas · numpy", "Server-side binding and native COPY for the bulk load.", GREEN],
    ["Dashboard", "Streamlit 1.40", "Three panels in 190 lines instead of a hand-written frontend.", GREEN],
    ["Packaging", "Docker Compose · Makefile", "make all runs the whole pipeline in about two minutes.", MUTED],
    ["Testing", "pytest · 34 tests · 28 runtime self-checks", "Constraint violations are attempted and asserted to fail.", MUTED],
    ["Dataset", "AI4I 2020 (UCI, CC BY 4.0)", "339 real failures across five documented modes.", AMBER],
  ];
  rows.forEach(([a, b, c, col], i) => {
    const yy = 1.66 + i * 0.555;
    if (i % 2 === 0) {
      s.addShape(pres.ShapeType.rect, { x: M, y: yy - 0.07, w: 12.2, h: 0.53,
        fill: { color: "F7F8FA" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "row" + i });
    }
    s.addShape(pres.ShapeType.ellipse, { x: M + 0.12, y: yy + 0.13, w: 0.14, h: 0.14,
      fill: { color: col }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "rd" + i });
    s.addText(a, { x: M + 0.38, y: yy, w: 1.55, h: 0.4, fontSize: 11.5, bold: true,
      color: MUTED, valign: "middle", margin: 0, isTextBox: true });
    s.addText(b, { x: M + 1.95, y: yy, w: 4.15, h: 0.4, fontSize: 12, bold: true,
      color: INK, valign: "middle", margin: 0, isTextBox: true });
    s.addText(c, { x: M + 6.2, y: yy, w: 5.85, h: 0.4, fontSize: 11.5, color: "55617A",
      valign: "middle", margin: 0, isTextBox: true });
  });

  s.addText("Nothing here is a hosted service. The entire project runs on a laptop with Docker, or on a free hosted PostgreSQL if Docker is blocked.",
    { x: M, y: 6.76, w: 12.2, h: 0.4, fontSize: 12, italic: true, color: MUTED,
      margin: 0, isTextBox: true });

  s.addNotes("If a judge asks why not a managed vector DB: the answer is on slide 3, and the cost line here.");
}

/* ======================================================== 6  SECTION: DESIGN */
pres.addSection({ title: "Database design" });
{
  const s = add("SECTION", "Database design");
  s.addText("Database design", { placeholder: "title" });
  s.addText("Entities, relationships, normalisation, constraints — and the decisions behind them",
    { placeholder: "body" });
  [[TEAL, 0], [INDIGO, 0.42], [MAG, 0.84]].forEach(([c, dx], i) => {
    s.addShape(pres.ShapeType.ellipse, { x: 1.0 + dx, y: 2.3, w: 0.26, h: 0.26,
      fill: { color: c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "sd" + i });
  });
  s.addNotes("Transition: this is the part the judging criteria weight most.");
}

/* ================================================================= 7  ER */
{
  const s = add("CONTENT", "Database design");
  head(s, "Database relations",
       "Generated directly from the live pg_catalog — this diagram cannot drift from the schema. 9 tables · 1 materialized view · 17 indexes · 12 constraints.");
  s.addImage({ path: path.join(ROOT, "docs/er_diagram_slide.png"),
    x: 0.42, y: 1.42, w: 12.46, h: 5.93, objectName: "er" });
  s.addNotes("Point out: assets is the single source of asset identity; everything references it. The dashed line to audit_logs is a trigger, not a foreign key - an audit trail must survive the row it describes.");
}

/* ==================================================== 8  SCHEMA DECISIONS */
{
  const s = add("CONTENT", "Database design");
  head(s, "Three design decisions worth defending",
       "");

  const items = [
    { n: "1", c: INDIGO, t: "An edge table, not a parent_id column",
      p: "asset_dependencies is a self-referencing M:N on assets.",
      rows: [["One asset, several parents", "impossible", "natural"],
             ["Typed relationship", "nowhere to put it", "a column"],
             ["Add a redundant feed", "schema change", "one INSERT"]],
      close: "A pump fed by two lines is ordinary in a real plant. parent_id cannot express it." },
    { n: "2", c: MAG, t: "UNIQUE is what makes it 1:1",
      p: "The foreign key alone would permit many reports per failure.",
      code: "event_id INT NOT NULL UNIQUE\n  REFERENCES failure_events(event_id)",
      close: "Without UNIQUE, the similar-failure search could return three rows that are all the same incident." },
    { n: "3", c: GREEN, t: "CHECK constraints over lookup tables",
      p: "asset_type and failure_type are TEXT + CHECK, not foreign keys to a lookup table.",
      close: "Both are closed sets — four physical types, five AI4I codes. A lookup table buys flexibility nobody needs and costs a join on every query. If the set became user-defined, the lookup table would be correct." },
  ];

  // 1
  card(s, { x: M, y: 1.6, w: 3.9, h: 4.35, fill: SURF, name: "d1" });
  chip(s, M + 0.26, 1.84, items[0].n, items[0].c, 0.42);
  s.addText(items[0].t, { x: M + 0.26, y: 2.38, w: 3.5, h: 0.62, fontSize: 15, bold: true,
    color: INK, valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.05 });
  s.addText(items[0].p, { x: M + 0.26, y: 3.0, w: 3.34, h: 0.46, fontSize: 11.5, color: MUTED,
    valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.12 });
  items[0].rows.forEach(([a, b, c], i) => {
    const yy = 3.52 + i * 0.47;
    s.addText(a, { x: M + 0.26, y: yy, w: 3.5, h: 0.22, fontSize: 10.5, bold: true,
      color: INK, margin: 0, isTextBox: true });
    s.addText([{ text: b, options: { color: "A06A5A" } },
               { text: "   →   ", options: { color: MUTED } },
               { text: c, options: { color: INDIGO, bold: true } }],
      { x: M + 0.26, y: yy + 0.2, w: 3.5, h: 0.24, fontSize: 10.5, margin: 0, isTextBox: true });
  });
  s.addText(items[0].close, { x: M + 0.26, y: 5.0, w: 3.5, h: 0.78, fontSize: 11,
    italic: true, color: "55617A", margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  // 2
  card(s, { x: 4.7, y: 1.6, w: 3.9, h: 4.35, fill: SURF, name: "d2" });
  chip(s, 4.96, 1.84, items[1].n, items[1].c, 0.42);
  s.addText(items[1].t, { x: 4.96, y: 2.38, w: 3.5, h: 0.62, fontSize: 15, bold: true,
    color: INK, valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.05 });
  s.addText(items[1].p, { x: 4.96, y: 3.0, w: 3.34, h: 0.46, fontSize: 11.5, color: MUTED,
    valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.12 });
  code(s, { x: 4.96, y: 3.56, w: 3.5, h: 0.78, text: items[1].code, size: 9.5 });
  s.addText(items[1].close, { x: 4.96, y: 4.5, w: 3.5, h: 1.2, fontSize: 11, italic: true,
    color: "55617A", margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  // 3
  card(s, { x: 8.85, y: 1.6, w: 3.9, h: 4.35, fill: SURF, name: "d3" });
  chip(s, 9.11, 1.84, items[2].n, items[2].c, 0.42);
  s.addText(items[2].t, { x: 9.11, y: 2.38, w: 3.34, h: 0.62, fontSize: 15, bold: true,
    color: INK, valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.05 });
  s.addText(items[2].p, { x: 9.11, y: 3.0, w: 3.34, h: 0.7, fontSize: 11.5, color: MUTED,
    valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.12 });
  s.addText(items[2].close, { x: 9.11, y: 3.96, w: 3.34, h: 1.8, fontSize: 11, italic: true,
    color: "55617A", margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  card(s, { x: M, y: 6.14, w: 12.2, h: 0.82, fill: "EEF4F5", shadow: false, name: "nf" });
  s.addText([
    { text: "Normal form:  ", options: { bold: true, color: TEAL } },
    { text: "3NF throughout. sensor_readings stores asset_id, never the machine's name or location — renaming a machine is one UPDATE, not five million. readings_hourly is the one deliberate denormalisation, and it is a measured trade.",
      options: { color: "35505A" } }],
    { x: M + 0.35, y: 6.3, w: 11.5, h: 0.52, fontSize: 12.5, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.15 });

  s.addNotes("These are the three questions a database examiner asks. Decision 3 is the one to be humble about - name the condition under which the other choice wins.");
}

/* ============================================================= 9  INTEGRITY */
{
  const s = add("CONTENT", "Database design");
  head(s, "Integrity lives in the database, not the application",
       "Every rule holds no matter what connects — the app, psql, or a migration script.");

  const cons = [
    ["assets", "UNIQUE (name)", "Two machines with the same tag"],
    ["assets", "CHECK asset_type IN (…)", "Typos becoming new asset classes"],
    ["asset_dependencies", "PK (asset_id, dependent_id)", "Duplicate edges"],
    ["asset_dependencies", "CHECK asset_id <> dependent_id", "A machine depending on itself"],
    ["failure_events", "CHECK failure_type IN (…)", "Codes outside the AI4I set"],
    ["maintenance_reports", "UNIQUE (event_id)", "More than one report per failure"],
    ["all foreign keys", "ON DELETE CASCADE", "Orphaned readings and edges"],
  ];
  s.addText("CONSTRAINTS", { x: M, y: 1.62, w: 3, h: 0.26, fontSize: 10.5, bold: true,
    color: MUTED, charSpacing: 1.4, margin: 0, isTextBox: true });
  cons.forEach(([a, b, c], i) => {
    const yy = 1.98 + i * 0.52;
    if (i % 2 === 0) {
      s.addShape(pres.ShapeType.rect, { x: M, y: yy - 0.06, w: 7.55, h: 0.5,
        fill: { color: "F7F8FA" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "cr" + i });
    }
    s.addText(a, { x: M + 0.12, y: yy, w: 1.9, h: 0.38, fontSize: 10, color: MUTED,
      fontFace: "Courier New", valign: "middle", margin: 0, isTextBox: true });
    s.addText(b, { x: M + 2.05, y: yy, w: 2.75, h: 0.38, fontSize: 10, bold: true,
      color: INK, fontFace: "Courier New", valign: "middle", margin: 0, isTextBox: true });
    s.addText(c, { x: M + 4.85, y: yy, w: 2.65, h: 0.38, fontSize: 10.5, color: "55617A",
      valign: "middle", margin: 0, isTextBox: true });
  });

  card(s, { x: 8.42, y: 1.56, w: 4.33, h: 2.72, fill: SURF, name: "trg" });
  s.addText("The audit trigger", { x: 8.66, y: 1.74, w: 3.85, h: 0.3, fontSize: 14,
    bold: true, color: INK, margin: 0, isTextBox: true });
  s.addText("AFTER INSERT OR UPDATE OR DELETE, FOR EACH ROW. It cannot be bypassed — it fires for the app, for psql and for a migration alike.",
    { x: 8.66, y: 2.06, w: 3.85, h: 0.7, fontSize: 11.5, color: MUTED, margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.15 });
  code(s, { x: 8.66, y: 2.82, w: 3.85, h: 1.18,
    text: "IF TG_OP = 'DELETE' THEN\n  v_pk := OLD.report_id;\nELSE\n  v_pk := NEW.report_id;\nEND IF;", size: 9.5 });

  card(s, { x: 8.42, y: 4.52, w: 4.33, h: 1.76, fill: "FDF6EE", shadow: false, name: "why" });
  s.addText("Why the explicit branch", { x: 8.66, y: 4.68, w: 3.85, h: 0.28, fontSize: 12,
    bold: true, color: "8A6A52", margin: 0, isTextBox: true });
  s.addText("In a DELETE trigger NEW is unassigned. COALESCE(NEW.report_id, OLD.report_id) happens to work on PostgreSQL 16 — we tested it — but it relies on field access against an unassigned record returning NULL rather than raising. One IF removes the question.",
    { x: 8.66, y: 4.98, w: 3.85, h: 1.24, fontSize: 10.5, color: "7A5A3A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.12 });

  s.addText("verify.py attempts each violation and asserts it is rejected — 28 checks, all passing. These are demonstrated, not claimed.",
    { x: M, y: 5.72, w: 7.55, h: 0.6, fontSize: 12, italic: true, color: TEAL,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });

  s.addNotes("The trigger footnote is a good place to show we tested rather than assumed. We found the original pattern works on PG16 and hardened it anyway.");
}

/* =========================================================== 10  TIME-SERIES */
pres.addSection({ title: "The three layers" });
{
  const s = add("CONTENT", "The three layers");
  chip(s, M, 0.42, "1", TEAL, 0.52);
  s.addText("Time-series layer", { x: M + 0.72, y: 0.38, w: 8, h: 0.6, fontSize: 32,
    bold: true, color: INK, fontFace: THEME.headFontFace, valign: "middle",
    margin: 0, isTextBox: true });
  s.addText("Partitioning for the raw stream, a pre-aggregated roll-up for the baseline.",
    { x: M + 0.72, y: 1.0, w: 11.5, h: 0.32, fontSize: 13.5, color: MUTED,
      margin: 0, isTextBox: true });

  card(s, { x: M, y: 1.62, w: 6.0, h: 2.5, fill: SURF, name: "part" });
  s.addText("One partition per day", { x: M + 0.3, y: 1.8, w: 5.4, h: 0.3, fontSize: 14,
    bold: true, color: INK, margin: 0, isTextBox: true });
  code(s, { x: M + 0.3, y: 2.16, w: 5.4, h: 0.6,
    text: "CREATE TABLE sensor_readings (...)\n  PARTITION BY RANGE (time);", size: 10 });
  s.addText([{ text: "Partition pruning. ", options: { bold: true, color: TEAL } },
             { text: "A one-hour query reads one partition, not 48 — the planner discards the rest before executing.", options: { color: "55617A" } }],
    { x: M + 0.3, y: 2.86, w: 5.4, h: 0.5, fontSize: 11.5, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.12 });
  s.addText([{ text: "Cheap retention. ", options: { bold: true, color: TEAL } },
             { text: "Dropping old data is DROP TABLE — instant — instead of a DELETE that rewrites millions of rows.", options: { color: "55617A" } }],
    { x: M + 0.3, y: 3.42, w: 5.4, h: 0.5, fontSize: 11.5, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.12 });

  card(s, { x: 6.75, y: 1.62, w: 6.0, h: 2.5, fill: SURF, name: "roll" });
  s.addText("readings_hourly roll-up", { x: 7.05, y: 1.8, w: 5.4, h: 0.3, fontSize: 14,
    bold: true, color: INK, margin: 0, isTextBox: true });
  s.addText("Hourly averages per asset, computed once, not on every dashboard load.",
    { x: 7.05, y: 2.12, w: 5.4, h: 0.44, fontSize: 11.5, color: MUTED, margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.12 });
  [["Storage cost", "+10 MB on top of 551 MB", MUTED],
   ["Freshness cost", "stale until refreshed", MUTED],
   ["30-day aggregate", "1,200 ms → 44 ms", TEAL]].forEach(([a, b, c], i) => {
    const yy = 2.66 + i * 0.46;
    s.addText(a, { x: 7.05, y: yy, w: 2.3, h: 0.3, fontSize: 11.5, color: MUTED,
      margin: 0, isTextBox: true });
    s.addText(b, { x: 9.4, y: yy, w: 3.05, h: 0.3, fontSize: 11.5, bold: c === TEAL,
      color: c, margin: 0, isTextBox: true });
  });

  card(s, { x: M, y: 4.38, w: 12.2, h: 1.22, fill: "EEF4F5", line: "BBD6DC", shadow: false, name: "eng" });
  s.addText("Two engines, one schema", { x: M + 0.35, y: 4.52, w: 11.5, h: 0.3,
    fontSize: 13.5, bold: true, color: TEAL, margin: 0, isTextBox: true });
  s.addText("TimescaleDB hypertable where available; native PARTITION BY RANGE where it is not. Column definitions, indexes and every downstream query are identical — only the partitioning mechanism differs. make schema detects which to use, so a locked-down lab PC or a hosted PostgreSQL with pgvector runs the same demo.",
    { x: M + 0.35, y: 4.84, w: 11.5, h: 0.68, fontSize: 12, color: "35505A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.15 });

  stat(s, { x: M, y: 5.82, w: 3, value: "5,090,501", label: "sensor readings loaded", size: 32, color: TEAL });
  stat(s, { x: 3.8, y: 5.82, w: 3, value: "52 s", label: "to generate and load them", size: 32, color: TEAL });
  stat(s, { x: 7.0, y: 5.82, w: 3, value: "48", label: "daily partitions", size: 32, color: TEAL });
  stat(s, { x: 10.1, y: 5.82, w: 3, value: TOPX, label: "faster point query", size: 32, color: AMBER });

  s.addNotes("The two-engines point is worth making: it is the difference between a demo that runs anywhere and one that needs a specific Docker image.");
}

/* ================================================================ 11  GRAPH */
{
  const s = add("CONTENT", "The three layers");
  chip(s, M, 0.42, "2", INDIGO, 0.52);
  s.addText("Graph layer", { x: M + 0.72, y: 0.38, w: 8, h: 0.6, fontSize: 32, bold: true,
    color: INK, fontFace: THEME.headFontFace, valign: "middle", margin: 0, isTextBox: true });
  s.addText("A recursive CTE walks the dependency graph upward from the failing machine.",
    { x: M + 0.72, y: 1.0, w: 11.5, h: 0.32, fontSize: 13.5, color: MUTED,
      margin: 0, isTextBox: true });

  code(s, { x: M, y: 1.66, w: 6.35, h: 2.72, size: 10.5, text:
    "WITH RECURSIVE impacted AS (\n" +
    "  SELECT d.dependent_id, 1 AS depth\n" +
    "  FROM   asset_dependencies d\n" +
    "  WHERE  d.asset_id = :asset\n" +
    "UNION ALL\n" +
    "  SELECT d.dependent_id, i.depth + 1\n" +
    "  FROM   asset_dependencies d\n" +
    "  JOIN   impacted i ON d.asset_id = i.asset_id\n" +
    "  WHERE  i.depth < 5)\n" +
    "SELECT a.name, a.asset_type, i.depth\n" +
    "FROM impacted i JOIN assets a USING (asset_id)\n" +
    "ORDER BY i.depth;" });

  // cascade visual
  card(s, { x: 7.1, y: 1.66, w: 5.65, h: 2.72, fill: SURF, name: "casc" });
  s.addText("L3-P2-Motor-3 fails", { x: 7.38, y: 1.84, w: 5.1, h: 0.3, fontSize: 13,
    bold: true, color: AMBER, margin: 0, isTextBox: true });
  [["Motor", "the failure", AMBER, 0],
   ["Pump", "depth 1  ·  powers", INDIGO, 1],
   ["Line", "depth 2  ·  feeds", INDIGO, 2],
   ["Plant", "depth 3  ·  feeds", INDIGO, 3]].forEach(([a, b, c, i]) => {
    const yy = 2.16 + i * 0.46;
    s.addShape(pres.ShapeType.roundRect, { x: 7.38 + i * 0.22, y: yy, w: 2.3, h: 0.4,
      rectRadius: 0.06, fill: { color: i === 0 ? "FBEEE0" : "EDEEFA" },
      line: { color: c, width: 1 }, objectName: "casc" + i });
    s.addText(a, { x: 7.38 + i * 0.22, y: yy, w: 2.3, h: 0.4, fontSize: 11.5, bold: true,
      color: c, align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText(b, { x: 9.78 + i * 0.22, y: yy, w: 2.2, h: 0.4, fontSize: 10.5, color: MUTED,
      valign: "middle", margin: 0, isTextBox: true });
  });
  s.addText("3 assets impacted  ·  0.4 ms", { x: 7.38, y: 4.0, w: 5.1, h: 0.26,
    fontSize: 11, italic: true, color: INDIGO, margin: 0, isTextBox: true });

  card(s, { x: M, y: 4.6, w: 6.35, h: 1.95, fill: "FDF6EE", shadow: false, name: "guard" });
  s.addText("depth < 5 is not decoration", { x: M + 0.3, y: 4.78, w: 5.75, h: 0.3,
    fontSize: 13, bold: true, color: "8A6A52", margin: 0, isTextBox: true });
  s.addText("A mis-entered pair — (A→B) and (B→A) — forms a cycle, and UNION ALL without a bound never terminates. The test suite inserts exactly that cycle and asserts the query still returns.",
    { x: M + 0.3, y: 5.1, w: 5.75, h: 1.2, fontSize: 12, color: "7A5A3A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.18 });

  card(s, { x: 7.1, y: 4.6, w: 5.65, h: 1.95, fill: SURF, name: "dir" });
  s.addText("Edge direction", { x: 7.38, y: 4.78, w: 5.1, h: 0.3, fontSize: 13, bold: true,
    color: INK, margin: 0, isTextBox: true });
  s.addText([{ text: "(asset_id) → (dependent_id)", options: { fontFace: "Courier New", bold: true, color: INDIGO } }],
    { x: 7.38, y: 5.1, w: 5.1, h: 0.28, fontSize: 11.5, margin: 0, isTextBox: true });
  s.addText("Read as “dependent_id depends on asset_id”. A motor powers a pump, so the row is (motor, pump, 'powers') and a motor failure cascades upward to the plant.",
    { x: 7.38, y: 5.44, w: 5.1, h: 0.95, fontSize: 12, color: "55617A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.18 });

  s.addNotes("If asked why not Neo4j: depth-3 traversals over 100 edges are not where a graph database earns its keep. At depth 10 over millions of edges, it would.");
}

/* =============================================================== 12  VECTOR */
{
  const s = add("CONTENT", "The three layers");
  chip(s, M, 0.42, "3", MAG, 0.52);
  s.addText("Vector layer", { x: M + 0.72, y: 0.38, w: 8, h: 0.6, fontSize: 32, bold: true,
    color: INK, fontFace: THEME.headFontFace, valign: "middle", margin: 0, isTextBox: true });
  s.addText("Report text becomes a vector(384). Finding a similar past failure is one ORDER BY.",
    { x: M + 0.72, y: 1.0, w: 11.5, h: 0.32, fontSize: 13.5, color: MUTED,
      margin: 0, isTextBox: true });

  code(s, { x: M, y: 1.66, w: 6.35, h: 1.52, size: 10.5, text:
    "SELECT f.failure_type, r.summary, r.action_taken,\n" +
    "       1 - (r.embedding <=> :qvec) AS similarity\n" +
    "FROM   maintenance_reports r\n" +
    "JOIN   failure_events f USING (event_id)\n" +
    "ORDER  BY r.embedding <=> :qvec\n" +
    "LIMIT  3;" });

  s.addText([{ text: "<=>", options: { fontFace: "Courier New", bold: true, color: MAG } },
             { text: "  is cosine distance. Because the HNSW index is built on vector_cosine_ops, this is an index scan — not a scan of every report. The JOIN is the part a separate vector database cannot do.",
               options: { color: "55617A" } }],
    { x: M, y: 3.32, w: 6.35, h: 0.86, fontSize: 12, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.18 });

  card(s, { x: 7.1, y: 1.66, w: 5.65, h: 2.54, fill: SURF, name: "res" });
  s.addText("Operator types a symptom", { x: 7.38, y: 1.84, w: 5.1, h: 0.28, fontSize: 12,
    bold: true, color: MUTED, margin: 0, isTextBox: true });
  s.addText("“temperature rising while rotational speed is low, coolant problem”",
    { x: 7.38, y: 2.14, w: 5.1, h: 0.44, fontSize: 12.5, italic: true, color: INK,
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.1 });
  [["HDF", "0.488", "Process temperature reached 318.2\u00a0K, speed stayed low"],
   ["HDF", "0.479", "Heat dissipation failure, coolant could not carry heat away"]].forEach(([t, sim, txt], i) => {
    const yy = 2.76 + i * 0.68;
    s.addShape(pres.ShapeType.roundRect, { x: 7.38, y: yy, w: 5.1, h: 0.6, rectRadius: 0.06,
      fill: { color: "FBEFF4" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "hit" + i });
    s.addText(t, { x: 7.5, y: yy + 0.06, w: 0.58, h: 0.24, fontSize: 10, bold: true,
      color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addShape(pres.ShapeType.roundRect, { x: 7.5, y: yy + 0.06, w: 0.58, h: 0.24,
      rectRadius: 0.04, fill: { color: MAG }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "tag" + i });
    s.addText(t, { x: 7.5, y: yy + 0.06, w: 0.58, h: 0.24, fontSize: 9.5, bold: true,
      color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true });
    s.addText("sim " + sim, { x: 8.14, y: yy + 0.06, w: 0.9, h: 0.24, fontSize: 9.5,
      color: MAG, valign: "middle", margin: 0, isTextBox: true });
    s.addText(txt, { x: 7.5, y: yy + 0.3, w: 4.9, h: 0.28, fontSize: 9.5, color: "55617A",
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.05 });
  });

  card(s, { x: M, y: 4.42, w: 6.35, h: 2.1, fill: "FDF6EE", shadow: false, name: "fail" });
  s.addText("The failure mode worth knowing", { x: M + 0.3, y: 4.6, w: 5.75, h: 0.3,
    fontSize: 13, bold: true, color: "8A6A52", margin: 0, isTextBox: true });
  s.addText("Embedding stored reports with one model and queries with another produces no error at all. Both vectors are valid, the distances compute, the results are nonsense. seed.py and app.py therefore call the same loader, and the schema's vector(384) is checked against the model's width at load time.",
    { x: M + 0.3, y: 4.92, w: 5.75, h: 1.4, fontSize: 12, color: "7A5A3A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.18 });

  card(s, { x: 7.1, y: 4.42, w: 5.65, h: 2.1, fill: "EEF4F5", shadow: false, name: "off" });
  s.addText("It works without the internet", { x: 7.38, y: 4.6, w: 5.1, h: 0.3, fontSize: 13,
    bold: true, color: TEAL, margin: 0, isTextBox: true });
  s.addText("If the model cannot be downloaded, seed.py falls back to a deterministic lexical embedder: synonym-folded bag of words, IDF-weighted, same 384 dimensions, same index, same queries. It returns the correct failure mode on all five test symptoms. The demo cannot be taken down by the venue's wifi.",
    { x: 7.38, y: 4.92, w: 5.1, h: 1.44, fontSize: 12, color: "35505A", margin: 0,
      isTextBox: true, lineSpacingMultiple: 1.18 });

  s.addNotes("The offline fallback is a robustness story, not a shortcut. Mention that the schema and queries are byte-identical either way.");
}

/* ============================================================== 13  RESULTS */
pres.addSection({ title: "Results" });
{
  const s = add("CONTENT", "Results");
  head(s, "Measured, not estimated",
       "Baseline is the identical rows in one unpartitioned heap, no indexes. Median of 7 runs, 5,090,501 readings \u2014 figures read live from benchmark_results.csv.");

  s.addChart(pres.ChartType.bar, [{
    name: "Speed-up",
    labels: BENCH.map((r) => chartLabel(r[0])),
    values: BENCH.map((r) => r[3]),
  }], {
    x: M, y: 1.64, w: 7.5, h: 3.9,
    barDir: "col",
    chartColors: [AMBER],
    showTitle: false,
    showLegend: false,
    showValue: true,
    dataLabelPosition: "outEnd",
    dataLabelFormatCode: '0.0"×"',
    dataLabelColor: INK,
    dataLabelFontSize: 12,
    dataLabelFontBold: true,
    dataLabelFontFace: "+mn-lt",
    catAxisLabelColor: "55617A",
    catAxisLabelFontSize: 10,
    catAxisLabelFontFace: "+mn-lt",
    valAxisLabelColor: "8A94A6",
    valAxisLabelFontSize: 10,
    valAxisLabelFontFace: "+mn-lt",
    valAxisTitle: "times faster than baseline",
    showValAxisTitle: true,
    valAxisTitleColor: "8A94A6",
    valAxisTitleFontSize: 10,
    valAxisTitleFontFace: "+mn-lt",
    valGridLine: { color: "E6E9EE", size: 1 },
    catGridLine: { style: "none" },
    barGapWidthPct: 58,
    valAxisMaxVal: Math.ceil((TOP * 1.14) / 10) * 10,
    valAxisMinVal: 0,
  });

  const rows = BENCH.map((r) => [r[0], r[1] + " ms", r[2] + " ms"]);
  s.addText("RAW TIMINGS", { x: 8.35, y: 1.64, w: 4.4, h: 0.26, fontSize: 10.5, bold: true,
    color: MUTED, charSpacing: 1.4, margin: 0, isTextBox: true });
  s.addText("baseline", { x: 10.85, y: 1.94, w: 0.95, h: 0.24, fontSize: 9.5, color: MUTED,
    align: "right", margin: 0, isTextBox: true });
  s.addText("optimised", { x: 11.85, y: 1.94, w: 0.78, h: 0.24, fontSize: 9.5, color: AMBER,
    align: "right", margin: 0, isTextBox: true });
  rows.forEach(([a, b, c], i) => {
    const yy = 2.24 + i * 0.56;
    if (i % 2 === 0) {
      s.addShape(pres.ShapeType.rect, { x: 8.35, y: yy - 0.06, w: 4.4, h: 0.52,
        fill: { color: "F7F8FA" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "rt" + i });
    }
    s.addText(a, { x: 8.47, y: yy, w: 2.5, h: 0.4, fontSize: 10, color: INK,
      valign: "middle", margin: 0, isTextBox: true, lineSpacingMultiple: 1.0 });
    s.addText(b, { x: 10.85, y: yy, w: 0.95, h: 0.4, fontSize: 10.5, color: MUTED,
      align: "right", valign: "middle", margin: 0, isTextBox: true });
    s.addText(c, { x: 11.85, y: yy, w: 0.78, h: 0.4, fontSize: 10.5, bold: true, color: AMBER,
      align: "right", valign: "middle", margin: 0, isTextBox: true });
  });

  card(s, { x: 8.35, y: 4.62, w: 4.4, h: 0.92, fill: "EEF4F5", shadow: false, name: "roll" });
  s.addText([{ text: "10 MB", options: { bold: true, color: TEAL, fontSize: 16 } },
             { text: "  answers the 30-day question that would otherwise scan ", options: { color: "35505A" } },
             { text: "551 MB", options: { bold: true, color: TEAL } }],
    { x: 8.55, y: 4.78, w: 4.0, h: 0.6, fontSize: 11.5, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.15 });

  card(s, { x: M, y: 5.74, w: 12.2, h: 1.04, fill: "FDF6EE", shadow: false, name: "cost" });
  s.addText([{ text: "The cost, stated plainly:  ", options: { bold: true, color: "8A6A52" } },
             { text: "the optimised table occupies 551 MB against the baseline's 293 MB. The indexes are 88% more storage. That is the deal we are making — disk is cheap, the operator waiting on a dashboard is not.",
               options: { color: "7A5A3A" } }],
    { x: M + 0.35, y: 5.92, w: 11.5, h: 0.7, fontSize: 12.5, margin: 0, isTextBox: true,
      lineSpacingMultiple: 1.15 });

  s.addNotes("Volunteer the storage cost before a judge finds it. It reads as rigour, not weakness.");
}

/* ============================================================ 14  TRADE-OFFS */
{
  const s = add("CONTENT", "Results");
  head(s, "What we got wrong, and fixed",
       "Three findings from testing against a live database rather than reasoning about it.");

  const f = [
    { c: AMBER, t: "A z-score alone raised a false positive",
      p: "On a machine whose hourly average varies by 0.1 K, a 0.3 K drift crosses z > 3. Testing surfaced exactly one such alert at z = 3.0, alongside the three real ones.",
      fix: "The query now requires an absolute rise of 1.0 K as well. Three injected anomalies, three detected, no false positives." },
    { c: MAG, t: "The HNSW index is ignored on a small table",
      p: "On a few hundred reports PostgreSQL picks a sequential scan — correctly, because scanning that many rows is cheaper than walking a graph index.",
      fix: "The benchmark uses 20,000 vectors, where HNSW wins by the margin on the results slide. The test suite asserts the index is used there and records the sequential scan on the small table rather than hiding it." },
    { c: TEAL, t: "The demo decayed as the clock advanced",
      p: "The anomaly is injected relative to seed time, but the query compares windows relative to now(). After an hour the injected rows drift into the baseline they are measured against and the z-scores collapse.",
      fix: "make demo reverts the previous injection exactly and re-applies it at the current time, in one transaction. Run it before presenting." },
  ];
  f.forEach((it, i) => {
    const x = M + i * 4.17;
    card(s, { x, y: 1.66, w: 3.86, h: 4.55, fill: SURF, name: "f" + i });
    s.addShape(pres.ShapeType.ellipse, { x: x + 0.28, y: 1.92, w: 0.16, h: 0.16,
      fill: { color: it.c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "fd" + i });
    s.addText(it.t, { x: x + 0.28, y: 2.18, w: 3.3, h: 0.66, fontSize: 14.5, bold: true,
      color: INK, valign: "top", margin: 0, isTextBox: true, lineSpacingMultiple: 1.06 });
    s.addText(it.p, { x: x + 0.28, y: 2.9, w: 3.3, h: 1.5, fontSize: 11.5, color: "55617A",
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.18 });
    s.addShape(pres.ShapeType.roundRect, { x: x + 0.28, y: 4.42, w: 3.3, h: 1.62,
      rectRadius: 0.07, fill: { color: "FFFFFF" }, line: { color: it.c, width: 1 },
      objectName: "fx" + i });
    s.addText("FIXED", { x: x + 0.44, y: 4.54, w: 1.2, h: 0.24, fontSize: 9, bold: true,
      color: it.c, charSpacing: 1.2, margin: 0, isTextBox: true });
    s.addText(it.fix, { x: x + 0.44, y: 4.8, w: 2.98, h: 1.16, fontSize: 11, color: "55617A",
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.15 });
  });

  s.addText("Everything in this deck was run against a live PostgreSQL 16 database. 34 tests and 28 runtime self-checks pass.",
    { x: M, y: 6.42, w: 12.2, h: 0.4, fontSize: 12.5, italic: true, color: MUTED,
      margin: 0, isTextBox: true });

  s.addNotes("This is the slide that separates a project that was built from one that was only designed. Do not skip it to save time.");
}

/* ================================================================= 15  CLOSE */
pres.addSection({ title: "Close" });
{
  const s = add("SECTION", "Close");
  s.addText("One database. Three capabilities. " + TOPX + " faster.", { placeholder: "title" });
  s.addText("Run it yourself:  docker compose up -d  ·  make all  ·  make app",
    { placeholder: "body" });

  [["Where it goes next", "Streaming ingestion with logical replication; alert routing from the cascade result; compression policies on chunks older than 7 days."],
   ["What it already proves", "Time-series, graph and vector workloads coexist in PostgreSQL at plant scale — with one backup, one security model and joins across all three."]]
   .forEach(([t, b], i) => {
    const x = 1.0 + i * 5.75;
    s.addShape(pres.ShapeType.roundRect, { x, y: 5.0, w: 5.3, h: 1.65, rectRadius: 0.1,
      fill: { color: "2A3542" }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "c" + i });
    s.addText(t, { x: x + 0.3, y: 5.2, w: 4.7, h: 0.3, fontSize: 13, bold: true,
      color: i === 0 ? AMBER : "5EC5D8", margin: 0, isTextBox: true });
    s.addText(b, { x: x + 0.3, y: 5.52, w: 4.7, h: 1.0, fontSize: 11.5, color: "B9C2CF",
      margin: 0, isTextBox: true, lineSpacingMultiple: 1.18 });
  });

  [[TEAL, 0], [INDIGO, 0.42], [MAG, 0.84]].forEach(([c, dx], i) => {
    s.addShape(pres.ShapeType.ellipse, { x: 1.0 + dx, y: 2.3, w: 0.26, h: 0.26,
      fill: { color: c }, line: { color: "FFFFFF", width: 0, type: "none" }, objectName: "cd" + i });
  });

  s.addNotes("Close on the proof line, then take questions. Have the dashboard already running on a second window.");
}

/* ------------------------------------------------------------------ write */
(async () => {
  const out = path.join(ROOT, "InfraTwin_DBTHON_2026.pptx");
  await pres.writeFile({ fileName: out });
  const { applyTheme } = require("/mnt/skills/public/pptx/scripts/apply_theme.js");
  await applyTheme(out, THEME);
  console.log("wrote " + out);
})();
