from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from . import queries
from .db import connect, engine, initialize
from .models import Balance, Product, Warehouse
from .service import create_operation, fail, mutate, transition, warehouse


@asynccontextmanager
async def lifespan(app):
    await initialize()
    yield
    await engine.dispose()


app = FastAPI(title="CheckStock", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["receipt", "transfer", "fbs", "fbo"]
    product_id: UUID
    quantity: int = Field(strict=True, gt=0, le=1000000)
    source_warehouse_id: UUID | None = None
    destination_warehouse_id: UUID | None = None
    key: UUID


class WarehouseInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    key: UUID


class Action(BaseModel):
    key: UUID


@app.get("/", response_class=HTMLResponse)
async def home():
    return (Path(__file__).parent / "templates/index.html").read_text()


@app.get("/api/catalog")
async def catalog():
    async with connect() as db:
        products = (
            await db.scalars(select(Product).order_by(Product.sku).limit(1000))
        ).all()
        warehouses = (
            await db.scalars(
                select(Warehouse)
                .order_by(Warehouse.archived_at.nullsfirst(), Warehouse.name)
                .limit(1000)
            )
        ).all()
        return {
            "products": [queries.fields(x) for x in products],
            "warehouses": [queries.fields(x) for x in warehouses],
        }


@app.post("/api/warehouses")
async def add_warehouse(data: WarehouseInput):
    async def run(db):
        if not data.name.strip():
            fail("Введите название склада.")
        ident = str(uuid4())
        db.add(Warehouse(id=ident, name=data.name.strip()))
        return {"id": ident}

    return await mutate(
        data.key, {"action": "create_warehouse", "name": data.name}, run
    )


@app.post("/api/warehouses/{ident}/rename")
async def rename(ident: UUID, data: WarehouseInput):
    async def run(db):
        row = await warehouse(db, ident, exclusive=True)
        if not data.name.strip():
            fail("Введите название склада.")
        row.name = data.name.strip()
        return {"id": str(ident)}

    return await mutate(
        data.key, {"action": "rename", "id": ident, "name": data.name}, run
    )


@app.post("/api/warehouses/{ident}/{action}")
async def archive(
    ident: UUID, action: Literal["archive", "restore"], data: Action
):
    async def run(db):
        row = await warehouse(db, ident, exclusive=True)
        row.archived_at = (
            (row.archived_at or datetime.now(timezone.utc))
            if action == "archive"
            else None
        )
        return {"id": str(ident)}

    return await mutate(data.key, {"action": action, "id": ident}, run)


@app.post("/api/operations")
async def create(data: OperationInput):
    payload = data.model_dump(mode="json", exclude={"key"})
    return await mutate(
        data.key, payload, lambda db: create_operation(db, payload)
    )


@app.post("/api/operations/{ident}/{action}")
async def perform(
    ident: UUID, action: Literal["receive", "ship", "cancel"], data: Action
):
    return await mutate(
        data.key,
        {"id": ident, "action": action},
        lambda db: transition(db, ident, action),
    )


@app.get("/api/stocks")
async def stocks(
    q: str = Query("", max_length=100),
    warehouse_id: UUID | None = None,
    mode: Literal["all", "reserved", "transit"] = "all",
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(25, ge=1, le=100),
):
    async with connect() as db:
        return await queries.stocks(
            db,
            q,
            str(warehouse_id) if warehouse_id else None,
            mode,
            page,
            limit,
        )


@app.get("/api/products/{ident}")
async def detail(ident: UUID):
    async with connect() as db:
        if not await db.get(Product, str(ident)):
            fail("Товар не найден.")
        rows = (
            await db.execute(
                select(
                    Warehouse,
                    func.coalesce(Balance.on_hand, 0).label("on_hand"),
                    func.coalesce(Balance.reserved, 0).label("reserved"),
                )
                .outerjoin(
                    Balance,
                    (Balance.warehouse_id == Warehouse.id)
                    & (Balance.product_id == str(ident)),
                )
                .order_by(Warehouse.name)
                .limit(1000)
            )
        ).all()
        ops = await queries.operations(db, "", None, "all", 1, 25, str(ident))
        return {
            "balances": [
                dict(
                    queries.fields(r[0]),
                    on_hand=r.on_hand,
                    reserved=r.reserved,
                )
                for r in rows
            ],
            "operations": ops["items"],
            "has_more_operations": ops["has_next"],
        }


@app.get("/api/operations")
async def operations(
    q: str = Query("", max_length=100),
    status: Literal["active", "all"] = "active",
    warehouse_id: UUID | None = None,
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(25, ge=1, le=100),
):
    async with connect() as db:
        return await queries.operations(
            db,
            q,
            str(warehouse_id) if warehouse_id else None,
            status,
            page,
            limit,
        )


@app.get("/api/history")
async def history(
    q: str = Query("", max_length=100),
    warehouse_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(1, ge=1, le=10000),
    limit: int = Query(25, ge=1, le=100),
):
    async with connect() as db:
        return await queries.history(
            db,
            q,
            str(warehouse_id) if warehouse_id else None,
            since,
            until,
            page,
            limit,
        )
