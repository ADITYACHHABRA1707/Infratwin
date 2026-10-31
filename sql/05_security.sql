-- =============================================================================
-- ChainTwin  |  05_security.sql
-- Application users, roles and role-based access control, plus the reporting
-- views, the stored procedure and the triggers that guard them.
--
-- ALL people live in ONE table, `users`. What a person may do is decided by the
-- `role_id` column, never by a table per kind of person.
-- =============================================================================

CREATE TABLE IF NOT EXISTS roles (
  role_id     SERIAL PRIMARY KEY,
  role_name   TEXT NOT NULL UNIQUE CHECK (role_name IN ('ADMIN','MANAGER','AUDITOR')),
  description TEXT NOT NULL
);

INSERT INTO roles (role_name, description) VALUES
  ('ADMIN',   'Everything: manage users and master data, change lot status, run tamper tests'),
  ('MANAGER', 'Quality / operations: manage lots and shipments, place holds and releases'),
  ('AUDITOR', 'Read-only: dashboards, provenance and ledger verification')
ON CONFLICT (role_name) DO NOTHING;

-- users : the single table for every person who signs in.
-- party_id optionally ties a person to the organisation they work for.
CREATE TABLE IF NOT EXISTS users (
  user_id       SERIAL PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  full_name     TEXT NOT NULL,
  password_hash TEXT NOT NULL,                       -- bcrypt, never the password
  role_id       INT  NOT NULL REFERENCES roles(role_id),
  party_id      INT  REFERENCES parties(party_id) ON DELETE SET NULL,
  is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ,
  CONSTRAINT users_email_format  CHECK (email ~* '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
  CONSTRAINT users_name_not_blank CHECK (length(btrim(full_name)) > 0)
);
CREATE INDEX IF NOT EXISTS idx_users_role  ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_party ON users(party_id);

-- -----------------------------------------------------------------------------
-- Triggers on users
--   trg_users_touch   keeps updated_at honest
--   trg_users_audit   writes role / active-flag changes to audit_logs
--   trg_users_keep_admin  the last active ADMIN can never be demoted, disabled
--                         or deleted, so the system cannot lock itself out
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION users_touch() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_touch ON users;
CREATE TRIGGER trg_users_touch BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION users_touch();

CREATE OR REPLACE FUNCTION users_audit() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('users', 'INSERT', NEW.user_id, NEW.email);
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('users', 'DELETE', OLD.user_id, OLD.email);
    RETURN OLD;
  ELSIF NEW.role_id IS DISTINCT FROM OLD.role_id OR NEW.is_active IS DISTINCT FROM OLD.is_active THEN
    INSERT INTO audit_logs(entity, action, row_pk, details)
    VALUES ('users', 'UPDATE', NEW.user_id,
            NEW.email || ': role ' || OLD.role_id || '->' || NEW.role_id ||
            ', active ' || OLD.is_active || '->' || NEW.is_active);
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_audit ON users;
CREATE TRIGGER trg_users_audit AFTER INSERT OR UPDATE OR DELETE ON users
FOR EACH ROW EXECUTE FUNCTION users_audit();

CREATE OR REPLACE FUNCTION users_keep_admin() RETURNS trigger AS $$
DECLARE
  admin_id INT := (SELECT role_id FROM roles WHERE role_name = 'ADMIN');
  others   INT;
BEGIN
  -- Only an active admin being removed can be a problem.
  IF OLD.role_id = admin_id AND OLD.is_active THEN
    IF TG_OP = 'UPDATE' THEN
      IF NEW.role_id = admin_id AND NEW.is_active THEN
        RETURN NEW;                       -- still an active admin
      END IF;
    END IF;
    SELECT count(*) INTO others FROM users
     WHERE role_id = admin_id AND is_active AND user_id <> OLD.user_id;
    IF others = 0 THEN
      RAISE EXCEPTION 'cannot remove the last active ADMIN (%)', OLD.email
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_keep_admin ON users;
CREATE TRIGGER trg_users_keep_admin BEFORE UPDATE OR DELETE ON users
FOR EACH ROW EXECUTE FUNCTION users_keep_admin();

-- -----------------------------------------------------------------------------
-- Stored procedure: change a user's role atomically, with an actor check.
-- Only an active ADMIN may call it; the audit trigger records the change and
-- trg_users_keep_admin still protects the last administrator.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_change_user_role(p_actor INT, p_target INT, p_role TEXT)
LANGUAGE plpgsql AS $$
DECLARE
  actor_role TEXT;
  new_role   INT;
BEGIN
  SELECT r.role_name INTO actor_role
    FROM users u JOIN roles r USING (role_id) WHERE u.user_id = p_actor AND u.is_active;
  IF actor_role IS DISTINCT FROM 'ADMIN' THEN
    RAISE EXCEPTION 'only an active ADMIN may change roles' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT role_id INTO new_role FROM roles WHERE role_name = p_role;
  IF new_role IS NULL THEN
    RAISE EXCEPTION 'unknown role %', p_role USING ERRCODE = 'invalid_parameter_value';
  END IF;
  UPDATE users SET role_id = new_role WHERE user_id = p_target;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'user % does not exist', p_target USING ERRCODE = 'no_data_found';
  END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Views (read models used by the dashboard and the API)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_lot_status_summary AS
SELECT lot_type, status, count(*) AS lots, sum(quantity) AS total_quantity
FROM lots GROUP BY lot_type, status;

CREATE OR REPLACE VIEW v_party_inventory AS
SELECT p.party_id, p.name AS party, p.party_type,
       count(l.lot_id)                                  AS lots,
       COALESCE(sum(l.quantity), 0)                     AS total_quantity,
       count(*) FILTER (WHERE l.status = 'HOLD')        AS on_hold,
       count(*) FILTER (WHERE l.status = 'RECALLED')    AS recalled
FROM parties p LEFT JOIN lots l ON l.party_id = p.party_id
GROUP BY p.party_id, p.name, p.party_type;

CREATE OR REPLACE VIEW v_shipment_overview AS
SELECT s.shipment_id, s.shipment_code,
       o.name AS origin, d.name AS destination, c.name AS carrier, u.name AS vehicle,
       s.depart_time, s.arrive_time,
       round(extract(epoch FROM (s.arrive_time - s.depart_time)) / 3600.0, 1) AS hours,
       count(DISTINCT cu.lot_id) AS lots_carried
FROM shipments s
JOIN parties o ON o.party_id = s.origin_party
JOIN parties d ON d.party_id = s.dest_party
JOIN parties c ON c.party_id = s.carrier_party
JOIN units   u ON u.unit_id  = s.unit_id
LEFT JOIN custody cu ON cu.shipment_id = s.shipment_id
GROUP BY s.shipment_id, o.name, d.name, c.name, u.name;
