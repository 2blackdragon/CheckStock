import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .models import Base

url = os.environ.get("DATABASE_URL", "postgresql://localhost/checkstock")
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_async_engine(
    url, pool_pre_ping=True, pool_size=5, max_overflow=5
)
Session = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def connect():
    async with Session.begin() as session:
        yield session


async def initialize():
    # Bootstrap an empty database; evolve existing schemas with Alembic.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
