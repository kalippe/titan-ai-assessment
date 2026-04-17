import logging
import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://titan:titan@db:5432/titan")

# echo=False keeps SQL out of production logs; flip to True locally when debugging queries.
engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all SQLModel tables if they don't already exist.

    Called once at startup via the FastAPI lifespan. All models must be imported
    before this runs so SQLModel.metadata knows about their tables.
    """
    logger.info("Running create_all against %s", DATABASE_URL.split("@")[-1])
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a per-request async DB session.

    The session is automatically closed when the request completes.
    Use as: session: AsyncSession = Depends(get_session)
    """
    async with AsyncSessionLocal() as session:
        yield session
