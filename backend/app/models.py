from datetime import datetime

from sqlalchemy import Column, DateTime, JSON, UniqueConstraint, func
from sqlmodel import Field, SQLModel


class Tenant(SQLModel, table=True):
    """Represents a single library branch in the consortium.

    The slug is the URL-safe identifier used as tenant_id throughout the API
    (e.g. 'branch-a'). All other tables reference this value to scope their data.
    """

    __tablename__ = "tenants"

    id: int | None = Field(default=None, primary_key=True)

    # URL-safe identifier, matches the {tenant_id} path parameter in all routes.
    slug: str = Field(unique=True, index=True)

    # Human-readable branch name for display purposes (e.g. "Central Library").
    name: str

    # Set by the DB on insert; marks when the tenant was first registered.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )

    # Updated on any modification to the tenant record.
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )


class Book(SQLModel, table=True):
    """Catalog entry for a single book, scoped to a library branch tenant.

    ol_key is the Open Library work identifier (e.g. '/works/OL123W').
    The unique constraint on (tenant_id, ol_key) ensures the same work
    can exist in multiple tenants but never duplicated within one.
    """

    __tablename__ = "books"
    __table_args__ = (UniqueConstraint("tenant_id", "ol_key", name="uq_book_tenant"),)

    id: int | None = Field(default=None, primary_key=True)

    # Branch this book belongs to. Matches Tenant.slug.
    tenant_id: str = Field(index=True, foreign_key="tenants.slug")

    # Open Library work key (e.g. '/works/OL123W'). Unique within a tenant.
    ol_key: str

    # Book title as returned by Open Library.
    title: str

    # Primary author name. Open Library returns a list; we store only the first.
    author: str | None = None

    # Year the work was first published, as reported by Open Library.
    first_publish_year: int | None = None

    # Stored as a JSON array of strings (e.g. ["Fiction", "Science"]).
    subjects: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    # Full-size cover image URL built from Open Library's cover CDN (cover_i field).
    cover_url: str | None = None

    # Set by the DB on insert; never overwritten on upsert so the original ingestion date is preserved.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )

    # Updated by the DB on every upsert conflict via the explicit set_ dict in the ingest router.
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )


class ReadingListSubmission(SQLModel, table=True):
    """A patron's reading list submission, scoped to a library branch tenant.

    No plaintext PII is stored. Both patron name and email are SHA-256 hashed
    after normalization (lowercased, whitespace stripped) before being written.
    The email hash doubles as the dedup key — re-submitting from the same address
    updates the existing record rather than creating a duplicate.
    """

    __tablename__ = "reading_list_submissions"
    __table_args__ = (
        # Dedup: one active submission per patron (email hash) per tenant.
        UniqueConstraint("tenant_id", "patron_email_hash", name="uq_submission_tenant_patron"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # Branch this submission belongs to. Matches Tenant.slug.
    tenant_id: str = Field(index=True, foreign_key="tenants.slug")

    # SHA-256 of the patron's name after lowercasing and stripping whitespace.
    patron_name_hash: str

    # SHA-256 of the patron's email after lowercasing and stripping whitespace.
    # Used as the dedup key — see unique constraint above.
    patron_email_hash: str = Field(index=True)

    # Set by the DB on insert; marks when the submission was first received.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )

    # Updated when the patron re-submits (upsert on email hash conflict).
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class IngestionLog(SQLModel, table=True):
    """Audit record for every ingest operation, written regardless of outcome.

    Captures enough detail to diagnose failures and track pipeline throughput
    per tenant over time. Intended to be queryable via the ingest-logs endpoint
    and shippable to log aggregators (e.g. Splunk) for dashboard metrics.
    """

    __tablename__ = "ingestion_logs"

    id: int | None = Field(default=None, primary_key=True)

    # Branch that triggered the ingest. Matches Tenant.slug.
    tenant_id: str = Field(index=True, foreign_key="tenants.slug")

    # Category of the search query sent to Open Library (e.g. "author").
    query_type: str

    # The value passed to Open Library (e.g. "jordan").
    query_value: str

    # Lifecycle state: "pending" while the background task is running,
    # then "success", "partial", or "failed" once it completes.
    # "partial" means Open Library responded but some docs were dropped (missing key/title).
    status: str = Field(index=True)

    # Total number of docs returned by Open Library before filtering.
    fetched_count: int = Field(default=0)

    # Number of docs that passed validation and were upserted into the catalog.
    success_count: int = Field(default=0)

    # Number of docs dropped due to missing required fields (key or title).
    fail_count: int = Field(default=0)

    # Populated when status is "failed"; contains the exception message.
    error_detail: str | None = None

    # Set by the DB on insert; used to query log history chronologically.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )

    # Updated by the DB whenever the background task writes final counts or error detail.
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class ReadingListBook(SQLModel, table=True):
    """An individual book entry within a patron's reading list submission.

    tenant_id is denormalized here (it could be derived via submission_id → tenant_id)
    so that tenant-scoped queries on reading list books do not require a join.
    ol_key and title are both stored: ol_key links back to the catalog, title is
    captured at submission time so the record remains meaningful if the book is
    later removed from the catalog.
    """

    __tablename__ = "reading_list_books"

    id: int | None = Field(default=None, primary_key=True)

    # The submission this book belongs to.
    submission_id: int = Field(index=True, foreign_key="reading_list_submissions.id")

    # Denormalized branch identifier; allows direct tenant-scoped queries without a join.
    tenant_id: str = Field(index=True, foreign_key="tenants.slug")

    # Open Library work key. May not exist in this tenant's catalog if not yet ingested.
    ol_key: str

    # Title captured at submission time; not updated if the catalog entry changes later.
    title: str

    # Set by the DB on insert.
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
