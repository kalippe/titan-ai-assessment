# Titan Library Catalog Service

A backend service built for a consortium of public libraries. The consortium operates multiple library branches, each with their own catalog needs and patron base. This service provides a unified, multi-tenant catalog layer that aggregates book data from [Open Library](https://openlibrary.org) — a free, open-source, community-maintained book database — and makes it searchable and browsable for library patrons and staff.

Each library branch is treated as an independent tenant. Tenant data is strictly isolated: one library's catalog and reading lists are never visible in another library's API responses.

## Core Responsibilities

**Catalog Ingestion** — Fetch and store book data from Open Library's public API, scoped to individual library tenants. A librarian triggers ingestion by author; the service fetches matching works and upserts them into that branch's catalog.

**Reading List Submissions** — Library patrons can submit personal reading lists (name, email, and a list of books) through the service. Submissions contain PII that is handled carefully: email is normalized and SHA-256 hashed before storage, and no plaintext is ever written to the database.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Client / API Consumer                │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                      FastAPI Service                        │
│                                                             │
│  ┌─────────────────┐   ┌──────────────────────────────────┐ │
│  │  Catalog Router │   │     Patron Router                │ │
│  │  /catalog/{...} │   │     /patrons/{...}               │ │
│  └────────┬────────┘   └───────────────┬──────────────────┘ │
│           │                            │                     │
│  ┌────────▼────────────────────────────▼──────────────────┐ │
│  │              Tenant Isolation Middleware                │ │
│  │    Resolves X-Tenant-ID header → filters all queries   │ │
│  └────────────────────────────┬───────────────────────────┘ │
│                               │                             │
│  ┌────────────────────────────▼───────────────────────────┐ │
│  │              SQLModel / AsyncSession (asyncpg)          │ │
│  └────────────────────────────┬───────────────────────────┘ │
│                               │                             │
│  ┌────────────────────────────▼───────────────────────────┐ │
│  │         Background Ingestion (FastAPI BackgroundTasks)  │ │
│  │         httpx → Open Library API → catalog upsert       │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │         PostgreSQL 16            │
          │  tenant_id column on every table │
          └─────────────────────────────────┘
```

### Services

| Service | Responsibility |
|---|---|
| **FastAPI** | REST API, request validation, tenant resolution, PII hashing |
| **PostgreSQL** | Persistent storage; all tables carry a `tenant_id` FK |
| **Open Library** (external) | Book metadata source; polled by the ingestion worker |

### Data Flow

1. **Catalog ingestion** — A librarian POSTs `{"author": "..."}` to `/api/{tenant_id}/ingest`. The endpoint creates an `IngestionLog` record (status `pending`), returns `202 Accepted` with a `job_id` immediately, then hands off to a `BackgroundTask`. The background task runs two rounds of Open Library requests: first a search to get the list of works, then concurrent follow-up calls to `/works/{key}.json` (full subjects) and `/authors/{key}.json` (canonical name when missing) for each result. Enriched books are upserted into the tenant's catalog and the log is updated to `success`, `partial`, or `failed`. Progress is pollable via `GET /api/{tenant_id}/ingest-logs/{job_id}`.
2. **Patron submissions** — Patrons POST a reading list. Both name and email are lowercased, stripped, and SHA-256 hashed before any database write. The email hash is the dedup key — re-submitting from the same address updates the existing record. No plaintext PII is ever persisted.
3. **Reads** — All catalog and patron queries are filtered by `tenant_id` from the URL path. Branch A can never see Branch B's books or patron records.

---

## PII Handling Requirements

All patron-submitted personally identifiable information (PII) — name and email — **must be irreversibly hashed before any database write**. No plaintext PII may be written to the database, appear in logs, or be returned by any API response.

### Rules

1. **Hash before persist** — normalize (lowercase + strip whitespace) then SHA-256 hash both name and email before the record is written. This applies to inserts and updates.
2. **Both fields** — `patron_name` and `patron_email` are both treated as PII. Both are hashed. Neither is stored in any readable form.
3. **Email hash as dedup key** — the same email address, regardless of casing or surrounding whitespace, always produces the same hash. This hash is the unique identifier for a patron within a tenant: re-submitting a reading list from the same email updates the existing record rather than creating a duplicate. This is the only form of patron recognition the service supports — no plaintext lookup is ever possible.
4. **No plaintext surface** — the hash cannot be reversed. The service has no decryption path. Patron lookup at every layer (API, DB query, log) operates on the hash only. If account recovery or email display becomes a requirement, the correct solution is envelope encryption with keys managed outside this service.
5. **Normalization consistency** — `lower(strip(value))` must be applied before hashing every time, at every call site, to guarantee that `User@Example.com` and `user@example.com` resolve to the same patron.

### What is stored

| Field | Stored form |
|---|---|
| Patron name | SHA-256 of `lower(strip(name))` |
| Patron email | SHA-256 of `lower(strip(email))` — also the dedup key |
| Reading list books | `ol_key` + title (not PII; captured at submission time) |

---

## Key Design Decisions

### 1. Tenant isolation via `tenant_id` column (row-level) rather than separate schemas

A separate PostgreSQL schema per tenant (schema-per-tenant) gives stronger DDL isolation but makes migrations and connection pooling significantly more complex — each tenant needs its own search path, and tools like Alembic require per-tenant runs. For a library catalog at this scale, row-level isolation with a `tenant_id` column on every table is simpler to operate and still safe, provided all queries go through the middleware filter. A `CHECK` constraint or row-level security policy can be added later if the threat model demands hard schema separation.

### 2. Email hashed (SHA-256 of normalized form) as the sole patron identifier — no plaintext stored

The patron's email is normalized (lowercased, whitespace stripped) and then SHA-256 hashed before any database write. The hash is the dedup key: submitting the same email twice, even with different casing, produces the same hash and updates the existing record rather than creating a duplicate. No plaintext email is ever written to the database, logged, or returned by the API — there is no decryption surface to attack.

The trade-off is irreversibility: the service cannot display or recover the original email. Patron lookup must always re-hash the input. If a reversible identifier becomes necessary (e.g. for account recovery), the solution is envelope encryption with keys managed outside this service — not storage of plaintext.

### 3. Async everywhere (asyncpg + SQLAlchemy async engine)

The ingestion worker and the API share the same asyncio event loop. Using `asyncpg` through SQLAlchemy's async engine means neither HTTP I/O (httpx) nor database I/O blocks the event loop. The alternative — a separate Celery worker with a sync driver — would require a broker (Redis or RabbitMQ) and more infrastructure for what is currently a single background task.

### 4. Background ingestion via FastAPI `BackgroundTasks` to respect Open Library rate limits

Open Library is a free public API with rate limits. Fetching all works for a prolific author and enriching each result with follow-up requests can take tens of seconds and involve hundreds of HTTP calls. Doing this synchronously would block the HTTP connection for the full duration and risk exhausting rate limit allowances if multiple branches trigger ingestion concurrently.

The ingest endpoint creates the `IngestionLog` record (status `pending`) in the request handler, returns `202 Accepted` with the `job_id` immediately, then hands all Open Library work to FastAPI's built-in `BackgroundTasks`. The background task runs after the response is sent within the same worker process. It opens its own DB session (the request-scoped session is already closed by then) and performs two rounds of requests:

1. **Search** — `search.json?author=...` returns up to 100 work stubs.
2. **Enrichment** — concurrent follow-up calls (capped at 5 in-flight at once via a semaphore) to `/works/{key}.json` for the full subjects list and, for any work missing an author name, to `/authors/{key}.json` for the canonical name.

On completion the log is updated to `success`, `partial` (some docs lacked a key or title), or `failed` (upstream error). Progress is pollable via `GET /api/{tenant_id}/ingest-logs/{job_id}`.

This requires no broker, no extra container, and no additional dependencies. The natural upgrade path is ARQ or Celery backed by Redis if retry semantics, per-tenant scheduling, or cross-process fan-out become requirements.

### 5. Single `docker compose up` for local development

The Compose file defines exactly two services: `api` and `db`. The API waits for the database healthcheck before starting. No extra brokers, caches, or sidecars are required to run the service locally, keeping the onboarding path short.

---

## API Endpoints

All tenant-scoped endpoints are prefixed with `/api/{tenant_id}`. Requests for one tenant never return data belonging to another.

### Ingestion

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/{tenant_id}/ingest` | Trigger a background ingest by author. Creates an `IngestionLog` (status `pending`) and returns `202 Accepted` with a `job_id` immediately. The background task hits Open Library's search API then makes concurrent follow-up requests to enrich each work with full subjects and resolved author names before upserting. Body: `{"author": "..."}` |

### Catalog — Retrieval

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/{tenant_id}/books` | List books in the tenant's catalog. Supports `limit`/`offset` pagination and filters: `author`, `subject`, `year_from`, `year_to`. Keyword search across title and author via the `search` query parameter (case-insensitive, combinable with all filters). Returns `{total, limit, offset, items}`. |
| `GET` | `/api/{tenant_id}/books/{book_id}` | Retrieve a single book's full detail including subjects, cover URL, and timestamps. Returns `404` if the book does not exist within this tenant. |

### Activity Log

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/{tenant_id}/ingest-logs` | List all ingestion operations for a tenant, newest first. Each entry records query type and value, counts, timestamps, and any error detail. Supports `status` filter (`pending`, `success`, `partial`, `failed`) and `limit`. |
| `GET` | `/api/{tenant_id}/ingest-logs/{log_id}` | Fetch a single ingest job by the `job_id` returned from `POST /ingest`. Returns the current status (`pending` while running, then `success`, `partial`, or `failed`), counts, and any error detail. Returns `404` if the log does not belong to this tenant. |

### Reading Lists

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/{tenant_id}/reading-lists` | Submit a patron reading list. Accepts patron name, email, and a list of books. Name and email are SHA-256 hashed before storage — no plaintext PII is persisted. Re-submitting from the same email updates the existing record. |

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness and DB reachability check. Returns `{"status": "ok", "db": "reachable"}` or `503` if the database is unavailable. |

---

## Observability & Logging

Every significant operation emits a structured log line via Python's standard `logging` module, including tenant ID, operation type, and outcome (record counts, error messages). Key events covered:

- Service startup and shutdown lifecycle
- Every ingest request: tenant, author, number of records written
- Open Library fetch results and any upstream errors
- Catalog search queries and result counts
- Health check failures with full exception detail

Logs are written to stdout in a consistent `timestamp [LEVEL] module: message` format so they can be collected by any log aggregator without additional parsing configuration.

In a production deployment these logs can be shipped directly into **Splunk** (or equivalent — Datadog, OpenSearch) to build observability dashboards for:

- Ingest pipeline health (failure rate, books ingested per tenant over time)
- Patron submission volume per branch
- API error rates and upstream Open Library latency
- Tenant activity and cross-branch usage patterns

---

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app, lifespan, health endpoint
│   ├── database.py          # Async engine, session factory, get_session dependency
│   ├── models.py            # SQLModel table definitions (Tenant, Book, IngestionLog, ReadingList*)
│   ├── open_library.py      # Open Library search + per-work/per-author enrichment
│   └── routers/
│       ├── ingest.py        # POST /ingest — background task, job ID, upsert
│       ├── ingest_logs.py   # GET /ingest-logs, GET /ingest-logs/{log_id}
│       └── catalog.py       # Book catalog queries
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env
```

---

## Running Locally

```bash
cd backend
docker compose up --build
```

API available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

**Required header on all requests:**
```
X-Tenant-ID: <branch-uuid>
```

**Health check:**
```bash
curl http://localhost:8000/health
# {"status": "ok", "db": "reachable"}
```

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | asyncpg connection string | `postgresql+asyncpg://titan:titan@db:5432/titan` |

---

## What I would do differently if I had more time

1. ARQ over BackgroundTasks
2. Race Conditions
3. Auth
4. Tier 3
5. Finishing creating and verifying the test cases
