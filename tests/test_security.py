"""
Authentication, RBAC and ORM service tests.

Pure tests (hashing, tokens, permission map) need no database. The rest need the seeded
database and are skipped when it is unreachable. Every database test runs inside a
transaction that is rolled back, so the data is left untouched.

    python -m pytest tests/test_security.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings  # noqa: E402

try:
    import auth  # noqa: E402
    import services  # noqa: E402
    from auth import AuthError, Forbidden, Principal  # noqa: E402
    from db import SessionLocal  # noqa: E402
    from models import Lot, Product  # noqa: E402
    from sqlalchemy import select, text  # noqa: E402
except Exception as exc:                      # pragma: no cover
    pytest.skip(f"app modules unavailable: {exc}", allow_module_level=True)


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------
class TestPasswords:
    def test_hash_is_not_the_password_and_verifies(self):
        h = auth.hash_password("Str0ngPass")
        assert h != "Str0ngPass" and h.startswith("$2")          # bcrypt
        assert auth.verify_password("Str0ngPass", h)
        assert not auth.verify_password("wrong", h)

    def test_same_password_hashes_differently(self):
        assert auth.hash_password("Str0ngPass") != auth.hash_password("Str0ngPass")

    @pytest.mark.parametrize("pw", ["short1A", "alllowercase1", "ALLUPPERCASE1", "NoDigitsHere"])
    def test_weak_passwords_rejected(self, pw):
        with pytest.raises(ValueError):
            auth.validate_password_strength(pw)

    def test_garbage_hash_does_not_crash(self):
        assert not auth.verify_password("x", "not-a-hash")


class TestPermissions:
    def test_roles_are_nested_least_privilege(self):
        a, m, v = (auth.PERMISSIONS[r] for r in ("ADMIN", "MANAGER", "AUDITOR"))
        assert v < m < a

    def test_only_admin_manages_users_and_runs_attacks(self):
        for perm in ("users:manage", "tamper:run"):
            assert perm in auth.PERMISSIONS["ADMIN"]
            assert perm not in auth.PERMISSIONS["MANAGER"]
            assert perm not in auth.PERMISSIONS["AUDITOR"]

    def test_menus_differ_by_role(self):
        def mk(role):
            return Principal(1, "x@y.z", "X", role)
        assert "Users" in auth.visible_pages(mk("ADMIN"))
        assert "Users" not in auth.visible_pages(mk("MANAGER"))
        assert "Users" not in auth.visible_pages(mk("AUDITOR"))
        assert "Registry" in auth.visible_pages(mk("AUDITOR"))

    def test_require_raises_forbidden(self):
        with pytest.raises(Forbidden):
            Principal(1, "a@b.c", "A", "AUDITOR").require("status:change")


class _FakeUser:
    user_id = 7
    email = "t@example.com"

    class role:
        role_name = "MANAGER"


class TestTokens:
    def test_round_trip_carries_role(self):
        claims = auth.decode_token(auth.create_token(_FakeUser()))
        assert claims["sub"] == "7" and claims["role"] == "MANAGER"

    def test_tampered_token_rejected(self):
        t = auth.create_token(_FakeUser())
        with pytest.raises(AuthError):
            auth.decode_token(t[:-3] + ("AAA" if not t.endswith("AAA") else "BBB"))

    def test_token_signed_with_another_key_rejected(self):
        forged = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256")
        with pytest.raises(AuthError):
            auth.decode_token(forged)

    def test_expired_token_rejected(self):
        old = jwt.encode({"sub": "1", "exp": int(time.time()) - 5}, settings.jwt_secret(), algorithm="HS256")
        with pytest.raises(AuthError):
            auth.decode_token(old)

    def test_alg_none_rejected(self):
        t = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, None, algorithm="none")
        with pytest.raises(AuthError):
            auth.decode_token(t)


# ---------------------------------------------------------------------------
# Database (rolled back)
# ---------------------------------------------------------------------------
@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.execute(text("SELECT 1 FROM users LIMIT 1"))
        s.scalar(select(Lot.lot_id).limit(1))
    except Exception as exc:
        s.close()
        pytest.skip(f"database not ready: {exc}")
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def principal(role: str) -> Principal:
    return Principal(0, f"{role.lower()}@test", role.title(), role)


class TestUsersTable:
    def test_create_and_login(self, db):
        auth.create_user(db, email="Tester@Example.com", full_name="T", password="Str0ngPass", role_name="AUDITOR")
        u = auth.authenticate(db, "tester@example.com", "Str0ngPass")
        assert u.role.role_name == "AUDITOR" and u.last_login_at is not None

    def test_wrong_password_and_unknown_user_give_same_message(self, db):
        auth.create_user(db, email="a@example.com", full_name="A", password="Str0ngPass", role_name="AUDITOR")
        msgs = set()
        for email, pw in (("a@example.com", "nope"), ("ghost@example.com", "Str0ngPass")):
            with pytest.raises(AuthError) as e:
                auth.authenticate(db, email, pw)
            msgs.add(str(e.value))
        assert len(msgs) == 1

    def test_disabled_account_cannot_log_in(self, db):
        u = auth.create_user(db, email="d@example.com", full_name="D", password="Str0ngPass", role_name="AUDITOR")
        u.is_active = False
        db.flush()
        with pytest.raises(AuthError):
            auth.authenticate(db, "d@example.com", "Str0ngPass")

    def test_duplicate_email_rejected(self, db):
        auth.create_user(db, email="dup@example.com", full_name="D", password="Str0ngPass", role_name="AUDITOR")
        with pytest.raises(ValueError):
            auth.create_user(db, email="DUP@example.com", full_name="D2", password="Str0ngPass", role_name="AUDITOR")

    def test_password_is_stored_hashed(self, db):
        u = auth.create_user(db, email="h@example.com", full_name="H", password="Str0ngPass", role_name="AUDITOR")
        raw = db.execute(text("SELECT password_hash FROM users WHERE user_id = :i"), {"i": u.user_id}).scalar()
        assert raw != "Str0ngPass" and raw.startswith("$2")

    def test_db_rejects_malformed_email(self, db):
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            db.execute(text("INSERT INTO users(email, full_name, password_hash, role_id) "
                            "SELECT 'not-an-email', 'x', 'h', role_id FROM roles LIMIT 1"))


class TestStoredProcedureAndTriggers:
    def _admin(self, db, email="adm@example.com"):
        return auth.create_user(db, email=email, full_name="Adm", password="Str0ngPass", role_name="ADMIN")

    def test_procedure_rechecks_the_actor_inside_the_database(self, db):
        m = auth.create_user(db, email="m@example.com", full_name="M", password="Str0ngPass", role_name="MANAGER")
        t = auth.create_user(db, email="t@example.com", full_name="T", password="Str0ngPass", role_name="AUDITOR")
        # The caller claims to be ADMIN; the procedure looks the real role up and refuses.
        with pytest.raises(services.ServiceError) as e:
            services.change_user_role(db, Principal(m.user_id, m.email, "M", "ADMIN"), t.user_id, "MANAGER")
        assert e.value.status == 403

    def test_admin_changes_role_via_procedure_and_it_is_audited(self, db):
        a = self._admin(db)
        t = auth.create_user(db, email="t@example.com", full_name="T", password="Str0ngPass", role_name="AUDITOR")
        services.change_user_role(db, Principal(a.user_id, a.email, "A", "ADMIN"), t.user_id, "MANAGER")
        db.refresh(t)
        assert t.role.role_name == "MANAGER"
        logged = db.execute(text("SELECT count(*) FROM audit_logs WHERE entity='users' AND row_pk=:i AND action='UPDATE'"),
                            {"i": t.user_id}).scalar()
        assert logged == 1

    def test_last_admin_cannot_be_demoted(self, db):
        a = self._admin(db)
        db.execute(text("UPDATE users SET is_active = FALSE WHERE user_id <> :a AND role_id = "
                        "(SELECT role_id FROM roles WHERE role_name='ADMIN')"), {"a": a.user_id})
        with pytest.raises(services.ServiceError):
            services.change_user_role(db, Principal(a.user_id, a.email, "A", "ADMIN"), a.user_id, "AUDITOR")


class TestLotServices:
    def test_pagination_and_filter(self, db):
        p1 = services.list_lots(db, page=1, page_size=10)
        p2 = services.list_lots(db, page=2, page_size=10)
        assert len(p1.items) == 10 and p1.total > 100 and p1.pages > 5
        assert {l.lot_id for l in p1.items}.isdisjoint({l.lot_id for l in p2.items})
        only = services.list_lots(db, lot_type="RAW", page_size=50)
        assert only.total and all(l.lot_type == "RAW" for l in only.items)

    def test_search_is_parameterised_not_injectable(self, db):
        hostile = "'; DROP TABLE lots; --"
        assert services.list_lots(db, search=hostile).total == 0
        assert db.scalar(select(Lot.lot_id).limit(1)) is not None      # table still there

    def test_auditor_cannot_write(self, db):
        with pytest.raises(Forbidden):
            services.create_product(db, principal("AUDITOR"), sku="X-1", name="n", category="RAW",
                                    temp_min=0, temp_max=4, shelf_life_days=3)
        with pytest.raises(Forbidden):
            services.set_lot_status(db, principal("AUDITOR"), [1], "HOLD", "r")

    def test_product_crud_and_validation(self, db):
        mgr = principal("MANAGER")
        with pytest.raises(services.ServiceError):
            services.create_product(db, mgr, sku="bad sku!", name="n", category="RAW",
                                    temp_min=0, temp_max=4, shelf_life_days=3)
        with pytest.raises(services.ServiceError):
            services.create_product(db, mgr, sku="OK-1", name="n", category="RAW",
                                    temp_min=9, temp_max=4, shelf_life_days=3)
        p = services.create_product(db, mgr, sku="ok-1", name="Test", category="RAW",
                                    temp_min=0, temp_max=4, shelf_life_days=3)
        assert p.sku == "OK-1"
        with pytest.raises(services.ServiceError) as e:
            services.create_product(db, mgr, sku="OK-1", name="Again", category="RAW",
                                    temp_min=0, temp_max=4, shelf_life_days=3)
        assert e.value.status == 409
        services.update_product(db, mgr, p.product_id, name="Renamed", temp_min=1, temp_max=5, shelf_life_days=4)
        assert db.get(Product, p.product_id).name == "Renamed"
        services.delete_product(db, mgr, p.product_id)
        assert db.get(Product, p.product_id) is None

    def test_cannot_delete_product_with_lots(self, db):
        in_use = db.scalar(select(Lot.product_id).limit(1))
        with pytest.raises(services.ServiceError) as e:
            services.delete_product(db, principal("ADMIN"), in_use)
        assert e.value.status == 409

    def test_aggregates_via_orm(self, db):
        rows = services.lot_counts_by_status(db)
        assert sum(r[2] for r in rows) == db.execute(text("SELECT count(*) FROM lots")).scalar()
        inv = services.inventory_by_party(db, limit=5)
        assert len(inv) == 5 and inv[0][2] >= inv[-1][2]

    def test_view_matches_orm_aggregate(self, db):
        view = db.execute(text("SELECT sum(lots) FROM v_lot_status_summary")).scalar()
        assert view == db.execute(text("SELECT count(*) FROM lots")).scalar()
