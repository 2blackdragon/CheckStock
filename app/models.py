"""ORM schema. All links preserve historical records on deletion."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def identity():
    return mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )


def timestamp():
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = identity()
    sku: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = timestamp()


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0"),)
    id: Mapped[str] = identity()
    name: Mapped[str] = mapped_column(String(100))
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = timestamp()


class Balance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint("on_hand >= 0"),
        CheckConstraint("reserved >= 0 AND reserved <= on_hand"),
        Index("balances_product", "product_id"),
    )
    warehouse_id: Mapped[str] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True
    )
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = timestamp()


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint("quantity > 0"),
        CheckConstraint(
            "(type='receipt' AND status='completed' "
            "AND source_warehouse_id IS NULL "
            "AND destination_warehouse_id IS NOT NULL) "
            "OR (type='transfer' AND status IN ('in_transit','received') "
            "AND source_warehouse_id IS NOT NULL "
            "AND destination_warehouse_id IS NOT NULL "
            "AND source_warehouse_id <> destination_warehouse_id) "
            "OR (type='fbs' "
            "AND status IN ('reserved','shipped','cancelled') "
            "AND source_warehouse_id IS NOT NULL "
            "AND destination_warehouse_id IS NULL) "
            "OR (type='fbo' AND status IN ('in_transit','received') "
            "AND source_warehouse_id IS NOT NULL "
            "AND destination_warehouse_id IS NULL)"
        ),
        Index("operations_status_date", "status", "created_at"),
        Index("operations_product", "product_id"),
    )
    id: Mapped[str] = identity()
    type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )
    quantity: Mapped[int] = mapped_column(Integer)
    source_warehouse_id: Mapped[str | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT")
    )
    destination_warehouse_id: Mapped[str | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = timestamp()
    updated_at: Mapped[datetime] = timestamp()


class Event(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        Index("events_date", "created_at", "id"),
        Index("events_operation", "operation_id"),
    )
    id: Mapped[str] = identity()
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(20))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    balance_changes: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = timestamp()


class RequestKey(Base):
    __tablename__ = "request_keys"
    key: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = timestamp()
