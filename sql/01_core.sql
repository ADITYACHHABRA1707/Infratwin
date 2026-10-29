-- =============================================================================
-- ChainTwin  |  01_core.sql
-- Supply-chain digital twin: parties, products, lots, the lot genealogy graph,
-- shipments, chain of custody, lab results, incident memory.
--
-- The key modelling decision: a LOT is the node of the graph, and every
-- physical hand-off (raw -> batch -> finished -> DC stock -> store stock) is a
-- row in lot_flows. Sensors attach to UNITS (a truck, a cold room), not lots,
-- and `custody` records which lot sat in which unit during which interval.
-- That one table is what lets an excursion on a unit be turned into the exact
-- list of lots it touched.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- parties : every organisation that handles product. They are also the signers
-- on the ledger, so one table serves both the business and the trust model.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parties (
  party_id   SERIAL PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  party_type TEXT NOT NULL CHECK (party_type IN
             ('Supplier','Manufacturer','Carrier','Distributor','Retailer')),
  city       TEXT,
  CONSTRAINT parties_name_not_blank CHECK (length(btrim(name)) > 0)
);

-- -----------------------------------------------------------------------------
-- products : the spec. temp_min/temp_max is the storage window the product must
-- stay inside; NULL means ambient (no cold-chain requirement).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
  product_id      SERIAL PRIMARY KEY,
  sku             TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  category        TEXT NOT NULL CHECK (category IN ('RAW','FINISHED')),
  temp_min        REAL,
  temp_max        REAL,
  shelf_life_days INT  NOT NULL CHECK (shelf_life_days > 0),
  CONSTRAINT temp_window_ordered CHECK (temp_min IS NULL OR temp_max IS NULL OR temp_min < temp_max)
);

-- -----------------------------------------------------------------------------
-- units : anything with a thermometer -- reefer truck, cold room, pasteuriser,
-- store display case. temp_min/temp_max is the limit the unit is held to.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS units (
  unit_id   SERIAL PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  unit_type TEXT NOT NULL CHECK (unit_type IN ('Reefer','ColdRoom','ProcessLine','DisplayCase')),
  party_id  INT  NOT NULL REFERENCES parties(party_id),
  setpoint  REAL NOT NULL,
  temp_min  REAL NOT NULL,
  temp_max  REAL NOT NULL,
  CONSTRAINT unit_window_ordered CHECK (temp_min < temp_max)
);
CREATE INDEX IF NOT EXISTS idx_units_party ON units(party_id);

-- -----------------------------------------------------------------------------
-- lots : the graph node.
--   RAW      arrived from a supplier
--   BATCH    one production run at the plant (mixes several RAW lots)
--   FINISHED packed product leaving the plant
--   STOCK    FINISHED product as held by a distributor or retailer
-- status is the only mutable business column; every change is audited by
-- trigger and, when made through the app, anchored on the ledger.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lots (
  lot_id     SERIAL PRIMARY KEY,
  lot_code   TEXT NOT NULL UNIQUE,
  product_id INT  NOT NULL REFERENCES products(product_id),
  party_id   INT  NOT NULL REFERENCES parties(party_id),   -- holder / producer
  lot_type   TEXT NOT NULL CHECK (lot_type IN ('RAW','BATCH','FINISHED','STOCK')),
  quantity   NUMERIC(12,2) NOT NULL CHECK (quantity > 0),
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  status     TEXT NOT NULL DEFAULT 'OK' CHECK (status IN ('OK','HOLD','RECALLED'))
);
CREATE INDEX IF NOT EXISTS idx_lots_type_created ON lots(lot_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lots_party        ON lots(party_id);

-- shipments : one truck trip. A trip carries many lots; custody rows say which.
CREATE TABLE IF NOT EXISTS shipments (
  shipment_id   SERIAL PRIMARY KEY,
  shipment_code TEXT NOT NULL UNIQUE,
  origin_party  INT NOT NULL REFERENCES parties(party_id),
  dest_party    INT NOT NULL REFERENCES parties(party_id),
  carrier_party INT NOT NULL REFERENCES parties(party_id),
  unit_id       INT NOT NULL REFERENCES units(unit_id),
  depart_time   TIMESTAMPTZ NOT NULL,
  arrive_time   TIMESTAMPTZ NOT NULL,
  CONSTRAINT arrives_after_departure CHECK (arrive_time > depart_time)
);
CREATE INDEX IF NOT EXISTS idx_shipments_unit_time ON shipments(unit_id, depart_time);

-- -----------------------------------------------------------------------------
-- lot_flows : directed genealogy edge, (from_lot) --> (to_lot).
--   Read it as "to_lot was made from / received from from_lot".
--   quantity is how much of from_lot went into to_lot. That is what makes the
--   exposure share computable: a batch that is 40% lot A is 40% exposed if A is.
-- The primary key plus idx_flows_to serve traversal in both directions.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lot_flows (
  from_lot    INT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
  to_lot      INT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
  flow_type   TEXT NOT NULL CHECK (flow_type IN ('consumed_in','packed_as','shipped_as')),
  quantity    NUMERIC(12,2) NOT NULL CHECK (quantity > 0),
  shipment_id INT REFERENCES shipments(shipment_id),
  PRIMARY KEY (from_lot, to_lot),
  CONSTRAINT no_self_flow CHECK (from_lot <> to_lot)
);
CREATE INDEX IF NOT EXISTS idx_flows_to ON lot_flows(to_lot);

-- Share of each target lot that came from each source: quantity / total inflow.
-- A view, not a column, so it can never drift from lot_flows.
CREATE OR REPLACE VIEW lot_flow_share AS
SELECT from_lot, to_lot, flow_type, shipment_id, quantity,
       (quantity / sum(quantity) OVER (PARTITION BY to_lot))::float8 AS share
FROM lot_flows;

-- -----------------------------------------------------------------------------
-- custody : lot X was inside unit Y from from_time until to_time.
-- to_time NULL = still there. This is the join table between the lot world and
-- the sensor world, and the reason the exposure query is one SQL statement.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS custody (
  custody_id  BIGSERIAL PRIMARY KEY,
  lot_id      INT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
  unit_id     INT NOT NULL REFERENCES units(unit_id),
  shipment_id INT REFERENCES shipments(shipment_id),
  from_time   TIMESTAMPTZ NOT NULL,
  to_time     TIMESTAMPTZ,
  CONSTRAINT custody_interval_ordered CHECK (to_time IS NULL OR to_time >= from_time)
);
CREATE INDEX IF NOT EXISTS idx_custody_unit_time ON custody(unit_id, from_time, to_time);
CREATE INDEX IF NOT EXISTS idx_custody_lot       ON custody(lot_id);

-- -----------------------------------------------------------------------------
-- quality_events : lab results. A FAIL on a RAW lot is the other recall trigger,
-- the one no sensor will ever see (antibiotic residue, adulteration, ...).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quality_events (
  event_id    SERIAL PRIMARY KEY,
  lot_id      INT  NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
  test_name   TEXT NOT NULL,
  result      TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
  measured    REAL,
  limit_value REAL,
  tested_at   TIMESTAMPTZ NOT NULL,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_quality_lot ON quality_events(lot_id);

-- -----------------------------------------------------------------------------
-- incidents + incident_reports : the plant's memory of what went wrong before
-- and what was done. 1:1, enforced by UNIQUE(incident_id). The embedding turns
-- "find a case like this one" into a single SQL query.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
  incident_id SERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN
              ('REEFER_FAULT','COLDROOM_FAULT','PROCESS_DEVIATION','SUPPLIER_QUALITY','SENSOR_FAULT')),
  unit_id     INT REFERENCES units(unit_id) ON DELETE CASCADE,
  lot_id      INT REFERENCES lots(lot_id)   ON DELETE CASCADE,
  occurred_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT incident_has_subject CHECK (unit_id IS NOT NULL OR lot_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_incidents_kind ON incidents(kind);

CREATE TABLE IF NOT EXISTS incident_reports (
  report_id    SERIAL PRIMARY KEY,
  incident_id  INT  NOT NULL UNIQUE REFERENCES incidents(incident_id) ON DELETE CASCADE,
  summary      TEXT NOT NULL,
  action_taken TEXT NOT NULL,
  outcome      TEXT NOT NULL,
  embedding    vector(384)
);

-- Benchmark scaffolding only: random unit vectors at a volume where exact scan
-- and HNSW diverge measurably. Separate so the demo data stays honest.
CREATE TABLE IF NOT EXISTS report_bench (
  id        SERIAL PRIMARY KEY,
  embedding vector(384) NOT NULL
);

-- -----------------------------------------------------------------------------
-- audit_logs : append-only trail written by trigger, never by the application.
-- Covers status changes on lots -- the one business column that is mutable.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
  log_id   BIGSERIAL PRIMARY KEY,
  entity   TEXT        NOT NULL,
  action   TEXT        NOT NULL CHECK (action IN ('INSERT','UPDATE','DELETE')),
  row_pk   INT,
  log_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  details  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_entity_time ON audit_logs(entity, log_time DESC);

-- Demo scaffolding, not part of the domain: remembers the temperature offset
-- scripts/refresh_demo.py added to a unit so the next run can undo it exactly.
CREATE TABLE IF NOT EXISTS demo_injection (
  unit_id   INT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
  from_time TIMESTAMPTZ NOT NULL,
  to_time   TIMESTAMPTZ NOT NULL,
  delta     REAL NOT NULL,
  PRIMARY KEY (unit_id)
);

-- NEW is unassigned in DELETE triggers and OLD in INSERT triggers, so the
-- branch on TG_OP is explicit rather than a COALESCE(NEW, OLD).
CREATE OR REPLACE FUNCTION log_lot_status_change() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('lots', 'UPDATE', NEW.lot_id,
            NEW.lot_code || ': ' || OLD.status || ' -> ' || NEW.status);
  END IF;
  IF TG_OP = 'DELETE' THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('lots', 'DELETE', OLD.lot_id, OLD.lot_code);
    RETURN OLD;
  END IF;
  IF TG_OP = 'INSERT' THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('lots', 'INSERT', NEW.lot_id, NEW.lot_code);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lot_audit ON lots;
CREATE TRIGGER trg_lot_audit
AFTER INSERT OR UPDATE OR DELETE ON lots
FOR EACH ROW EXECUTE FUNCTION log_lot_status_change();
