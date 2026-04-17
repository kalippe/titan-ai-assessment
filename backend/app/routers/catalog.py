import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.dependencies import resolve_tenant
from app.models import Book

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Catalog"], dependencies=[Depends(resolve_tenant)])


class BookRead(BaseModel):
    id: int
    ol_key: str
    title: str
    author: str | None
    first_publish_year: int | None
    subjects: list[str] | None
    cover_url: str | None
    created_at: datetime | None
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class BookPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BookRead]


@router.get(
    "/api/{tenant_id}/books",
    response_model=BookPage,
    summary="List and search the tenant's book catalog",
)
async def list_books(
    tenant_id: str,
    search: str | None = Query(
        default=None,
        description="Keyword search across title and author name (case-insensitive)",
    ),
    author: str | None = Query(
        default=None,
        description="Filter by author name (case-insensitive substring match)",
    ),
    subject: str | None = Query(
        default=None,
        description="Filter by subject (case-insensitive substring match against the subjects list)",
    ),
    year_from: int | None = Query(
        default=None,
        description="Include only books first published on or after this year",
    ),
    year_to: int | None = Query(
        default=None,
        description="Include only books first published on or before this year",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum number of results to return"),
    offset: int = Query(default=0, ge=0, description="Number of results to skip"),
    session: AsyncSession = Depends(get_session),
):
    """List books in this tenant's catalog with optional filtering and pagination.

    All filters are combinable. `search` is a broad keyword match across title and author;
    `author` and `subject` are narrower field-specific filters. Year range narrows by
    `first_publish_year`. Results are ordered by title. Tenant isolation is enforced on
    every query — another tenant's books are never reachable via this endpoint.
    All data is served from the local database — no Open Library calls at read time.
    """
    stmt = select(Book).where(Book.tenant_id == tenant_id)

    if search:
        stmt = stmt.where(Book.title.ilike(f"%{search}%") | Book.author.ilike(f"%{search}%"))
    if author:
        stmt = stmt.where(Book.author.ilike(f"%{author}%"))
    if subject:
        # subjects is a JSON array; cast to text for case-insensitive substring match.
        stmt = stmt.where(cast(Book.subjects, String).ilike(f"%{subject}%"))
    if year_from is not None:
        stmt = stmt.where(Book.first_publish_year >= year_from)
    if year_to is not None:
        stmt = stmt.where(Book.first_publish_year <= year_to)

    total_result = await session.execute(select(func.count()).select_from(stmt.subquery()))
    total = total_result.scalar_one()

    stmt = stmt.order_by(Book.title).offset(offset).limit(limit)
    result = await session.execute(stmt)
    books = result.scalars().all()

    logger.info(
        "Catalog list: tenant=%r filters=(search=%r author=%r subject=%r years=%r-%r) "
        "limit=%d offset=%d total=%d returned=%d",
        tenant_id, search, author, subject, year_from, year_to, limit, offset, total, len(books),
    )
    return BookPage(total=total, limit=limit, offset=offset, items=books)


@router.get(
    "/api/{tenant_id}/books/{book_id}",
    response_model=BookRead,
    summary="Get full detail for a single book",
    responses={404: {"description": "Book not found in this tenant's catalog"}},
)
async def get_book(
    tenant_id: str,
    book_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return full detail for a single book by its internal ID.

    Returns 404 if the book does not exist or belongs to a different tenant —
    the two cases are intentionally indistinguishable to prevent catalog enumeration
    across tenant boundaries.
    """
    book = await session.get(Book, book_id)
    if book is None or book.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Book not found")

    logger.info("Book detail: tenant=%r book_id=%d title=%r", tenant_id, book_id, book.title)
    return book
