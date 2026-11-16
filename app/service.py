"""Stock commands with transaction-scoped, narrowly targeted locks.

Order: request key -> existing operation -> sorted warehouses -> balance.
Warehouse readers share locks; archival/rename take an exclusive row lock.
Each command changes at most one balance. Reads need no explicit locks.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .db import connect
from .models import Balance, Event, Operation, Product, RequestKey, Warehouse


def fail(message):
    raise HTTPException(409, message)


async def lock_request(db, key):
    # A stable per-key lock also works before a RequestKey row exists.
    # A rare 64-bit hash collision only causes additional waiting.
    lock_id = int.from_bytes(
        hashlib.blake2b(
            f"checkstock/request/{key}".encode(), digest_size=8
        ).digest(),
        signed=True,
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))


async def mutate(key, payload, action):
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    async with connect() as db:
        await lock_request(db, key)
        previous = await db.get(RequestKey, str(key))
        if previous:
            if previous.request_hash != digest:
                fail("Этот ключ запроса уже использован для другого действия.")
            return previous.response_body
        result = await action(db)
        result = json.loads(json.dumps(result, default=str))
        db.add(
            RequestKey(key=str(key), request_hash=digest, response_body=result)
        )
        return result


async def warehouse(db, ident, active=False, exclusive=False):
    row = await db.scalar(
        select(Warehouse)
        .where(Warehouse.id == str(ident))
        .with_for_update(read=not exclusive)
        .execution_options(populate_existing=True)
    )
    if not row:
        fail("Склад не найден.")
    if active and row.archived_at:
        fail("Для этой операции нужен действующий склад.")
    return row


async def lock_warehouses(db, identifiers):
    rows = {}
    for ident in sorted({str(i) for i in identifiers if i}):
        rows[ident] = await warehouse(db, ident)
    return rows


async def lock_balance(db, product, wh):
    # Upsert safely handles two first receipts for an absent balance.
    await db.execute(
        insert(Balance)
        .values(
            warehouse_id=str(wh),
            product_id=str(product),
            on_hand=0,
            reserved=0,
        )
        .on_conflict_do_nothing(
            index_elements=[Balance.warehouse_id, Balance.product_id]
        )
    )
    return await db.scalar(
        select(Balance)
        .where(
            Balance.warehouse_id == str(wh),
            Balance.product_id == str(product),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def change(db, product, wh, physical=0, reserved=0):
    # The caller acquires lock_balance before checking/changing this row.
    balance = await db.get(Balance, (str(wh), str(product)))
    balance.on_hand += physical
    balance.reserved += reserved
    balance.updated_at = datetime.now(timezone.utc)
    return {
        "warehouse_id": str(wh),
        "on_hand_delta": physical,
        "reserved_delta": reserved,
    }


async def available(db, product, wh, quantity):
    row = await db.get(Balance, (str(wh), str(product)))
    n = row.on_hand - row.reserved if row else 0
    if n < quantity:
        alternatives = (
            await db.execute(
                select(
                    Warehouse.name,
                    (Balance.on_hand - Balance.reserved).label("n"),
                )
                .join(Balance, Balance.warehouse_id == Warehouse.id)
                .where(
                    Balance.product_id == str(product),
                    Balance.warehouse_id != str(wh),
                    Balance.on_hand > Balance.reserved,
                )
                .order_by(Warehouse.name)
                .limit(10)
            )
        ).all()
        suffix = "; ".join(f"{r.name}: {r.n} шт." for r in alternatives)
        fail(
            f"На выбранном ФФ доступно {n} шт., требуется {quantity}."
            + (f" Другие ФФ: {suffix}" if suffix else "")
        )


def event(db, ident, kind, before, after, changes):
    db.add(
        Event(
            operation_id=str(ident),
            event_type=kind,
            from_status=before,
            to_status=after,
            balance_changes=changes,
        )
    )


async def create_operation(db, data):
    p, q, kind = data["product_id"], data["quantity"], data["type"]
    source, dest = data.get("source_warehouse_id"), data.get(
        "destination_warehouse_id"
    )
    if not await db.get(Product, p):
        fail("Товар не найден.")
    warehouses = await lock_warehouses(db, [source, dest])
    changes = []
    if kind == "receipt":
        if source or not dest:
            fail("Для прихода укажите только склад назначения.")
        if warehouses[dest].archived_at:
            fail("Для этой операции нужен действующий склад.")
        await lock_balance(db, p, dest)
        changes.append(await change(db, p, dest, physical=q))
        status = "completed"
    else:
        if not source:
            fail("Выберите склад отправления.")
        if kind != "transfer" and warehouses[source].archived_at:
            fail("Для этой операции нужен действующий склад.")
        if kind == "transfer":
            if not dest or source == dest:
                fail("Выберите другой склад назначения.")
            if warehouses[dest].archived_at:
                fail("Для этой операции нужен действующий склад.")
        elif dest:
            fail("Для FBS/FBO склад назначения не указывается.")
        await lock_balance(db, p, source)
        await available(db, p, source, q)
        changes.append(
            await change(db, p, source, reserved=q)
            if kind == "fbs"
            else await change(db, p, source, physical=-q)
        )
        status = "reserved" if kind == "fbs" else "in_transit"
    ident = str(uuid4())
    db.add(
        Operation(
            id=ident,
            type=kind,
            status=status,
            product_id=p,
            quantity=q,
            source_warehouse_id=source,
            destination_warehouse_id=dest,
        )
    )
    await db.flush()  # Insert parent before the event FK, without committing.
    event(db, ident, kind, None, status, changes)
    return {"id": ident, "status": status}


async def transition(db, ident, action):
    op = await db.scalar(
        select(Operation)
        .where(Operation.id == str(ident))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not op:
        fail("Операция не найдена.")
    target = {"receive": "received", "ship": "shipped", "cancel": "cancelled"}[
        action
    ]
    allowed = (action == "receive" and op.type in ("transfer", "fbo")) or (
        action in ("ship", "cancel") and op.type == "fbs"
    )
    if not allowed:
        fail("Это действие не подходит для операции.")
    if op.status == target:
        return {"id": str(ident), "status": target}
    expected = "in_transit" if action == "receive" else "reserved"
    if op.status != expected:
        fail("Операция уже завершена другим действием.")
    await lock_warehouses(
        db, [op.source_warehouse_id, op.destination_warehouse_id]
    )
    changes = []
    if action == "receive" and op.type == "transfer":
        await lock_balance(db, op.product_id, op.destination_warehouse_id)
        changes.append(
            await change(
                db,
                op.product_id,
                op.destination_warehouse_id,
                physical=op.quantity,
            )
        )
    elif action in ("ship", "cancel"):
        await lock_balance(db, op.product_id, op.source_warehouse_id)
        changes.append(
            await change(
                db,
                op.product_id,
                op.source_warehouse_id,
                physical=-op.quantity if action == "ship" else 0,
                reserved=-op.quantity,
            )
        )
    event(db, ident, action, op.status, target, changes)
    op.status, op.updated_at = target, datetime.now(timezone.utc)
    return {"id": str(ident), "status": target}
