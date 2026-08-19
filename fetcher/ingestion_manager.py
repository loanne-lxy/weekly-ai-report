"""统一调度中心 — 读 sources.yaml → 路由到 extractor → 返回 RawArticle 列表。

架构:
  Source Registry → IngestionManager → FetchManager (HTTP/Retry/Cache/RateLimit) → Connector
  ───────────────   ────────────────   ──────────────────────────────────────────  ─────────
  源配置            调度器              HTTP 基础设施 (统一)                      解析协议

IngestionManager 职责:
  1. 按 connector 路由到正确的 extractor
  2. 注入 FetchManager (所有 HTTP 经过统一基础设施层)
  3. 分阶段抓取 (稳定源 → Exa 搜索源)
  4. RawArticle 校验 (通过 BaseExtractor.batch_validate)
  5. Post-filter: 按关键词和时间过滤（如 arXiv 源过滤不相关领域）
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from extractors.contract import RawArticle
from fetcher.base_extractor import BaseExtractor
from fetcher.connector_registry import connector_registry
from fetcher.fetch_manager import FetchManager

logger = logging.getLogger(__name__)


def _apply_post_filter(
    articles: list[RawArticle],
    post_filter: dict,
) -> list[RawArticle]:
    """Apply post-filter to articles based on keywords and age.
    
    Args:
        articles: Raw articles to filter
        post_filter: Dict with optional keys:
            - max_age_days: int - filter out articles older than N days
            - include_keywords: list[str] - keep articles matching ANY keyword
            - exclude_keywords: list[str] - drop articles matching ANY keyword
    """
    if not post_filter:
        return articles
    
    filtered = []
    now = datetime.now(timezone.utc)
    max_age = post_filter.get("max_age_days")
    include = [re.compile(k, re.IGNORECASE) for k in post_filter.get("include_keywords", [])]
    exclude = []
    for kw in post_filter.get("exclude_keywords", []):
        # Short keywords (≤4 chars) use word boundaries to avoid substring matches
        if len(kw) <= 4:
            exclude.append(re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE))
        else:
            exclude.append(re.compile(re.escape(kw), re.IGNORECASE))
    
    for article in articles:
        # Age filter
        if max_age:
            pub = article.published
            if pub:
                if isinstance(pub, str):
                    try:
                        pub = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pub = None
                if isinstance(pub, datetime):
                    if not pub.tzinfo:
                        pub = pub.replace(tzinfo=timezone.utc)
                    if (now - pub).days > max_age:
                        continue
        
        # Text to match against
        text = f"{article.title or ''} {article.summary or ''} {article.content_preview or ''}".lower()
        
        # Exclude first (higher priority)
        if exclude and any(pat.search(text) for pat in exclude):
            continue
        
        # Include filter (must match at least one)
        if include and not any(pat.search(text) for pat in include):
            continue
        
        filtered.append(article)
    
    if len(filtered) < len(articles):
        logger.info(
            f"Post-filter: {len(articles)} → {len(filtered)} articles "
            f"(dropped {len(articles) - len(filtered)})"
        )
    
    return filtered


class IngestionManager:
    """
    统一调度中心。

    Usage:
        async with FetchManager(concurrency=10) as fm:
            manager = IngestionManager(fetch_manager=fm)
            articles = await manager.fetch(sources)

        # Or without explicit FetchManager (auto-creates one)
        manager = IngestionManager(concurrency=10)
        articles = await manager.fetch(sources)
    """

    def __init__(
        self,
        concurrency: int = 10,
        timeout: int = 30,
        fetch_manager: FetchManager | None = None,
    ):
        self.concurrency = concurrency
        self.timeout = timeout
        self.fetch_manager = fetch_manager

    async def fetch(self, sources: list[dict]) -> list[RawArticle]:
        """
        分阶段抓取：
          Stage 1: 稳定源（RSS / GitHub / arXiv）全量抓取。
          Stage 2: Exa 搜索源，先搜 URL 再跟稳定源做预去重，只深抓新 URL。
        """
        active = [s for s in sources if s.get("active") is not False]
        logger.info(
            f"IngestionManager: {len(active)} active sources, "
            f"concurrency={self.concurrency}"
        )

        # Split stable vs search sources (support both new 'connector' and old 'type')
        stable_sources = [
            s for s in active
            if s.get("connector") != "exa_search" and s.get("type") != "exa_search"
        ]
        search_sources = [
            s for s in active
            if s.get("connector") == "exa_search" or s.get("type") == "exa_search"
        ]

        stats = {"success": 0, "failed": 0, "total": 0}
        all_articles: list[RawArticle] = []

        # Use FetchManager if available, else create one
        fm = self.fetch_manager
        if fm is None:
            fm = FetchManager(concurrency=self.concurrency)
            await fm.start()
            created = True
        else:
            created = False

        try:
            # ── Stage 1: 稳定源全量抓取 ──
            logger.info(
                f"Stage 1: fetching {len(stable_sources)} stable sources "
                f"(RSS/GitHub/arXiv)"
            )
            stable_urls: set[str] = set()
            tasks = [
                self._fetch_one(fm, source, stats)
                for source in stable_sources
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    stable_urls.update(a.url for a in r if a.url)
                    all_articles.extend(r)
                elif isinstance(r, Exception):
                    logger.error(f"Stage 1 unhandled error: {r}")

            logger.info(
                f"Stage 1 done: {len(all_articles)} articles, "
                f"{len(stable_urls)} unique URLs"
            )

            # ── Stage 2: Exa 搜索源（带预去重） ──
            if search_sources:
                logger.info(
                    f"Stage 2: fetching {len(search_sources)} Exa search sources "
                    f"(will dedup against {len(stable_urls)} existing URLs)"
                )
                tasks = [
                    self._fetch_one(fm, source, stats, stable_urls)
                    for source in search_sources
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        all_articles.extend(r)
                    elif isinstance(r, Exception):
                        logger.error(f"Stage 2 unhandled error: {r}")

        finally:
            if created:
                await fm.close()

        # ── Health report ──
        dead_domains = [
            d for d, n in fm.domain_health().items() if n >= 3
        ]
        if dead_domains:
            logger.warning(
                f"Domains with persistent failures: {dead_domains}"
            )

        logger.info(
            f"IngestionManager done: {len(all_articles)} total articles, "
            f"stats={stats}"
        )
        return all_articles

    async def _fetch_one(
        self,
        fetch_manager: FetchManager,
        source: dict,
        stats: dict,
        existing_urls: set | None = None,
    ) -> list[RawArticle]:
        """抓取单个源，注入 FetchManager."""
        # Support both new schema (connector) and old schema (type)
        original_type = source.get("connector") or source.get("type", "rss")
        endpoint = source.get("endpoint") or source.get("url", "")

        extractor = connector_registry.get_extractor(original_type)
        stats["total"] += 1

        # Timeout: Exa gets more time (search + deep fetch)
        timeout_secs = (
            300 if original_type == "exa_search" else self.timeout
        )

        try:
            # Build extract kwargs — existing_urls only for exa_search
            extract_kwargs = {
                "session": fetch_manager._session,  # type: ignore[arg-type]
                "source": source,
                "fetch_manager": fetch_manager,
            }
            if original_type == "exa_search" and existing_urls:
                extract_kwargs["existing_urls"] = existing_urls

            raw = await asyncio.wait_for(
                extractor.extract(**extract_kwargs),
                timeout=timeout_secs,
            )

            # Map to RawArticle via Pydantic validation
            default_cat = source.get("category") or source.get(
                "default_category"
            )
            source_id = source.get("id", "")

            # Determine pydantic source_type
            pydantic_type = {
                "arxiv": "arxiv",
                "github": "github",
                "hf": "hf",
                "rsshub": "web",
                "web": "web",
                "rss": "web",
                "exa_search": "web",
            }.get(original_type, "web")

            validated = BaseExtractor.batch_validate([
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_id": source_id,
                    "source_name": source.get("name", ""),
                    "source_type": pydantic_type,
                    "default_category": default_cat,
                    "content_preview": r.get("summary", "")[
                        :BaseExtractor.get_preview_limit(pydantic_type)
                    ],
                    "raw_extra": {
                        k: v for k, v in r.items()
                        if k not in {
                            "url", "title", "summary",
                            "published", "author",
                        }
                    },
                } for r in raw
            ])

            if validated:
                # Apply post-filter (keywords + age)
                post_filter = source.get("post_filter", {})
                if post_filter:
                    validated = _apply_post_filter(validated, post_filter)
                
                logger.info(
                    f"[{original_type}] {source.get('name', '?')}: "
                    f"{len(validated)} articles"
                )
                stats["success"] += 1
            else:
                stats["failed"] += 1
            return validated or []

        except asyncio.TimeoutError:
            logger.warning(
                f"[{original_type}] {source.get('name', '?')}: "
                f"timeout after {timeout_secs}s"
            )
            stats["failed"] += 1
            return []
        except Exception as e:
            logger.warning(
                f"[{original_type}] {source.get('name', '?')}: {e}"
            )
            stats["failed"] += 1
            return []

    @staticmethod
    def list_extractors() -> dict[str, str]:
        """列出所有已注册的 extractor。"""
        return {
            name: connector_registry.get_extractor(name).name
            for name in connector_registry.available_names()
        }
