"""
API tests for the Titan Library Catalog service.

Uses FastAPI's TestClient against the real PostgreSQL database (Docker must be running).
BackgroundTasks that call Open Library are patched so no external HTTP requests are made.
"""
import asyncio
import hashlib
from unittest.mock import AsyncMock, patch

from tests.conftest import PII_TEST_EMAIL, TEST_TENANT
from app.database import AsyncSessionLocal
from app.models import ReadingListSubmission


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200_and_ok_status(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "db": "reachable"}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class TestIngest:
    def test_returns_202_with_pending_job(self, client):
        """Endpoint must return 202 immediately without calling Open Library."""
        with patch("app.routers.ingest._ingest_task", new_callable=AsyncMock):
            response = client.post(
                f"/api/{TEST_TENANT}/ingest",
                json={"author": "Test Author"},
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert isinstance(body["job_id"], int)
        assert body["tenant_id"] == TEST_TENANT
        assert body["author"] == "Test Author"

    def test_unknown_tenant_returns_404(self, client):
        with patch("app.routers.ingest._ingest_task", new_callable=AsyncMock):
            response = client.post(
                "/api/no-such-branch/ingest",
                json={"author": "Anyone"},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_list_books_returns_paginated_shape(self, client, test_books):
        response = client.get(f"/api/{TEST_TENANT}/books?limit=2&offset=0")
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 2
        assert body["offset"] == 0
        assert isinstance(body["total"], int)
        assert body["total"] >= 3  # at least our three test books
        assert len(body["items"]) == 2

    def test_list_books_offset_returns_different_items(self, client, test_books):
        page1 = client.get(f"/api/{TEST_TENANT}/books?limit=2&offset=0").json()
        page2 = client.get(f"/api/{TEST_TENANT}/books?limit=2&offset=2").json()

        ids_p1 = {item["id"] for item in page1["items"]}
        ids_p2 = {item["id"] for item in page2["items"]}
        assert ids_p1.isdisjoint(ids_p2), "Offset pages must not overlap"

    def test_single_book_404_for_missing_id(self, client):
        response = client.get(f"/api/{TEST_TENANT}/books/999999999")
        assert response.status_code == 404
        assert response.json()["detail"] == "Book not found"

    def test_single_book_404_for_wrong_tenant(self, client, test_books):
        # A book that exists under branch-a must not be visible under branch-b.
        book_id = test_books[0]
        response = client.get(f"/api/branch-b/books/{book_id}")
        assert response.status_code == 404

    def test_unknown_tenant_returns_404(self, client):
        response = client.get("/api/no-such-branch/books")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Reading lists — PII handling
# ---------------------------------------------------------------------------

class TestReadingList:
    NAME = "Jane Doe"
    EMAIL = PII_TEST_EMAIL  # defined in conftest so cleanup fixture can key on the hash

    def test_stores_only_hashed_pii(self, client, test_books, cleanup_pii_submission):
        response = client.post(
            f"/api/{TEST_TENANT}/reading-lists",
            json={
                "name": self.NAME,
                "email": self.EMAIL,
                "books": ["/works/TEST001", "/works/TEST002"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        submission_id = body["submission_id"]

        # Fetch the raw DB record — this is what we assert PII against.
        async def _fetch():
            async with AsyncSessionLocal() as session:
                return await session.get(ReadingListSubmission, submission_id)

        record = asyncio.run(_fetch())
        assert record is not None

        expected_name_hash  = _sha256(self.NAME.lower().strip())
        expected_email_hash = _sha256(self.EMAIL.lower().strip())

        # Hashes must match the SHA-256 of the normalised values.
        assert record.patron_name_hash  == expected_name_hash
        assert record.patron_email_hash == expected_email_hash

        # Plaintext must never appear in any stored field.
        assert self.NAME  not in (record.patron_name_hash,  record.patron_email_hash)
        assert self.EMAIL not in (record.patron_name_hash,  record.patron_email_hash)

    def test_same_email_deduplicates(self, client, test_books, cleanup_pii_submission):
        payload = {
            "name": self.NAME,
            "email": self.EMAIL,
            "books": ["/works/TEST001"],
        }
        r1 = client.post(f"/api/{TEST_TENANT}/reading-lists", json=payload)
        r2 = client.post(f"/api/{TEST_TENANT}/reading-lists", json=payload)

        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["is_update"] is False
        assert r2.json()["is_update"] is True
        assert r1.json()["submission_id"] == r2.json()["submission_id"]

    def test_resolved_and_not_found_split(self, client, test_books, cleanup_pii_submission):
        response = client.post(
            f"/api/{TEST_TENANT}/reading-lists",
            json={
                "name": self.NAME,
                "email": self.EMAIL,
                "books": ["/works/TEST001", "/works/DOES_NOT_EXIST"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        resolved_keys = [b["ol_key"] for b in body["resolved"]]
        assert "/works/TEST001" in resolved_keys
        assert "/works/DOES_NOT_EXIST" in body["not_found"]
