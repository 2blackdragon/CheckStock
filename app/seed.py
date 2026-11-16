"""Idempotent sample receipts. Run: python -m app.seed."""

import asyncio
from uuid import NAMESPACE_URL, uuid5

from .db import connect, engine, initialize
from .models import Product, Warehouse
from .service import create_operation, lock_request, mutate


def uid(value):
    return str(uuid5(NAMESPACE_URL, "checkstock/demo/" + value))


async def seed():
    await initialize()
    async with connect() as db:
        await lock_request(db, uid("catalog-bootstrap"))
        for sku, name in [
            ("SKU-101", "Органайзер"),
            ("SKU-102", "Контейнер"),
            ("SKU-103", "Корзина"),
        ]:
            if not await db.get(Product, uid(sku)):
                db.add(Product(id=uid(sku), sku=sku, name=name))
        for name in ["Север", "Юг"]:
            if not await db.get(Warehouse, uid(name)):
                db.add(Warehouse(id=uid(name), name=name))
    for sku, north, south in [
        ("SKU-101", 10, 6),
        ("SKU-102", 5, 0),
        ("SKU-103", 0, 4),
    ]:
        for name, q in [("Север", north), ("Юг", south)]:
            if q:
                data = {
                    "type": "receipt",
                    "product_id": uid(sku),
                    "quantity": q,
                    "destination_warehouse_id": uid(name),
                }
                await mutate(
                    uid(sku + name),
                    data,
                    lambda db, data=data: create_operation(db, data),
                )
    print("Тестовые приходы готовы. Повторный запуск не увеличивает остатки.")


async def main():
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
