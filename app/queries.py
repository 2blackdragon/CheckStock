"""Bounded ORM read queries, independent balance/transit aggregation."""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from .models import Balance, Event, Operation, Product, Warehouse


def fields(entity):
    return {
        column.name: getattr(entity, column.name)
        for column in entity.__table__.columns
    }


def search(q):
    # Escape LIKE metacharacters so a user's % or _ is a literal search.
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(
        Product.sku.ilike(f"%{escaped}%", escape="\\"),
        Product.name.ilike(f"%{escaped}%", escape="\\"),
    )


def route_filter(wh):
    return or_(
        Operation.source_warehouse_id == wh,
        Operation.destination_warehouse_id == wh,
    )


async def stocks(db, q, wh, mode, page, limit):
    b = select(
        Balance.product_id,
        func.sum(Balance.on_hand).label("physical"),
        func.sum(Balance.reserved).label("reserved"),
    )
    if wh:
        b = b.where(Balance.warehouse_id == wh)
    b = b.group_by(Balance.product_id).subquery()
    transit = and_(
        Operation.type == "transfer", Operation.status == "in_transit"
    )
    incoming = (
        and_(transit, Operation.destination_warehouse_id == wh)
        if wh
        else transit
    )
    outgoing = (
        and_(transit, Operation.source_warehouse_id == wh) if wh else transit
    )
    fbo = and_(Operation.type == "fbo", Operation.status == "in_transit")
    fbs = and_(Operation.type == "fbs", Operation.status == "shipped")
    if wh:
        fbo = and_(fbo, Operation.source_warehouse_id == wh)
        fbs = and_(fbs, Operation.source_warehouse_id == wh)
    t = select(
        Operation.product_id,
        *[
            func.coalesce(func.sum(Operation.quantity).filter(expr), 0).label(
                name
            )
            for name, expr in [
                ("incoming", incoming),
                ("outgoing", outgoing),
                ("fbo", fbo),
                ("fbs", fbs),
            ]
        ],
    )
    t = t.group_by(Operation.product_id).subquery()
    physical, reserved = func.coalesce(b.c.physical, 0), func.coalesce(
        b.c.reserved, 0
    )
    metrics = {
        name: func.coalesce(t.c[name], 0)
        for name in ["incoming", "outgoing", "fbo", "fbs"]
    }
    stmt = select(
        Product.id,
        Product.sku,
        Product.name,
        physical.label("physical"),
        reserved.label("reserved"),
        (physical - reserved).label("available"),
        *[value.label(name) for name, value in metrics.items()],
    )
    stmt = (
        stmt.outerjoin(b, b.c.product_id == Product.id)
        .outerjoin(t, t.c.product_id == Product.id)
        .where(search(q))
    )
    if mode == "reserved":
        stmt = stmt.where(reserved > 0)
    if mode == "transit":
        stmt = stmt.where(sum(metrics.values()) > 0)
    rows = (
        (
            await db.execute(
                stmt.order_by(Product.sku)
                .limit(limit + 1)
                .offset((page - 1) * limit)
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [dict(r) for r in rows[:limit]],
        "has_next": len(rows) > limit,
    }


async def operations(db, q, wh, status, page, limit, product=None):
    source, dest = aliased(Warehouse), aliased(Warehouse)
    stmt = select(
        Operation,
        Product.sku,
        Product.name,
        source.name.label("source_name"),
        dest.name.label("destination_name"),
    )
    stmt = (
        stmt.join(Product, Product.id == Operation.product_id)
        .outerjoin(source, source.id == Operation.source_warehouse_id)
        .outerjoin(dest, dest.id == Operation.destination_warehouse_id)
        .where(search(q))
    )
    if wh:
        stmt = stmt.where(route_filter(wh))
    if product:
        stmt = stmt.where(Operation.product_id == product)
    if status == "active":
        stmt = stmt.where(Operation.status.in_(["reserved", "in_transit"]))
    rows = (
        await db.execute(
            stmt.order_by(Operation.created_at.desc(), Operation.id)
            .limit(limit + 1)
            .offset((page - 1) * limit)
        )
    ).all()
    return {
        "items": [
            dict(
                fields(row[0]),
                sku=row.sku,
                name=row.name,
                source_name=row.source_name,
                destination_name=row.destination_name,
            )
            for row in rows[:limit]
        ],
        "has_next": len(rows) > limit,
    }


async def history(db, q, wh, since, until, page, limit):
    source, dest = aliased(Warehouse), aliased(Warehouse)
    stmt = select(
        Event,
        Operation.type,
        Operation.quantity,
        Product.sku,
        Product.name,
        source.name.label("source_name"),
        dest.name.label("destination_name"),
    )
    stmt = (
        stmt.join(Operation, Operation.id == Event.operation_id)
        .join(Product, Product.id == Operation.product_id)
        .outerjoin(source, source.id == Operation.source_warehouse_id)
        .outerjoin(dest, dest.id == Operation.destination_warehouse_id)
        .where(search(q))
    )
    if wh:
        stmt = stmt.where(route_filter(wh))
    if since:
        stmt = stmt.where(Event.created_at >= since)
    if until:
        stmt = stmt.where(Event.created_at <= until)
    rows = (
        await db.execute(
            stmt.order_by(Event.created_at.desc(), Event.id)
            .limit(limit + 1)
            .offset((page - 1) * limit)
        )
    ).all()
    return {
        "items": [
            dict(
                fields(r[0]),
                type=r.type,
                quantity=r.quantity,
                sku=r.sku,
                name=r.name,
                source_name=r.source_name,
                destination_name=r.destination_name,
            )
            for r in rows[:limit]
        ],
        "has_next": len(rows) > limit,
    }
