import hashlib
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.dependencies import resolve_tenant
from app.models import Book, ReadingListBook, ReadingListSubmission

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Reading Lists"], dependencies=[Depends(resolve_tenant)])


def _hash(value: str) -> str:
    """SHA-256 of a pre-normalized string."""
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize(value: str) -> str:
    return value.lower().strip()


class ReadingListRequest(BaseModel):
    name: str
    email: str
    books: list[str]

    @field_validator("name", "email")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("books")
    @classmethod
    def must_have_books(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must contain at least one work ID")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "books": [
                    "/works/OL7924161W",
                    "/works/OL16799133W",
                    "/works/OL99999W",
                ],
            }
        }
    }


class ResolvedBook(BaseModel):
    ol_key: str
    title: str


class ReadingListResponse(BaseModel):
    submission_id: int
    is_update: bool
    resolved: list[ResolvedBook]
    not_found: list[str]

    model_config = {
        "json_schema_extra": {
            "example": {
                "submission_id": 1,
                "is_update": False,
                "resolved": [
                    {"ol_key": "/works/OL7924161W", "title": "A Crown of Swords"},
                    {"ol_key": "/works/OL16799133W", "title": "A Memory of Light"},
                ],
                "not_found": ["/works/OL99999W"],
            }
        }
    }


@router.post(
    "/api/{tenant_id}/reading-lists",
    status_code=201,
    response_model=ReadingListResponse,
    summary="Submit a patron reading list",
    responses={
        201: {"description": "Reading list created or updated"},
    },
)
async def submit_reading_list(
    tenant_id: str,
    body: ReadingListRequest,
    session: AsyncSession = Depends(get_session),
):
    """Submit a patron reading list.

    **PII handling** — name and email are normalized (`lower().strip()`) then
    SHA-256 hashed before any database write. No plaintext is ever stored, logged,
    or returned. The email hash is the dedup key: re-submitting from the same address
    replaces the existing list rather than creating a duplicate (`is_update: true`).

    **Book resolution** — each work ID is looked up in this tenant's local catalog
    (no Open Library calls). `resolved` contains the IDs that matched a known book;
    `not_found` contains IDs not yet in the catalog. Only resolved books are stored.
    """
    name_hash = _hash(_normalize(body.name))
    email_hash = _hash(_normalize(body.email))

    # Find or create the submission record for this patron within this tenant.
    result = await session.execute(
        select(ReadingListSubmission)
        .where(ReadingListSubmission.tenant_id == tenant_id)
        .where(ReadingListSubmission.patron_email_hash == email_hash)
    )
    existing = result.scalar_one_or_none()
    is_update = existing is not None

    if existing:
        existing.patron_name_hash = name_hash
        submission = existing
        session.add(submission)
    else:
        submission = ReadingListSubmission(
            tenant_id=tenant_id,
            patron_name_hash=name_hash,
            patron_email_hash=email_hash,
        )
        session.add(submission)
        await session.flush()  # populate submission.id before inserting books

    # Deduplicate work IDs while preserving submission order.
    ol_keys = list(dict.fromkeys(body.books))

    # Resolve each work ID against the tenant's local catalog — no OL calls at read time.
    books_result = await session.execute(
        select(Book)
        .where(Book.tenant_id == tenant_id)
        .where(Book.ol_key.in_(ol_keys))
    )
    found: dict[str, Book] = {b.ol_key: b for b in books_result.scalars().all()}

    resolved = [ResolvedBook(ol_key=k, title=found[k].title) for k in ol_keys if k in found]
    not_found = [k for k in ol_keys if k not in found]

    # Replace the stored book list with the patron's latest submission.
    await session.execute(
        delete(ReadingListBook).where(ReadingListBook.submission_id == submission.id)
    )
    for book in resolved:
        session.add(ReadingListBook(
            submission_id=submission.id,
            tenant_id=tenant_id,
            ol_key=book.ol_key,
            title=book.title,
        ))

    await session.commit()

    logger.info(
        "Reading list %s: tenant=%r submission_id=%d resolved=%d not_found=%d",
        "updated" if is_update else "created",
        tenant_id, submission.id, len(resolved), len(not_found),
    )
    return ReadingListResponse(
        submission_id=submission.id,
        is_update=is_update,
        resolved=resolved,
        not_found=not_found,
    )
