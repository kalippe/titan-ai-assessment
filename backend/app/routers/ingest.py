import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.database import AsyncSessionLocal, get_session
from app.dependencies import resolve_tenant
from app.models import Book, IngestionLog
from app.open_library import enrich_works, search_works_by_author

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ingestion"], dependencies=[Depends(resolve_tenant)])


class IngestRequest(BaseModel):
    author: str

    model_config = {
        "json_schema_extra": {
            "example": {"author": "Robert Jordan"}
        }
    }


class IngestAccepted(BaseModel):
    """Immediate response returned when an ingest job is accepted."""

    job_id: int
    tenant_id: str
    author: str
    status: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_id": 1,
                "tenant_id": "branch-a",
                "author": "Robert Jordan",
                "status": "pending",
            }
        }
    }


async def _ingest_task(
    tenant_id: str,
    author: str,
    log_id: int,
    http_client: httpx.AsyncClient,
) -> None:
    """Fetch, enrich, and upsert Open Library works after the 202 response is sent.

    Opens its own DB session — the request-scoped session is already closed before
    BackgroundTasks execute. Updates the pre-created IngestionLog (log_id) with
    final counts and status so callers can poll the ingest-logs endpoint for progress.

    Two rounds of Open Library requests run here:
      1. search_works_by_author — search.json results (may have truncated subjects/missing names)
      2. enrich_works — per-work detail and per-author detail follow-ups to fill gaps
    """
    async with AsyncSessionLocal() as session:
        log = await session.get(IngestionLog, log_id)
        if log is None:
            logger.error("Ingest task: log_id=%d not found in DB", log_id)
            return

        try:
            search_docs = await search_works_by_author(http_client, author)
            docs = await enrich_works(http_client, search_docs)
        except Exception as exc:
            logger.error(
                "Ingest task failed: tenant=%r author=%r log_id=%d error=%s",
                tenant_id, author, log_id, exc,
            )
            log.status = "failed"
            log.error_detail = str(exc)
            session.add(log)
            await session.commit()
            return

        fetched_count = len(docs)

        rows = [
            {
                "tenant_id": tenant_id,
                "ol_key": doc["key"],
                "title": doc["title"],
                "author": (doc.get("author_name") or [None])[0],
                "first_publish_year": doc.get("first_publish_year"),
                "subjects": doc.get("subject") or [],
                # cover_i may come from the search result or from the work-detail fallback.
                "cover_url": f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
                if doc.get("cover_i")
                else None,
            }
            for doc in docs
            if doc.get("key") and doc.get("title")
        ]

        success_count = len(rows)
        fail_count = fetched_count - success_count

        if rows:
            # Upsert: refresh metadata on conflict; created_at excluded to preserve
            # the original ingestion date.
            stmt = pg_insert(Book).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_book_tenant",
                set_={
                    "title": stmt.excluded.title,
                    "author": stmt.excluded.author,
                    "first_publish_year": stmt.excluded.first_publish_year,
                    "subjects": stmt.excluded.subjects,
                    "cover_url": stmt.excluded.cover_url,
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)

        log.status = "success" if fail_count == 0 else "partial"
        log.fetched_count = fetched_count
        log.success_count = success_count
        log.fail_count = fail_count
        session.add(log)
        await session.commit()

        logger.info(
            "Ingest complete: tenant=%r author=%r log_id=%d status=%r "
            "fetched=%d success=%d fail=%d",
            tenant_id, author, log_id, log.status,
            fetched_count, success_count, fail_count,
        )


@router.post(
    "/api/{tenant_id}/ingest",
    status_code=202,
    response_model=IngestAccepted,
    summary="Trigger a background catalog ingest by author",
    responses={
        202: {"description": "Job accepted — poll `/ingest-logs/{job_id}` for status"},
    },
)
async def ingest(
    tenant_id: str,
    body: IngestRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Accept an ingest request and return 202 with a job ID immediately.

    Creates the IngestionLog with status `pending` before responding so callers
    can poll `GET /api/{tenant_id}/ingest-logs/{job_id}` for progress.

    The background task runs two rounds of Open Library requests:
    - **Search** — `search.json?author=...` returns up to 100 work stubs.
    - **Enrichment** — concurrent follow-up calls (≤ 5 in-flight) to `/works/{key}.json`
      for full subjects and `/authors/{key}.json` for any missing author names.

    Enriched books are upserted into the tenant catalog and the log is updated to
    `success`, `partial` (some docs dropped), or `failed` (upstream error).
    """
    log = IngestionLog(
        tenant_id=tenant_id,
        query_type="author",
        query_value=body.author,
        status="pending",
    )
    session.add(log)
    await session.flush()   # assigns log.id without ending the transaction
    log_id = log.id
    await session.commit()  # makes the record visible to the background task's session

    logger.info("Ingest accepted: tenant=%r author=%r log_id=%d", tenant_id, body.author, log_id)
    background_tasks.add_task(_ingest_task, tenant_id, body.author, log_id, request.app.state.http)
    return {"job_id": log_id, "tenant_id": tenant_id, "author": body.author, "status": "pending"}
