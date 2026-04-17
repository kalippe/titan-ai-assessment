import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://openlibrary.org/search.json"
_OL_BASE = "https://openlibrary.org"

# Fields requested from the search endpoint. author_key is needed so we can fall
# back to the author detail endpoint when author_name is absent from a result.
_SEARCH_FIELDS = "key,title,author_name,author_key,first_publish_year,subject,cover_i"

# Max concurrent follow-up requests to Open Library — caps parallel load on their API.
_ENRICH_CONCURRENCY = 5


async def search_works_by_author(client: httpx.AsyncClient, author: str) -> list[dict[str, Any]]:
    """Query Open Library's search API and return raw work documents for an author.

    Returns up to 100 results. The response may have truncated subjects and missing
    author names — call enrich_works() to fill those gaps with follow-up requests.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    params = {"author": author, "fields": _SEARCH_FIELDS, "limit": 100}
    logger.info("Fetching Open Library works: author=%r", author)
    response = await client.get(_SEARCH_URL, params=params, timeout=20.0)
    response.raise_for_status()
    docs = response.json().get("docs", [])
    logger.info("Open Library returned %d docs for author=%r", len(docs), author)
    return docs


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    """Fetch a single JSON resource from Open Library; returns None on any error."""
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Open Library fetch failed: url=%r error=%s", url, exc)
        return None


async def enrich_works(
    client: httpx.AsyncClient,
    docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enrich search results with full work details and resolved author names.

    Two rounds of follow-up requests run concurrently (bounded by _ENRICH_CONCURRENCY):

    1. Work detail  — GET /works/{key}.json — gives the complete subjects list.
       The search endpoint truncates subjects; the work endpoint has the full set.
       The work endpoint also carries a covers list used as a fallback when the
       search result has no cover_i.

    2. Author detail — GET /authors/{key}.json — resolves the canonical author name
       for any work that came back from search without an author_name value.

    Results are merged back into the original docs, preferring the richer data.
    """
    sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    async def guarded(coro):
        async with sem:
            return await coro

    # Round 1: fetch full work detail for every doc that has a key.
    work_coros = [
        guarded(_fetch_json(client, f"{_OL_BASE}{doc['key']}.json"))
        if doc.get("key")
        else asyncio.coroutine(lambda: None)()
        for doc in docs
    ]
    work_details: list[dict | None] = list(await asyncio.gather(*work_coros))

    # Round 2: fetch author detail only for the unique author keys that are missing
    # a resolved name in the search response.
    missing_keys: set[str] = set()
    for doc in docs:
        if not doc.get("author_name"):
            missing_keys.update(doc.get("author_key") or [])

    author_name_map: dict[str, str] = {}
    if missing_keys:
        author_results = await asyncio.gather(*(
            guarded(_fetch_json(client, f"{_OL_BASE}{key}.json"))
            for key in missing_keys
        ))
        for key, data in zip(missing_keys, author_results):
            if data and data.get("name"):
                author_name_map[key] = data["name"]
        if author_name_map:
            logger.info("Resolved %d missing author names from author endpoint", len(author_name_map))

    # Merge enriched data back into each doc.
    enriched: list[dict[str, Any]] = []
    for doc, detail in zip(docs, work_details):
        merged = dict(doc)

        if detail:
            # Prefer the full subjects list from the work endpoint.
            if detail.get("subjects"):
                merged["subject"] = detail["subjects"]

            # Fall back to work-endpoint covers if search had no cover_i.
            if not merged.get("cover_i") and detail.get("covers"):
                merged["cover_i"] = detail["covers"][0]

        # Resolve author name from fetched map when search didn't include it.
        if not merged.get("author_name"):
            primary_key = (merged.get("author_key") or [None])[0]
            if primary_key and primary_key in author_name_map:
                merged["author_name"] = [author_name_map[primary_key]]

        enriched.append(merged)

    return enriched
