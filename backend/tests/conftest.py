import asyncio
import hashlib
import os

# Must be set before any app import — database.py reads DATABASE_URL at module load
# time to create the engine. Tests connect to the published host port rather than
# the Docker-internal hostname used by the api container.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://titan:titan@localhost:5432/titan",
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import select

import app.models  # noqa: F401 — registers all SQLModel tables before the client starts
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Book, ReadingListBook, ReadingListSubmission

TEST_TENANT = "branch-a"
TEST_OL_KEYS = ["/works/TEST001", "/works/TEST002", "/works/TEST003"]

# Stable test identity used by the PII test and its cleanup fixture.
PII_TEST_EMAIL = "pii_test@titan.local"
PII_TEST_EMAIL_HASH = hashlib.sha256(PII_TEST_EMAIL.encode()).hexdigest()


@pytest.fixture(scope="module")
def client():
    """Single TestClient shared across all tests in the module.

    The FastAPI lifespan runs once: DB tables are created, tenants are seeded,
    and the shared httpx client is initialised. Module scope avoids repeated
    startup/shutdown per test.
    """
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# asyncio.run() is safe in pytest fixtures: fixtures run in the main thread,
# which has no running event loop. TestClient manages its own loop in a
# background thread independently.
# ---------------------------------------------------------------------------

async def _delete_test_books() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Book)
            .where(Book.tenant_id == TEST_TENANT)
            .where(Book.ol_key.in_(TEST_OL_KEYS))
        )
        for book in result.scalars().all():
            await session.delete(book)
        await session.commit()


async def _insert_test_books() -> list[int]:
    async with AsyncSessionLocal() as session:
        books = [
            Book(tenant_id=TEST_TENANT, ol_key="/works/TEST001",
                 title="Alpha Book", author="Alice Author", first_publish_year=1990),
            Book(tenant_id=TEST_TENANT, ol_key="/works/TEST002",
                 title="Beta Book", author="Bob Writer", first_publish_year=2000),
            Book(tenant_id=TEST_TENANT, ol_key="/works/TEST003",
                 title="Gamma Book", author="Alice Author", first_publish_year=2010),
        ]
        for b in books:
            session.add(b)
        await session.flush()
        ids = [b.id for b in books]
        await session.commit()
        return ids


@pytest.fixture
def test_books():
    """Insert three test books into branch-a; remove them after the test.

    Pre-cleans any leftovers from a previously interrupted run so tests
    always start from a known state.
    """
    asyncio.run(_delete_test_books())
    book_ids = asyncio.run(_insert_test_books())
    yield book_ids
    asyncio.run(_delete_test_books())


@pytest.fixture
def cleanup_pii_submission():
    """Remove the PII test submission (keyed on PII_TEST_EMAIL_HASH) after the test."""
    yield
    async def _cleanup():
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ReadingListSubmission)
                .where(ReadingListSubmission.tenant_id == TEST_TENANT)
                .where(ReadingListSubmission.patron_email_hash == PII_TEST_EMAIL_HASH)
            )
            sub = result.scalar_one_or_none()
            if sub:
                await session.execute(
                    delete(ReadingListBook)
                    .where(ReadingListBook.submission_id == sub.id)
                )
                await session.delete(sub)
            await session.commit()
    asyncio.run(_cleanup())
