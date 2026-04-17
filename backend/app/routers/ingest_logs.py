import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.dependencies import resolve_tenant
from app.models import IngestionLog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ingestion"], dependencies=[Depends(resolve_tenant)])


@router.get(
    "/api/{tenant_id}/ingest-logs",
    response_model=list[IngestionLog],
    summary="List ingest job history for a tenant",
)
async def list_ingest_logs(
    tenant_id: str,
    status: str | None = Query(
        default=None,
        description="Filter by status: `pending`, `success`, `partial`, `failed`",
    ),
    limit: int = Query(default=50, le=200, description="Maximum number of records to return"),
    session: AsyncSession = Depends(get_session),
):
    """Return the ingest history for a tenant, newest first.

    Each entry captures the query, result counts, timestamps, and any error detail.
    Filter by `status` to surface only in-progress jobs, failures, or partial runs.
    """
    logger.info("Ingest log query: tenant=%r status=%r limit=%d", tenant_id, status, limit)

    stmt = (
        select(IngestionLog)
        .where(IngestionLog.tenant_id == tenant_id)
        .order_by(IngestionLog.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(IngestionLog.status == status)

    result = await session.execute(stmt)
    logs = result.scalars().all()

    logger.info("Returning %d ingest log entries for tenant=%r", len(logs), tenant_id)
    return logs


@router.get(
    "/api/{tenant_id}/ingest-logs/{log_id}",
    response_model=IngestionLog,
    summary="Get a single ingest job by ID",
    responses={
        200: {"description": "Job found — check `status` field for current state"},
        404: {"description": "No ingest log with that ID exists for this tenant"},
    },
)
async def get_ingest_log(
    tenant_id: str,
    log_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Fetch a single ingest job by the `job_id` returned from `POST /ingest`.

    Poll this endpoint after triggering an ingest to track progress:
    - `pending` — background task is still running
    - `success` — all fetched works were upserted
    - `partial` — some works were dropped (missing `key` or `title` from Open Library)
    - `failed` — upstream error; see `error_detail` for the exception message

    The `tenant_id` path parameter is enforced — a tenant cannot read another tenant's jobs.
    """
    log = await session.get(IngestionLog, log_id)
    if log is None or log.tenant_id != tenant_id:
        logger.warning("Ingest log not found: tenant=%r log_id=%d", tenant_id, log_id)
        raise HTTPException(status_code=404, detail="Ingest log not found")
    return log
