-- =============================================================================
-- ChainTwin  |  04_ledger.sql
-- The blockchain: an append-only chain of blocks, each holding signed
-- transactions. Business data stays in the relational tables; the ledger holds
-- hashes and signatures of it, so any later edit to either side is detectable.
--
--   block_hash = SHA-256(height | prev_hash | merkle_root | nonce | ts_ms)
--   merkle_root = Merkle tree over the block's tx_hash values
--   tx_hash    = SHA-256(canonical JSON of type, subject, ts, payload_hash)
--   signatures = HMAC-SHA256(party key, tx_hash), one per required signer
--
-- All hashing is done in chain.py, not in SQL, so there is exactly one
-- definition of the encoding and the verifier cannot disagree with the writer.
-- =============================================================================

CREATE TABLE IF NOT EXISTS blocks (
  height      INT    PRIMARY KEY,
  prev_hash   TEXT   NOT NULL,
  merkle_root TEXT   NOT NULL,
  nonce       BIGINT NOT NULL,
  difficulty  INT    NOT NULL,
  ts_ms       BIGINT NOT NULL,
  block_hash  TEXT   NOT NULL UNIQUE,
  tx_count    INT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_tx (
  tx_id        BIGSERIAL PRIMARY KEY,
  height       INT    NOT NULL REFERENCES blocks(height),
  tx_index     INT    NOT NULL,
  tx_type      TEXT   NOT NULL CHECK (tx_type IN
               ('LOT_REGISTERED','CUSTODY_TRANSFER','TELEMETRY_DIGEST',
                'QUALITY_RESULT','STATUS_CHANGE')),
  subject      TEXT   NOT NULL,             -- lot code, shipment code or unit window
  ts_ms        BIGINT NOT NULL,
  payload      TEXT   NOT NULL,             -- canonical JSON, exactly what was hashed
  payload_hash TEXT   NOT NULL,
  tx_hash      TEXT   NOT NULL UNIQUE,
  signatures   TEXT   NOT NULL,             -- canonical JSON {party name: hex hmac}
  UNIQUE (height, tx_index)
);
CREATE INDEX IF NOT EXISTS idx_tx_subject ON ledger_tx(subject);
CREATE INDEX IF NOT EXISTS idx_tx_type    ON ledger_tx(tx_type);

-- telemetry_windows : which slices of sensor history have been anchored, so a
-- verifier knows what to recompute. Written together with the TELEMETRY_DIGEST.
CREATE TABLE IF NOT EXISTS telemetry_windows (
  window_id    SERIAL PRIMARY KEY,
  unit_id      INT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
  window_start TIMESTAMPTZ NOT NULL,
  window_end   TIMESTAMPTZ NOT NULL,           -- exclusive
  shipment_id  INT REFERENCES shipments(shipment_id),
  tx_hash      TEXT NOT NULL REFERENCES ledger_tx(tx_hash),
  UNIQUE (unit_id, window_start, window_end)
);
CREATE INDEX IF NOT EXISTS idx_windows_unit ON telemetry_windows(unit_id, window_end DESC);

-- -----------------------------------------------------------------------------
-- Append-only guard. This is a convenience, NOT the security boundary: a
-- database owner can disable a trigger. The security comes from the hash chain
-- and the party signatures, which make the edit detectable afterwards.
-- scripts/tamper_demo.py disables this trigger on purpose to prove that.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ledger is append-only: % on % rejected', TG_OP, TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_blocks_immutable ON blocks;
CREATE TRIGGER trg_blocks_immutable
BEFORE UPDATE OR DELETE ON blocks
FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

DROP TRIGGER IF EXISTS trg_tx_immutable ON ledger_tx;
CREATE TRIGGER trg_tx_immutable
BEFORE UPDATE OR DELETE ON ledger_tx
FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
