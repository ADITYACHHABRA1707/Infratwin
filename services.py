"""
Service layer: every read and write the dashboard and the API perform on business data,
done through the ORM. Each write names the permission it needs and checks it first, so the
same rules protect the web UI and the REST API.

    Lots       list (search / filter / paginate), create (RAW intake, anchored on the ledger),
               change status (ledger-signed). Deleted never: the ledger signed them.
    Products   full CRUD; the SKU is immutable because it is part of what lots signed.
    Parties    full CRUD; the name is immutable (it is the ledger signer); delete only if unused.
    Users      admin CRUD, role change through the stored procedure.
    Analytics  aggregates and joins built with the ORM.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Generic, Sequence, TypeVar

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

import auth
import chain
from auth import Principal
from models import AuditLog, Lot, Party, Product, Role, User

T = TypeVar("T")

LOT_TYPES = ("RAW", "BATCH", "FINISHED", "STOCK")
LOT_STATUSES = ("OK", "HOLD", "RECALLED")
PARTY_TYPES = ("Supplier", "Manufacturer", "Carrier", "Distributor", "Retailer")
PRODUCT_CATEGORIES = ("RAW", "FINISHED")


class ServiceError(Exception):
    """A request the system refuses, with a message that is safe to show to the user."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


class NotFound(ServiceError):
    def __init__(self, what: str):
        super().__init__(f"{what} not found.", status=404)


@dataclass
class Page(Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


def _paginate(session: Session, stmt, page: int, page_size: int, count_stmt=None) -> tuple[list, int]:
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 200)
    total = session.scalar(count_stmt if count_stmt is not None
                           else select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.scalars(stmt.limit(page_size).offset((page - 1) * page_size)).unique().all()
    return list(rows), int(total or 0)


def _save(session: Session, obj=None, *, delete: bool = False, on_conflict: ServiceError | None = None) -> None:
    """Flush inside a SAVEPOINT, so a rejected row leaves the session usable."""
    try:
        with session.begin_nested():
            if obj is not None:
                session.delete(obj) if delete else session.add(obj)
            session.flush()
    except IntegrityError as exc:
        raise (on_conflict or translate_db_error(exc)) from exc


def translate_db_error(exc: DBAPIError) -> ServiceError:
    """Turn a database constraint failure into a message a user can act on."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None) or ""
    sqlstate = getattr(orig, "sqlstate", "") or ""
    message = (getattr(diag, "message_primary", None) or str(orig or exc)).strip()
    if sqlstate == "23505":
        return ServiceError(f"Already exists: a record with the same unique value is present ({constraint}).",
                            status=409)
    if sqlstate == "23503":
        return ServiceError("This record is referenced by other data and cannot be removed.", status=409)
    if sqlstate == "23514":
        return ServiceError(f"Rejected by a database rule: {message}", status=422)
    if sqlstate == "23502":
        return ServiceError("A required value is missing.", status=422)
    if sqlstate == "42501":
        return ServiceError(message, status=403)
    return ServiceError("The database rejected the request.", status=500)


# ===========================================================================
# Lots
# ===========================================================================
def list_lots(session: Session, *, search: str = "", lot_type: str | None = None,
              status: str | None = None, party_id: int | None = None,
              page: int = 1, page_size: int = 20) -> Page[Lot]:
    stmt = select(Lot).join(Lot.product).join(Lot.party)
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Lot.lot_code.ilike(like), Product.name.ilike(like), Party.name.ilike(like)))
    if lot_type:
        stmt = stmt.where(Lot.lot_type == lot_type)
    if status:
        stmt = stmt.where(Lot.status == status)
    if party_id:
        stmt = stmt.where(Lot.party_id == party_id)
    stmt = stmt.order_by(Lot.created_at.desc(), Lot.lot_id.desc())
    rows, total = _paginate(session, stmt, page, page_size)
    return Page(rows, total, page, page_size)


def get_lot(session: Session, lot_id: int) -> Lot:
    lot = session.get(Lot, lot_id)
    if lot is None:
        raise NotFound("Lot")
    return lot


def _next_raw_code(session: Session, prefix: str, day: datetime) -> str:
    stem = f"{prefix}-{day:%y%m%d}-"
    last = session.scalar(select(func.max(Lot.lot_code)).where(Lot.lot_code.like(stem + "%")))
    n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{stem}{n:02d}"


def _raw_cursor(session: Session):
    """The psycopg cursor inside the ORM session's own transaction, for the ledger code."""
    return session.connection().connection.dbapi_connection.cursor()


def create_raw_lot(session: Session, who: Principal, *, product_id: int, party_id: int,
                   quantity: str | float | Decimal, shelf_life_days: int | None = None) -> Lot:
    """Register an incoming RAW lot and anchor it on the ledger in the same transaction."""
    who.require("lots:write")
    try:
        qty = Decimal(str(quantity)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ServiceError("Quantity must be a number.", status=422) from exc
    if qty <= 0 or qty > Decimal("10000000"):
        raise ServiceError("Quantity must be greater than 0 and at most 10,000,000.", status=422)
    product = session.get(Product, product_id)
    if product is None:
        raise NotFound("Product")
    if product.category != "RAW":
        raise ServiceError("Only RAW products can be received as raw lots.", status=422)
    party = session.get(Party, party_id)
    if party is None:
        raise NotFound("Supplier")
    if party.party_type != "Supplier":
        raise ServiceError("Raw lots can only be received from a Supplier.", status=422)

    now = datetime.now(timezone.utc)
    days = shelf_life_days or product.shelf_life_days
    words = re.findall(r"[A-Za-z]+", product.name.upper())
    prefix = (words[0][0] + words[-1][0]) if words else "RW"       # "Raw chilled milk" -> RM
    try:
        lot = Lot(lot_code=_next_raw_code(session, prefix, now), product_id=product.product_id,
                  party_id=party.party_id, lot_type="RAW", quantity=qty, created_at=now,
                  expires_at=now + timedelta(days=days), status="OK")
        session.add(lot)
        session.flush()
        cur = _raw_cursor(session)
        payload, signers = chain.lot_payloads(cur)[lot.lot_code]
        tx = chain.make_tx("LOT_REGISTERED", lot.lot_code, int(time.time() * 1000), payload, signers)
        chain.append_txs(cur, [tx], live=True)
    except IntegrityError as exc:
        raise translate_db_error(exc) from exc
    session.refresh(lot)
    return lot


def set_lot_status(session: Session, who: Principal, lot_ids: Sequence[int], new_status: str,
                   reason: str) -> int:
    """HOLD / release lots. The ORM update and the signed ledger block commit together."""
    who.require("status:change")
    if new_status not in LOT_STATUSES:
        raise ServiceError("Unknown status.", status=422)
    if not reason.strip():
        raise ServiceError("A reason is required.", status=422)
    lots = session.scalars(select(Lot).where(Lot.lot_id.in_(list(lot_ids)))).unique().all()
    if len(lots) != len(set(lot_ids)):
        raise NotFound("One or more lots")
    now_ms = int(time.time() * 1000)
    txs, changed = [], 0
    for lot in lots:
        old = lot.status
        if old == new_status or (old == "RECALLED" and new_status != "RECALLED"):
            continue            # RECALLED is terminal
        lot.status = new_status
        payload, signers = chain.status_payload(lot.lot_code, old, new_status,
                                                f"{reason.strip()} (by {who.email})", chain.PLANT_NAME)
        txs.append(chain.make_tx("STATUS_CHANGE", lot.lot_code, now_ms, payload, signers))
        changed += 1
    try:
        session.flush()
        if txs:
            chain.append_txs(_raw_cursor(session), txs, live=True)
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc
    return changed


# ===========================================================================
# Products
# ===========================================================================
def list_products(session: Session, *, search: str = "", category: str | None = None,
                  page: int = 1, page_size: int = 20) -> Page[Product]:
    stmt = select(Product)
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
    if category:
        stmt = stmt.where(Product.category == category)
    rows, total = _paginate(session, stmt.order_by(Product.product_id), page, page_size)
    return Page(rows, total, page, page_size)


def _check_product(sku: str, name: str, category: str, temp_min, temp_max, shelf_life_days) -> None:
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{1,23}", sku):
        raise ServiceError("SKU must be 2-24 characters: capital letters, digits and hyphens.", status=422)
    if not name.strip():
        raise ServiceError("Name is required.", status=422)
    if category not in PRODUCT_CATEGORIES:
        raise ServiceError("Category must be RAW or FINISHED.", status=422)
    if temp_min is not None and temp_max is not None and not temp_min < temp_max:
        raise ServiceError("Minimum temperature must be below the maximum.", status=422)
    if int(shelf_life_days) <= 0:
        raise ServiceError("Shelf life must be at least 1 day.", status=422)


def create_product(session: Session, who: Principal, *, sku: str, name: str, category: str,
                   temp_min: float | None, temp_max: float | None, shelf_life_days: int) -> Product:
    who.require("registry:write")
    sku = sku.strip().upper()
    _check_product(sku, name, category, temp_min, temp_max, shelf_life_days)
    p = Product(sku=sku, name=name.strip(), category=category, temp_min=temp_min, temp_max=temp_max,
                shelf_life_days=int(shelf_life_days))
    _save(session, p)
    return p


def update_product(session: Session, who: Principal, product_id: int, *, name: str,
                   temp_min: float | None, temp_max: float | None, shelf_life_days: int) -> Product:
    who.require("registry:write")
    p = session.get(Product, product_id)
    if p is None:
        raise NotFound("Product")
    _check_product(p.sku, name, p.category, temp_min, temp_max, shelf_life_days)
    p.name, p.temp_min, p.temp_max, p.shelf_life_days = name.strip(), temp_min, temp_max, int(shelf_life_days)
    _save(session)
    return p


def delete_product(session: Session, who: Principal, product_id: int) -> None:
    who.require("registry:write")
    p = session.get(Product, product_id)
    if p is None:
        raise NotFound("Product")
    if session.scalar(select(func.count()).select_from(Lot).where(Lot.product_id == product_id)):
        raise ServiceError("This product has lots recorded against it and cannot be deleted.", status=409)
    _save(session, p, delete=True)


# ===========================================================================
# Parties
# ===========================================================================
def list_parties(session: Session, *, search: str = "", party_type: str | None = None,
                 page: int = 1, page_size: int = 20) -> Page[Party]:
    stmt = select(Party)
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Party.name.ilike(like), Party.city.ilike(like)))
    if party_type:
        stmt = stmt.where(Party.party_type == party_type)
    rows, total = _paginate(session, stmt.order_by(Party.party_id), page, page_size)
    return Page(rows, total, page, page_size)


def create_party(session: Session, who: Principal, *, name: str, party_type: str, city: str) -> Party:
    who.require("registry:write")
    if not name.strip():
        raise ServiceError("Name is required.", status=422)
    if party_type not in PARTY_TYPES:
        raise ServiceError("Unknown party type.", status=422)
    p = Party(name=name.strip(), party_type=party_type, city=city.strip() or None)
    _save(session, p)
    return p


def update_party_city(session: Session, who: Principal, party_id: int, city: str) -> Party:
    who.require("registry:write")
    p = session.get(Party, party_id)
    if p is None:
        raise NotFound("Party")
    p.city = city.strip() or None
    session.flush()
    return p


def delete_party(session: Session, who: Principal, party_id: int) -> None:
    who.require("registry:write")
    p = session.get(Party, party_id)
    if p is None:
        raise NotFound("Party")
    _save(session, p, delete=True,
          on_conflict=ServiceError("This party owns lots, units or shipments and cannot be deleted.", status=409))


# ===========================================================================
# Users (single table, role column)
# ===========================================================================
def list_users(session: Session, who: Principal, *, search: str = "", role: str | None = None,
               page: int = 1, page_size: int = 20) -> Page[User]:
    who.require("users:manage")
    stmt = select(User).join(User.role)
    if search.strip():
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
    if role:
        stmt = stmt.where(Role.role_name == role)
    rows, total = _paginate(session, stmt.order_by(User.user_id), page, page_size)
    return Page(rows, total, page, page_size)


def add_user(session: Session, who: Principal, *, email: str, full_name: str, password: str,
             role_name: str, party_id: int | None = None) -> User:
    who.require("users:manage")
    try:
        return auth.create_user(session, email=email, full_name=full_name, password=password,
                                role_name=role_name, party_id=party_id)
    except ValueError as exc:
        raise ServiceError(str(exc), status=422) from exc
    except IntegrityError as exc:
        raise translate_db_error(exc) from exc


def change_user_role(session: Session, who: Principal, user_id: int, role_name: str) -> None:
    """Calls the stored procedure, which re-checks the actor and the last-admin rule."""
    who.require("users:manage")
    try:
        session.execute(text("CALL sp_change_user_role(:actor, :target, :role)"),
                        {"actor": who.user_id, "target": user_id, "role": role_name})
        session.flush()
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc


def set_user_active(session: Session, who: Principal, user_id: int, active: bool) -> None:
    who.require("users:manage")
    u = session.get(User, user_id)
    if u is None:
        raise NotFound("User")
    u.is_active = active
    try:
        session.flush()
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc


# ===========================================================================
# Analytics (ORM aggregates and joins)
# ===========================================================================
def lot_counts_by_status(session: Session) -> list[tuple[str, str, int, Decimal]]:
    stmt = (select(Lot.lot_type, Lot.status, func.count(), func.sum(Lot.quantity))
            .group_by(Lot.lot_type, Lot.status).order_by(Lot.lot_type, Lot.status))
    return [tuple(r) for r in session.execute(stmt)]


def inventory_by_party(session: Session, limit: int = 20):
    """LEFT JOIN + conditional aggregation: lots, quantity and holds per organisation."""
    stmt = (select(Party.name, Party.party_type, func.count(Lot.lot_id).label("lots"),
                   func.coalesce(func.sum(Lot.quantity), 0).label("quantity"),
                   func.count(case((Lot.status == "HOLD", 1))).label("on_hold"))
            .outerjoin(Lot, Lot.party_id == Party.party_id)
            .group_by(Party.party_id).order_by(func.count(Lot.lot_id).desc()).limit(limit))
    return [tuple(r) for r in session.execute(stmt)]


def recent_audit(session: Session, limit: int = 15) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.log_id.desc()).limit(limit)))


# ===========================================================================
# Ledger housekeeping
# ===========================================================================
def save_ledger_checkpoint() -> None:
    """Record the chain head outside the database. Call after a ledger-writing commit."""
    from db import session_scope
    with session_scope() as s:
        chain.save_checkpoint(_raw_cursor(s))


def verify_ledger(session: Session) -> dict:
    return chain.verify_chain(_raw_cursor(session))
