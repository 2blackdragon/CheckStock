"""Integration tests require a separate PostgreSQL database, never SQLite.

Each test creates unique goods/warehouses; existing records are not deleted.
Run only against a test database (TEST_DATABASE_URL is required).
"""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.db import connect, engine, initialize
from app.main import app
from app.models import Event, Operation, Product, Warehouse

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip(
        "Set TEST_DATABASE_URL to a dedicated PostgreSQL database",
        allow_module_level=True,
    )


def run(scenario):
    async def wrapped():
        try:
            await initialize()
            await scenario()
        finally:
            await engine.dispose()

    asyncio.run(wrapped())


async def setup():
    p, a, b = [str(uuid4()) for _ in range(3)]
    async with connect() as db:
        db.add(Product(id=p, sku="TEST-" + p, name="Тестовый товар"))
        db.add_all(
            [
                Warehouse(id=a, name="Север " + a),
                Warehouse(id=b, name="Юг " + b),
            ]
        )
    return p, a, b


async def post(c, path, data=None, key=None, expected=200):
    response = await c.post(
        "/api/" + path, json={**(data or {}), "key": key or str(uuid4())}
    )
    assert response.status_code == expected, response.text
    return response.json()


async def op(c, p, a, kind, q, dest=None, **kw):
    data = {"type": kind, "product_id": p, "quantity": q}
    if kind != "receipt":
        data["source_warehouse_id"] = a
    if kind in ("receipt", "transfer"):
        data["destination_warehouse_id"] = dest or a
    return await post(c, "operations", data, **kw)


async def balance(c, p, a):
    response = await c.get("/api/products/" + p)
    assert response.status_code == 200, response.text
    return next(x for x in response.json()["balances"] if x["id"] == a)


def test_receipt_reserve_cancel_ship_and_invalid_transition():
    async def scenario():
        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            r = await op(c, p, a, "fbs", 3)
            b = await balance(c, p, a)
            assert (b["on_hand"], b["reserved"]) == (10, 3)
            await post(c, f"operations/{r['id']}/cancel")
            assert (await balance(c, p, a))["reserved"] == 0
            await post(c, f"operations/{r['id']}/ship", expected=409)
            r = await op(c, p, a, "fbs", 4)
            await post(c, f"operations/{r['id']}/ship")
            await post(c, f"operations/{r['id']}/ship")
            b = await balance(c, p, a)
            assert (b["on_hand"], b["reserved"]) == (6, 0)

    run(scenario)


def test_transfers_fbo_and_repeated_receipt():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            r = await op(c, p, a, "transfer", 4, dest=b)
            assert (await balance(c, p, a))["on_hand"] == 6
            assert (await balance(c, p, b))["on_hand"] == 0
            await post(c, f"operations/{r['id']}/receive")
            await post(c, f"operations/{r['id']}/receive")
            assert (await balance(c, p, b))["on_hand"] == 4
            f = await op(c, p, b, "fbo", 3)
            await post(c, f"operations/{f['id']}/receive")
            await post(c, f"operations/{f['id']}/receive")
            assert (await balance(c, p, b))["on_hand"] == 1
            async with connect() as db:
                count = await db.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.operation_id == f["id"])
                )
                assert count == 2

    run(scenario)


def test_shortage_is_local_and_reserved_is_unavailable():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 5)
            await op(c, p, b, "receipt", 20)
            await op(c, p, a, "fbs", 4)
            await op(c, p, a, "fbo", 2, expected=409)
            await op(c, p, a, "transfer", 2, dest=b, expected=409)
            assert (await balance(c, p, a))["on_hand"] == 5
            assert (await balance(c, p, b))["on_hand"] == 20

    run(scenario)


def test_archive_preserves_goods_allows_drain_and_pending_receipt():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            await op(c, p, b, "receipt", 5)
            reserve = await op(c, p, a, "fbs", 2)
            incoming = await op(c, p, b, "transfer", 3, dest=a)
            await post(c, f"warehouses/{a}/archive")
            assert (await balance(c, p, a))["on_hand"] == 10
            for kind in ["receipt", "fbs", "fbo"]:
                await op(c, p, a, kind, 1, expected=409)
            await op(c, p, b, "transfer", 1, dest=a, expected=409)
            await post(c, f"operations/{reserve['id']}/ship")
            await post(c, f"operations/{incoming['id']}/receive")
            assert (await balance(c, p, a))["on_hand"] == 11
            outgoing = await op(c, p, a, "transfer", 11, dest=b)
            await post(c, f"operations/{outgoing['id']}/receive")
            assert (await balance(c, p, a))["on_hand"] == 0
            await post(c, f"warehouses/{a}/restore")
            await op(c, p, a, "receipt", 1)

    run(scenario)


def test_idempotency_concurrent_creation_and_key_conflict():
    async def scenario():
        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            key = str(uuid4())
            results = await asyncio.gather(
                op(c, p, a, "receipt", 10, key=key),
                op(c, p, a, "receipt", 10, key=key),
            )
            assert results[0]["id"] == results[1]["id"]
            assert (await balance(c, p, a))["on_hand"] == 10
            await op(c, p, a, "receipt", 11, key=key, expected=409)

    run(scenario)


def test_concurrent_reserves_cannot_oversell():
    async def scenario():
        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 5)

            async def reserve():
                return await c.post(
                    "/api/operations",
                    json={
                        "key": str(uuid4()),
                        "type": "fbs",
                        "product_id": p,
                        "source_warehouse_id": a,
                        "quantity": 4,
                    },
                )

            responses = await asyncio.gather(reserve(), reserve())
            assert sorted(r.status_code for r in responses) == [200, 409]
            b = await balance(c, p, a)
            assert (b["on_hand"], b["reserved"]) == (5, 4)

    run(scenario)


def test_validation_zero_positions_and_pagination():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 0, expected=422)
            await op(c, p, a, "receipt", 1.5, expected=422)
            await op(c, p, a, "transfer", 1, dest=a, expected=409)
            response = await c.get(
                "/api/stocks", params={"q": "TEST-" + p, "warehouse_id": a}
            )
            assert response.status_code == 200, response.text
            row = response.json()["items"][0]
            assert (row["physical"], row["available"]) == (0, 0)
            assert (await c.get("/api/stocks?limit=101")).status_code == 422
            await op(c, p, a, "receipt", 8)
            await op(c, p, a, "transfer", 3, dest=b)
            rows = (
                await c.get("/api/stocks", params={"q": "TEST-" + p})
            ).json()["items"]
            assert rows[0]["physical"] == 5 and rows[0]["incoming"] == 3

    run(scenario)


def test_all_transit_types_and_per_warehouse_aggregation():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 50)
            await op(c, p, b, "receipt", 20)
            await op(c, p, a, "transfer", 7, dest=b)
            await op(c, p, a, "transfer", 3, dest=b)
            await op(c, p, b, "transfer", 2, dest=a)
            await op(c, p, a, "fbo", 4)
            f = await op(c, p, a, "fbs", 5)
            await post(c, f"operations/{f['id']}/ship")
            await op(c, p, b, "fbs", 6)

            async def stock(wh=None):
                params = {"q": "TEST-" + p}
                if wh:
                    params["warehouse_id"] = wh
                response = await c.get("/api/stocks", params=params)
                assert response.status_code == 200, response.text
                return response.json()["items"][0]

            total = await stock()
            assert [
                total[k]
                for k in [
                    "physical",
                    "reserved",
                    "available",
                    "incoming",
                    "fbo",
                    "fbs",
                ]
            ] == [49, 6, 43, 12, 4, 5]
            first, second = await stock(a), await stock(b)
            assert (first["incoming"], first["outgoing"]) == (2, 10)
            assert (second["incoming"], second["outgoing"]) == (10, 2)
            assert first["physical"] + second["physical"] == total["physical"]
            assert second["fbo"] == second["fbs"] == 0

    run(scenario)


def test_rejected_operation_is_rolled_back_including_history_and_key():
    async def scenario():
        from app.models import RequestKey

        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            key = str(uuid4())
            await op(c, p, a, "fbs", 3, key=key, expected=409)
            async with connect() as db:
                assert await db.get(RequestKey, key) is None
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(Operation)
                        .where(Operation.product_id == p)
                    )
                    == 0
                )
            await op(c, p, a, "receipt", 3)
            await op(c, p, a, "fbs", 3, key=key)
            b = await balance(c, p, a)
            assert (b["on_hand"], b["reserved"]) == (3, 3)

    run(scenario)


def test_concurrent_ship_and_cancel_only_one_wins():
    async def scenario():
        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            r = await op(c, p, a, "fbs", 4)
            responses = await asyncio.gather(
                *[
                    c.post(
                        f"/api/operations/{r['id']}/{action}",
                        json={"key": str(uuid4())},
                    )
                    for action in ["ship", "cancel"]
                ]
            )
            assert sorted(r.status_code for r in responses) == [200, 409]
            b = await balance(c, p, a)
            assert b["reserved"] == 0 and b["on_hand"] in [6, 10]
            async with connect() as db:
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.operation_id == r["id"])
                    )
                    == 2
                )

    run(scenario)


def test_concurrent_receive_increments_destination_once():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            r = await op(c, p, a, "transfer", 10, dest=b)
            await asyncio.gather(
                *[post(c, f"operations/{r['id']}/receive") for _ in range(5)]
            )
            assert (await balance(c, p, b))["on_hand"] == 10
            async with connect() as db:
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(Event.operation_id == r["id"])
                    )
                    == 2
                )

    run(scenario)


def test_rename_keeps_route_and_history_and_whitespace_name_is_rejected():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            await op(c, p, a, "transfer", 3, dest=b)
            name = "Переименованный ФФ " + a
            await post(c, f"warehouses/{a}/rename", {"name": name})
            await post(
                c, f"warehouses/{a}/rename", {"name": "   "}, expected=409
            )
            h = (
                await c.get(
                    "/api/history",
                    params={"q": "TEST-" + p, "warehouse_id": a},
                )
            ).json()["items"]
            assert len(h) == 2
            assert (
                next(r for r in h if r["type"] == "transfer")["source_name"]
                == name
            )
            assert (await balance(c, p, a))["on_hand"] == 7

    run(scenario)


def test_pagination_search_and_state_filters():
    async def scenario():
        p, a, _ = await setup()
        prefix = "PAGE-" + str(uuid4())
        async with connect() as db:
            for i in range(31):
                db.add(
                    Product(
                        id=str(uuid4()), sku=f"{prefix}-{i:02}", name="Позиция"
                    )
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            one = (
                await c.get("/api/stocks", params={"q": prefix, "limit": 25})
            ).json()
            two = (
                await c.get(
                    "/api/stocks", params={"q": prefix, "limit": 25, "page": 2}
                )
            ).json()
            assert len(one["items"]) == 25 and one["has_next"]
            assert len(two["items"]) == 6 and not two["has_next"]
            assert len({r["id"] for r in one["items"] + two["items"]}) == 31
            for mode in ["reserved", "transit"]:
                assert not (
                    await c.get(
                        "/api/stocks", params={"q": "TEST-" + p, "mode": mode}
                    )
                ).json()["items"]
            await op(c, p, a, "receipt", 5)
            r = await op(c, p, a, "fbs", 2)
            assert (
                len(
                    (
                        await c.get(
                            "/api/stocks",
                            params={"q": "TEST-" + p, "mode": "reserved"},
                        )
                    ).json()["items"]
                )
                == 1
            )
            await post(c, f"operations/{r['id']}/ship")
            assert (
                len(
                    (
                        await c.get(
                            "/api/stocks",
                            params={"q": "TEST-" + p, "mode": "transit"},
                        )
                    ).json()["items"]
                )
                == 1
            )
            assert not (
                await c.get(
                    "/api/operations",
                    params={"q": "TEST-" + p, "status": "active"},
                )
            ).json()["items"]

    run(scenario)


def test_history_deltas_explain_current_balances():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 20)
            r = await op(c, p, a, "fbs", 4)
            await post(c, f"operations/{r['id']}/ship")
            r = await op(c, p, a, "fbs", 2)
            await post(c, f"operations/{r['id']}/cancel")
            r = await op(c, p, a, "transfer", 6, dest=b)
            await post(c, f"operations/{r['id']}/receive")
            await op(c, p, b, "fbo", 3)
            h = (
                await c.get(
                    "/api/history", params={"q": "TEST-" + p, "limit": 100}
                )
            ).json()["items"]
            for wh in [a, b]:
                changes = [
                    delta
                    for e in h
                    for delta in e["balance_changes"]
                    if delta["warehouse_id"] == wh
                ]
                balance_now = await balance(c, p, wh)
                assert (
                    sum(d["on_hand_delta"] for d in changes)
                    == balance_now["on_hand"]
                )
                assert (
                    sum(d["reserved_delta"] for d in changes)
                    == balance_now["reserved"]
                )
            assert not (
                await c.get(
                    "/api/history",
                    params={"q": "TEST-" + p, "since": "2100-01-01T00:00:00Z"},
                )
            ).json()["items"]

    run(scenario)


async def wait_for_database_lock(pid):
    """Observe actual PostgreSQL waiting, rather than infer it from timing."""
    from sqlalchemy import column, table

    activity = table(
        "pg_stat_activity", column("pid"), column("wait_event_type")
    )
    async with asyncio.timeout(5):
        while True:
            async with connect() as db:
                waiting = await db.scalar(
                    select(activity.c.wait_event_type).where(
                        activity.c.pid == pid
                    )
                )
            if waiting == "Lock":
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("same_warehouse", [True, False])
def test_independent_balances_progress_while_another_balance_waits(
    same_warehouse,
):
    async def scenario():
        from app.service import (
            create_operation,
            lock_balance,
            mutate,
            warehouse,
        )

        p, a, b = await setup()
        other = str(uuid4())
        async with connect() as db:
            db.add(Product(id=other, sku="OTHER-" + other, name="Другой"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 5)
            pid = asyncio.get_running_loop().create_future()
            payload = {
                "type": "fbs",
                "product_id": p,
                "quantity": 1,
                "source_warehouse_id": a,
            }

            async def action(db):
                pid.set_result(await db.scalar(select(func.pg_backend_pid())))
                return await create_operation(db, payload)

            task = None
            try:
                async with connect() as holder:
                    await warehouse(holder, a)
                    await lock_balance(holder, p, a)
                    task = asyncio.create_task(
                        mutate(str(uuid4()), payload, action)
                    )
                    await wait_for_database_lock(await pid)
                    # Both cases must finish before the first lock is released:
                    # another SKU on this FF, or this SKU on another FF.
                    await asyncio.wait_for(
                        op(
                            c,
                            other if same_warehouse else p,
                            a if same_warehouse else b,
                            "receipt",
                            2,
                        ),
                        timeout=5,
                    )
                    assert not task.done()
                await asyncio.wait_for(task, timeout=5)
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            stock = await balance(c, p, a)
            assert (stock["on_hand"], stock["reserved"]) == (5, 1)

    run(scenario)


def test_concurrent_first_receipts_create_one_balance_without_loss():
    async def scenario():
        p, a, _ = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await asyncio.wait_for(
                asyncio.gather(*[op(c, p, a, "receipt", 1) for _ in range(8)]),
                timeout=10,
            )
            assert (await balance(c, p, a))["on_hand"] == 8

    run(scenario)


def test_two_receipts_of_transfers_to_initially_empty_destination():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            one = await op(c, p, a, "transfer", 3, dest=b)
            two = await op(c, p, a, "transfer", 4, dest=b)
            await asyncio.wait_for(
                asyncio.gather(
                    post(c, f"operations/{one['id']}/receive"),
                    post(c, f"operations/{two['id']}/receive"),
                ),
                timeout=5,
            )
            assert (await balance(c, p, b))["on_hand"] == 7

    run(scenario)


def test_archive_waits_for_stock_command_already_using_warehouse():
    async def scenario():
        from datetime import datetime, timezone

        from app.service import create_operation, mutate, warehouse

        p, a, _ = await setup()
        pid = asyncio.get_running_loop().create_future()

        async def archive_action(db):
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
            row = await warehouse(db, a, exclusive=True)
            row.archived_at = datetime.now(timezone.utc)
            return {"id": a}

        task = None
        try:
            async with connect() as holder:
                await create_operation(
                    holder,
                    {
                        "type": "receipt",
                        "product_id": p,
                        "quantity": 5,
                        "destination_warehouse_id": a,
                    },
                )
                task = asyncio.create_task(
                    mutate(
                        str(uuid4()),
                        {"archive": a},
                        archive_action,
                    )
                )
                await wait_for_database_lock(await pid)
                assert not task.done()
            await asyncio.wait_for(task, timeout=5)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            row = await balance(c, p, a)
            assert row["archived_at"] and row["on_hand"] == 5
            await op(c, p, a, "receipt", 1, expected=409)

    run(scenario)


def test_receipt_waiting_for_archive_rechecks_warehouse_state():
    async def scenario():
        from datetime import datetime, timezone

        from fastapi import HTTPException

        from app.service import create_operation, mutate, warehouse

        p, a, _ = await setup()
        pid = asyncio.get_running_loop().create_future()
        payload = {
            "type": "receipt",
            "product_id": p,
            "quantity": 5,
            "destination_warehouse_id": a,
        }

        async def action(db):
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))
            return await create_operation(db, payload)

        task = None
        try:
            async with connect() as holder:
                row = await warehouse(holder, a, exclusive=True)
                row.archived_at = datetime.now(timezone.utc)
                await holder.flush()
                task = asyncio.create_task(
                    mutate(
                        str(uuid4()),
                        payload,
                        action,
                    )
                )
                await wait_for_database_lock(await pid)
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, timeout=5)
            assert error.value.status_code == 409
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            assert (await balance(c, p, a))["on_hand"] == 0

    run(scenario)


def test_opposite_transfers_and_receipts_do_not_deadlock():
    async def scenario():
        p, a, b = await setup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            await op(c, p, a, "receipt", 10)
            await op(c, p, b, "receipt", 10)
            one, two = await asyncio.wait_for(
                asyncio.gather(
                    op(c, p, a, "transfer", 3, dest=b),
                    op(c, p, b, "transfer", 4, dest=a),
                ),
                timeout=5,
            )
            await asyncio.wait_for(
                asyncio.gather(
                    post(c, f"operations/{one['id']}/receive"),
                    post(c, f"operations/{two['id']}/receive"),
                ),
                timeout=5,
            )
            assert (await balance(c, p, a))["on_hand"] == 11
            assert (await balance(c, p, b))["on_hand"] == 9

    run(scenario)
