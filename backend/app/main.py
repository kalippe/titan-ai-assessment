import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_session, init_db
from app.models import Tenant
from app.routers import catalog, ingest, ingest_logs, reading_lists, tenants

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Fixed set of library branches in the consortium. Seeded on every startup via
# on_conflict_do_nothing so restarts are idempotent and existing data is untouched.
SEED_TENANTS = [
    {"slug": "branch-a", "name": "Branch A — Downtown"},
    {"slug": "branch-b", "name": "Branch B — Westside"},
    {"slug": "branch-c", "name": "Branch C — Northside"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up: initialise DB tables, seed tenants, open shared HTTP client."""
    logger.info("Starting up — running DB migrations")
    await init_db()
    logger.info("DB ready")

    async with AsyncSessionLocal() as session:
        await session.execute(
            pg_insert(Tenant)
            .values(SEED_TENANTS)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        await session.commit()
    logger.info("Tenant seed complete: %d branches ready", len(SEED_TENANTS))

    async with httpx.AsyncClient() as client:
        app.state.http = client
        logger.info("HTTP client initialised")
        yield

    logger.info("Shutting down — HTTP client closed")


app = FastAPI(title="Titan Library Catalog", lifespan=lifespan)

app.include_router(tenants.router)
app.include_router(ingest.router)
app.include_router(ingest_logs.router)
app.include_router(catalog.router)
app.include_router(reading_lists.router)


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    """Liveness + DB reachability check. Returns 503 if the database is unavailable."""
    try:
        await session.execute(text("SELECT 1"))
        logger.debug("Health check passed")
        return {"status": "ok", "db": "reachable"}
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc
