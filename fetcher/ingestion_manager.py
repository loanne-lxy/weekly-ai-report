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
        """
        分阶段抓取：
          Stage 1: 稳定源（RSS / GitHub / arXiv）全量抓取。
          Stage 2: Exa 搜索源，先搜 URL 再跟稳定源做预去重，只深抓新 URL。
        """
        active = [s for s in sources if s.get("active", True)]
        logger.info(f"IngestionManager: {len(active)} active sources, concurrency={self.concurrency}")

        # 分拆稳定源和搜索源
        stable_sources = [s for s in active if s.get("type") != "exa_search"]
        search_sources = [s for s in active if s.get("type") == "exa_search"]

        connector = aiohttp.TCPConnector(limit=self.concurrency)
        sem = asyncio.Semaphore(self.concurrency)
        stats = {"success": 0, "failed": 0, "total": 0}
        all_articles: list[RawArticle] = []

        async with aiohttp.ClientSession(connector=connector) as session:
            # ── Stage 1: 稳定源全量抓取 ──
            logger.info(f"Stage 1: fetching {len(stable_sources)} stable sources (RSS/GitHub/arXiv)")
            stable_urls: set[str] = set()
            tasks = [self._fetch_one(session, sem, source, stats) for source in stable_sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    stable_urls.update(a.url for a in r if a.url)
                    all_articles.extend(r)
                elif isinstance(r, Exception):
                    logger.error(f"Stage 1 unhandled error: {r}")

            logger.info(f"Stage 1 done: {len(all_articles)} articles, {len(stable_urls)} unique URLs")

            # ── Stage 2: Exa 搜索源（带预去重） ──
            if search_sources:
                logger.info(f"Stage 2: fetching {len(search_sources)} Exa search sources "
                            f"(will dedup against {len(stable_urls)} existing URLs)")
                tasks = [self._fetch_one(session, sem, source, stats, stable_urls)
                         for source in search_sources]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        all_articles.extend(r)
                    elif isinstance(r, Exception):
                        logger.error(f"Stage 2 unhandled error: {r}")

        logger.info(f"IngestionManager done: {len(all_articles)} total articles, stats={stats}")
        return all_articles

    async def _fetch_one(self, session, sem, source: dict, stats: dict,
                         existing_urls: set | None = None) -> list[RawArticle]:
        """抓取单个源。"""
        original_type = source.get("type", "rss")
        url = source.get("url", "")
        extractor_type = original_type
        pydantic_type = {"arxiv": "arxiv", "github": "github", "hf": "hf",
                         "rsshub": "web", "web": "web", "rss": "web",
                         "exa_search": "web"}.get(extractor_type, "web")
        extractor = get_extractor(extractor_type)
        stats["total"] += 1

        async with sem:
            try:
                # Exa 搜索源传入 existing_urls 做预去重
                if extractor_type == "exa_search":
                    timeout_secs = 300  # Exa 搜+深抓 50 篇网页需要时间
                    raw = await asyncio.wait_for(
                        extractor.extract(session, source, existing_urls),
                        timeout=timeout_secs,
                    )
                else:
                    raw = await asyncio.wait_for(
                        extractor.extract(session, source),
                        timeout=self.timeout,
                    )
                default_cat = source.get("default_category")
                validated = BaseExtractor.batch_validate([
                    {
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "summary": r.get("summary", ""),
                        "published": r.get("published"),
                        "author": r.get("author"),
                        "source_name": source.get("name", ""),
                        "source_type": pydantic_type,
                        "default_category": default_cat,
                        "feed_url": source.get("url"),
                        "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit(pydantic_type)],
                        "raw_extra": {k: v for k, v in r.items()
                                      if k not in {"url", "title", "summary", "published", "author"}},
                    } for r in raw
                ])
                if validated:
                    logger.info(f"[{extractor_type}] {source.get('name', '?')}: {len(validated)} articles")
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
                return validated or []
            except asyncio.TimeoutError:
                logger.warning(f"[{extractor_type}] {source.get('name', '?')}: timeout after {self.timeout}s")
                stats["failed"] += 1
                return []
            except Exception as e:
                logger.warning(f"[{extractor_type}] {source.get('name', '?')}: {e}")
                stats["failed"] += 1
                return []

    @staticmethod
    def list_extractors() -> dict[str, str]:
        """列出所有已注册的 extractor。"""
        return {k: v.__class__.name for k, v in EXTRACTOR_REGISTRY.items()}
