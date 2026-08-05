"""Unified fetch orchestrator — delegates to Connector Registry

Design:
  Source Pool → Connector Registry (fetcher/connectors.py) → Unified Articles
  Adding a new source type = adding a Connector class. No pipeline changes.
"""
import asyncio
import logging
import aiohttp
from fetcher.connectors import get_connector

logger = logging.getLogger(__name__)


async def fetch_all(sources: list[dict], concurrency: int = 10) -> list[dict]:
    """Concurrently fetch all active sources via their connectors."""
    active = [s for s in sources if s.get("active", True)]
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(source: dict) -> list[dict]:
            async with sem:
                c = get_connector(source.get("type", "rss"))
                try:
                    return await c.fetch(session, source)
                except Exception as e:
                    logger.warning(f"[{c.name}] {source.get('name', '?')}: {e}")
                    return []

        tasks = [fetch_one(s) for s in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
    return all_articles
