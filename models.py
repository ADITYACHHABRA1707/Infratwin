"""
ORM mapping of the ChainTwin schema (SQLAlchemy 2.0).

The schema itself is defined in sql/*.sql (tables, CHECKs, partitions, triggers, the HNSW
index), so these models describe it rather than create it. They cover every business table
plus Users / Roles. `sensor_readings` (a partitioned time series without a primary key) and
the ledger are read through purpose-built SQL because they need features an ORM does not
express.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, REAL, String,
                        Text, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(Base):
    __tablename__ = "roles"
    role_id: Mapped[int] = mapped_column(primary_key=True)
    role_name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str] = mapped_column(Text)
    users: Mapped[list["User"]] = relationship(back_populates="role")


class Party(Base):
    __tablename__ = "parties"
    party_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    party_type: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    units: Mapped[list["Unit"]] = relationship(back_populates="party")
    lots: Mapped[list["Lot"]] = relationship(back_populates="party")


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.role_id"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("parties.party_id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[Role] = relationship(back_populates="users", lazy="joined")
    party: Mapped[Party | None] = relationship(lazy="joined")


class Product(Base):
    __tablename__ = "products"
    product_id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    temp_min: Mapped[float | None] = mapped_column(REAL)
    temp_max: Mapped[float | None] = mapped_column(REAL)
    shelf_life_days: Mapped[int] = mapped_column(Integer)
    lots: Mapped[list["Lot"]] = relationship(back_populates="product")


class Unit(Base):
    __tablename__ = "units"
    unit_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    unit_type: Mapped[str] = mapped_column(Text)
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.party_id"))
    setpoint: Mapped[float] = mapped_column(REAL)
    temp_min: Mapped[float] = mapped_column(REAL)
    temp_max: Mapped[float] = mapped_column(REAL)
    party: Mapped[Party] = relationship(back_populates="units")


class Lot(Base):
    __tablename__ = "lots"
    lot_id: Mapped[int] = mapped_column(primary_key=True)
    lot_code: Mapped[str] = mapped_column(Text, unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.product_id"))
    party_id: Mapped[int] = mapped_column(ForeignKey("parties.party_id"))
    lot_type: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="OK")
    product: Mapped[Product] = relationship(back_populates="lots", lazy="joined")
    party: Mapped[Party] = relationship(back_populates="lots", lazy="joined")


class Shipment(Base):
    __tablename__ = "shipments"
    shipment_id: Mapped[int] = mapped_column(primary_key=True)
    shipment_code: Mapped[str] = mapped_column(Text, unique=True)
    origin_party: Mapped[int] = mapped_column(ForeignKey("parties.party_id"))
    dest_party: Mapped[int] = mapped_column(ForeignKey("parties.party_id"))
    carrier_party: Mapped[int] = mapped_column(ForeignKey("parties.party_id"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.unit_id"))
    depart_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    arrive_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LotFlow(Base):
    __tablename__ = "lot_flows"
    from_lot: Mapped[int] = mapped_column(ForeignKey("lots.lot_id"), primary_key=True)
    to_lot: Mapped[int] = mapped_column(ForeignKey("lots.lot_id"), primary_key=True)
    flow_type: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.shipment_id"))


class Custody(Base):
    __tablename__ = "custody"
    custody_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.lot_id"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.unit_id"))
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.shipment_id"))
    from_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    to_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QualityEvent(Base):
    __tablename__ = "quality_events"
    event_id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("lots.lot_id"))
    test_name: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    measured: Mapped[float | None] = mapped_column(REAL)
    limit_value: Mapped[float | None] = mapped_column(REAL)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class Incident(Base):
    __tablename__ = "incidents"
    incident_id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("units.unit_id"))
    lot_id: Mapped[int | None] = mapped_column(ForeignKey("lots.lot_id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    row_pk: Mapped[int | None] = mapped_column(Integer)
    log_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    details: Mapped[str | None] = mapped_column(Text)
