"""统一调度中心 — 读 sources.yaml → 路由到 extractor → 返回 RawArticle 列表。

Simplified: uses direct import from extractors module (mature tool wrappers).
No adapter wrapping needed — each extractor already outputs dict compatible with RawArticle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from extractors.contract import RawArticle
from fetcher.base_extractor import BaseExtractor
from fetcher.extractors import get_extractor, EXTRACTOR_REGISTRY

logger = logging.getLogger(__name__)


class IngestionManager:
    """
    统一调度中心。

    Usage:
        manager = IngestionManager(concurrency=10)
        articles = await manager.fetch(sources)
    """

    def __init__(self, concurrency: int = 10, timeout: int = 30):
        self.concurrency = concurrency
        self.timeout = timeout

    async def fetch(self, sources: list[dict]) -> list[RawArticle]:
        """并发抓取所有活跃源，返回标准化 RawArticle 列表。"""
        active = [s for s in sources if s.get("active", True)]
        logger.info(f"IngestionManager: {len(active)} active sources, concurrency={self.concurrency}")

        connector = aiohttp.TCPConnector(limit=self.concurrency)
        sem = asyncio.Semaphore(self.concurrency)
        stats = {"success": 0, "failed": 0, "total": 0}

        async with aiohttp.ClientSession(connector=connector) as session:
            async def fetch_one(source: dict) -> list[RawArticle]:
                original_type = source.get("type", "rss")
                url = source.get("url", "")

                # 1) Choose extractor by original type (registry keys: rss, web, github, arxiv)
                #    RSS feeds → feedparser (fast), NOT trafilatura (slow web scraping)
                #    arxiv library has 3s built-in delays per request — avoid for RSS feeds
                extractor_type = original_type  # "rss", "web", "github", etc.

                # 2) Pydantic source_type must match pattern ^(github|hf|wechat|web|arxiv)$
                #    Map: rss→web, hf→hf, everything else→web
                pydantic_type = {"arxiv": "arxiv", "github": "github", "hf": "hf",
                                 "rsshub": "web", "web": "web", "rss": "web"}.get(extractor_type, "web")

                extractor = get_extractor(extractor_type)
                stats["total"] += 1

                async with sem:
                    try:
                        raw = await asyncio.wait_for(
                            extractor.extract(session, source),
                            timeout=self.timeout,
                        )
                        # Validate as RawArticle
                        validated = BaseExtractor.batch_validate([
                            {
                                "url": r.get("url", ""),
                                "title": r.get("title", ""),
                                "summary": r.get("summary", ""),
                                "published": r.get("published"),
                                "author": r.get("author"),
                                "source_name": source.get("name", ""),
                                "source_type": pydantic_type,
                                "feed_url": source.get("url"),
                                "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit(pydantic_type)],
                                "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                            } for r in raw
                        ])
                        if validated:
                            logger.info(f"[{extractor_type}] {source.get('name', '?')}: {len(validated)} articles")
                            stats["success"] += 1
                        else:
                            stats["failed"] += 1
                        return validated
                    except asyncio.TimeoutError:
                        logger.warning(f"[{extractor_type}] {source.get('name', '?')}: timeout after {self.timeout}s")
                        stats["failed"] += 1
                        return []
                    except Exception as e:
                        logger.warning(f"[{extractor_type}] {source.get('name', '?')}: {e}")
                        stats["failed"] += 1
                        return []

            tasks = [fetch_one(s) for s in active]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_articles: list[RawArticle] = []
        for r in results:
            if isinstance(r, list):
                all_articles.extend(r)
            elif isinstance(r, Exception):
                logger.error(f"IngestionManager unhandled error: {r}")

        logger.info(f"IngestionManager done: {len(all_articles)} articles, stats={stats}")
        return all_articles

    @staticmethod
    def list_extractors() -> dict[str, str]:
        """列出所有已注册的 extractor。"""
        return {k: v.__class__.name for k, v in EXTRACTOR_REGISTRY.items()}
