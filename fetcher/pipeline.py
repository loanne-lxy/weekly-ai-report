"""Shared fetch pipeline — retry, cache, dedup, normalize, orchestrate

All extractors share this infrastructure. Adding a new platform = one extractor class.
"""
import asyncio
import hashlib
import logging
from typing import Optional

import aiohttp

from fetcher.extractors import get_extractor
from dedup.curator_cache import CuratorCache

logger = logging.getLogger(__name__)
USER_AGENT = "Weekly-AI-Report-Agent/1.0"

# ── Unified Article Schema ────────────────────────────────────────

def _article_id(title: str, url: str) -> str:
    return hashlib.sha256(f"{title}{url}".encode()).hexdigest()[:12]


def normalize(raw: dict, source: dict, connector_name: str) -> dict:
    """Convert raw item from extractor to unified Article Schema."""
    return {
        "id": _article_id(raw.get("title", ""), raw.get("url", "")),
        "title": raw.get("title", ""),
        "url": raw.get("url", ""),
        "summary": raw.get("summary", ""),
        "published": raw.get("published", ""),
        "source_name": source.get("name", ""),
        "source_category": source.get("category", ""),
        "source_type": source.get("type", ""),
        "connector": connector_name,
    }


# ── Retry Helper ──────────────────────────────────────────────────

async def fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = 3,
    timeout: int = 30,
) -> Optional[aiohttp.ClientResponse]:
    """GET with exponential backoff (1s/2s/4s). Returns response or None."""
    for attempt in range(max_retries):
        try:
            resp = await session.get(
                url, timeout=timeout,
                headers=headers or {"User-Agent": USER_AGENT}
            )
            if resp.status < 500:
                return resp
            logger.debug(f"Retry {attempt+1}/{max_retries}: HTTP {resp.status} from {url}")
        except Exception:
            logger.debug(f"Retry {attempt+1}/{max_retries}: connection error for {url}")
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)
    return None


# ── Fetch All Orchestrator ────────────────────────────────────────

async def fetch_all(sources: list[dict], concurrency: int = 10) -> list[dict]:
    """Concurrently fetch all active sources via extractors + pipeline."""
    active = [s for s in sources if s.get("active", True)]
    connector = aiohttp.TCPConnector(limit=concurrency)
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(connector=connector) as session:
        async def fetch_one(source: dict) -> list[dict]:
            async with sem:
                extractor = get_extractor(source.get("type", "rss"))
                name = extractor.name
                try:
                    raw_items = await extractor.extract(session, source)
                except Exception as e:
                    logger.warning(f"[{name}] {source.get('name', '?')}: {e}")
                    return []
                return [normalize(r, source, name) for r in raw_items]

        tasks = [fetch_one(s) for s in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
    return all_articles
