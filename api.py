"""
ChainTwin REST API (FastAPI). Role-based API access: every route except /health and
/auth/login needs a bearer token, and write routes need a specific permission.

    uvicorn api:app --port 8000        interactive docs at /docs
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

import auth
import services
from auth import AuthError, Forbidden, Principal
from db import SessionLocal
from services import ServiceError

app = FastAPI(title="ChainTwin API", version="1.0",
              description="Supply-chain digital twin: lots, registry, users and ledger status.")
oauth2 = OAuth2PasswordBearer(tokenUrl="auth/login")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
def get_session() -> Iterator[Session]:
    """One transaction per request: commit when the handler returns, roll back on any error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def current_principal(token: str = Depends(oauth2), session: Session = Depends(get_session)) -> Principal:
    return auth.principal_from_token(session, token)


def needs(permission: str):
    def dep(who: Principal = Depends(current_principal)) -> Principal:
        who.require(permission)
        return who
    return dep


@app.exception_handler(AuthError)
async def _auth_error(_: Request, exc: AuthError):
    return JSONResponse({"detail": str(exc)}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(Forbidden)
async def _forbidden(_: Request, exc: Forbidden):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------------------
# Schemas (request validation)
# ---------------------------------------------------------------------------
class LotOut(BaseModel):
    lot_id: int
    lot_code: str
    lot_type: str
    product: str
    holder: str
    quantity: float
    status: str


def lot_out(l) -> LotOut:
    return LotOut(lot_id=l.lot_id, lot_code=l.lot_code, lot_type=l.lot_type, product=l.product.name,
                  holder=l.party.name, quantity=float(l.quantity), status=l.status)


class RawLotIn(BaseModel):
    product_id: int = Field(gt=0)
    party_id: int = Field(gt=0)
    quantity: float = Field(gt=0, le=10_000_000)


class StatusIn(BaseModel):
    lot_ids: list[int] = Field(min_length=1, max_length=500)
    status: str = Field(pattern="^(OK|HOLD|RECALLED)$")
    reason: str = Field(min_length=3, max_length=300)


class ProductIn(BaseModel):
    sku: str = Field(min_length=2, max_length=24)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(pattern="^(RAW|FINISHED)$")
    temp_min: float | None = None
    temp_max: float | None = None
    shelf_life_days: int = Field(gt=0, le=3650)


class PartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    party_type: str
    city: str = ""


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    role: str = Field(pattern="^(ADMIN|MANAGER|AUDITOR)$")


class RoleIn(BaseModel):
    role: str = Field(pattern="^(ADMIN|MANAGER|AUDITOR)$")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.post("/auth/login", tags=["auth"])
def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    user = auth.authenticate(session, form.username, form.password)
    return {"access_token": auth.create_token(user), "token_type": "bearer"}


@app.get("/auth/me", tags=["auth"])
def me(who: Principal = Depends(current_principal)):
    """Retrieval of the authenticated user's information."""
    return {"user_id": who.user_id, "email": who.email, "full_name": who.full_name, "role": who.role,
            "permissions": sorted(auth.PERMISSIONS[who.role]), "pages": auth.visible_pages(who)}


@app.post("/auth/logout", status_code=204, tags=["auth"])
def logout(_: Principal = Depends(current_principal)):
    """Tokens are stateless and short-lived; the client discards its token on logout."""
    return None


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------
@app.get("/lots", tags=["lots"])
def lots(search: str = "", lot_type: str | None = None, status: str | None = None,
         page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
         _: Principal = Depends(needs("view")), session: Session = Depends(get_session)):
    p = services.list_lots(session, search=search, lot_type=lot_type, status=status,
                           page=page, page_size=page_size)
    return {"total": p.total, "page": p.page, "pages": p.pages, "items": [lot_out(l) for l in p.items]}


@app.get("/lots/{lot_id}", response_model=LotOut, tags=["lots"])
def lot(lot_id: int, _: Principal = Depends(needs("view")), session: Session = Depends(get_session)):
    return lot_out(services.get_lot(session, lot_id))


@app.post("/lots/raw", response_model=LotOut, status_code=201, tags=["lots"])
def receive_raw_lot(body: RawLotIn, who: Principal = Depends(needs("lots:write")),
                    session: Session = Depends(get_session)):
    created = services.create_raw_lot(session, who, product_id=body.product_id, party_id=body.party_id,
                                      quantity=body.quantity)
    session.commit()
    services.save_ledger_checkpoint()
    return lot_out(created)


@app.post("/lots/status", tags=["lots"])
def change_status(body: StatusIn, who: Principal = Depends(needs("status:change")),
                  session: Session = Depends(get_session)):
    n = services.set_lot_status(session, who, body.lot_ids, body.status, body.reason)
    session.commit()
    if n:
        services.save_ledger_checkpoint()
    return {"changed": n}


# ---------------------------------------------------------------------------
# Registry: products and parties
# ---------------------------------------------------------------------------
@app.get("/products", tags=["registry"])
def products(search: str = "", category: str | None = None, page: int = Query(1, ge=1),
             page_size: int = Query(20, ge=1, le=200), _: Principal = Depends(needs("view")),
             session: Session = Depends(get_session)):
    p = services.list_products(session, search=search, category=category, page=page, page_size=page_size)
    return {"total": p.total, "pages": p.pages,
            "items": [{"product_id": x.product_id, "sku": x.sku, "name": x.name, "category": x.category,
                       "temp_min": x.temp_min, "temp_max": x.temp_max,
                       "shelf_life_days": x.shelf_life_days} for x in p.items]}


@app.post("/products", status_code=201, tags=["registry"])
def add_product(body: ProductIn, who: Principal = Depends(needs("registry:write")),
                session: Session = Depends(get_session)):
    x = services.create_product(session, who, **body.model_dump())
    return {"product_id": x.product_id, "sku": x.sku}


@app.delete("/products/{product_id}", status_code=204, tags=["registry"])
def remove_product(product_id: int, who: Principal = Depends(needs("registry:write")),
                   session: Session = Depends(get_session)):
    services.delete_product(session, who, product_id)


@app.get("/parties", tags=["registry"])
def parties(search: str = "", party_type: str | None = None, page: int = Query(1, ge=1),
            page_size: int = Query(20, ge=1, le=200), _: Principal = Depends(needs("view")),
            session: Session = Depends(get_session)):
    p = services.list_parties(session, search=search, party_type=party_type, page=page, page_size=page_size)
    return {"total": p.total, "pages": p.pages,
            "items": [{"party_id": x.party_id, "name": x.name, "party_type": x.party_type, "city": x.city}
                      for x in p.items]}


@app.post("/parties", status_code=201, tags=["registry"])
def add_party(body: PartyIn, who: Principal = Depends(needs("registry:write")),
              session: Session = Depends(get_session)):
    x = services.create_party(session, who, **body.model_dump())
    return {"party_id": x.party_id, "name": x.name}


# ---------------------------------------------------------------------------
# Users (ADMIN only)
# ---------------------------------------------------------------------------
@app.get("/users", tags=["users"])
def users(search: str = "", role: str | None = None, page: int = Query(1, ge=1),
          page_size: int = Query(20, ge=1, le=200), who: Principal = Depends(needs("users:manage")),
          session: Session = Depends(get_session)):
    p = services.list_users(session, who, search=search, role=role, page=page, page_size=page_size)
    return {"total": p.total, "pages": p.pages,
            "items": [{"user_id": u.user_id, "email": u.email, "full_name": u.full_name,
                       "role": u.role.role_name, "is_active": u.is_active} for u in p.items]}


@app.post("/users", status_code=201, tags=["users"])
def add_user(body: UserIn, who: Principal = Depends(needs("users:manage")),
             session: Session = Depends(get_session)):
    u = services.add_user(session, who, email=body.email, full_name=body.full_name,
                          password=body.password, role_name=body.role)
    return {"user_id": u.user_id, "email": u.email, "role": u.role.role_name}


@app.put("/users/{user_id}/role", tags=["users"])
def change_role(user_id: int, body: RoleIn, who: Principal = Depends(needs("users:manage")),
                session: Session = Depends(get_session)):
    services.change_user_role(session, who, user_id, body.role)
    return {"user_id": user_id, "role": body.role}


# ---------------------------------------------------------------------------
# Analytics and ledger
# ---------------------------------------------------------------------------
@app.get("/stats/lots", tags=["analytics"])
def lot_stats(_: Principal = Depends(needs("view")), session: Session = Depends(get_session)):
    return [{"lot_type": t, "status": s, "lots": n, "quantity": float(q or 0)}
            for t, s, n, q in services.lot_counts_by_status(session)]


@app.get("/ledger/verify", tags=["ledger"])
def ledger_verify(_: Principal = Depends(needs("view")), session: Session = Depends(get_session)):
    r = services.verify_ledger(session)
    return {"ok": r["ok"], "blocks": r["blocks"], "transactions": r["txs"], "problems": r["problems"][:20]}
