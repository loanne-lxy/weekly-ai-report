"""
统一调度中心 — 读配置 → 路由到各 extractor → 校验输出 → 返回标准化 RawArticle 列表。

职责：
1. 读取 sources.yaml，按 type 路由到对应 extractor
2. 并发调度，控制并发数
3. 统一校验输出为 RawArticle
4. 汇总统计日志

不做的：
- 去重（由 dedup 层处理）
- LLM 策展（由 filter 层处理）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from extractors.contract import RawArticle
from fetcher.base_extractor import BaseExtractor

logger = logging.getLogger(__name__)

# ── Extractor 注册表 ──────────────────────────────────────────────

# 延迟加载，避免未安装的依赖阻断启动
_EXTRACTOR_CLASSES: dict[str, type[BaseExtractor]] = {}
_EXTRACTOR_INSTANCES: dict[str, BaseExtractor] = {}


def register_extractor(source_type: str, cls: type[BaseExtractor]):
    """注册 extractor 类到注册表。"""
    _EXTRACTOR_CLASSES[source_type] = cls


def get_extractor_class(source_type: str) -> type[BaseExtractor] | None:
    """获取 extractor 类（延迟实例化）。"""
    if source_type not in _EXTRACTOR_INSTANCES:
        cls = _EXTRACTOR_CLASSES.get(source_type)
        if cls:
            _EXTRACTOR_INSTANCES[source_type] = cls()
    return _EXTRACTOR_INSTANCES.get(source_type)


# ── 预注册内置 extractor ─────────────────────────────────────────

def _register_builtins():
    """在模块加载时注册内置 extractor。"""
    # GitHub
    try:
        from fetcher.extractors import GitHubExtractor
        # 包装 GitHubExtractor 为 BaseExtractor 兼容
        class _GitHubAdapter(BaseExtractor):
            name = "github"
            async def extract(self, session, source):
                ge = GitHubExtractor()
                raw = await ge.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "GitHub"),
                    "source_type": "github",
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit("github")],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("github", _GitHubAdapter)
    except Exception as e:
        logger.debug(f"Could not register github extractor: {e}")

    # ArXiv
    try:
        from fetcher.extractors import ArxivExtractor
        class _ArxivAdapter(BaseExtractor):
            name = "arxiv"
            async def extract(self, session, source):
                ae = ArxivExtractor()
                raw = await ae.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "ArXiv"),
                    "source_type": "arxiv",
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit("arxiv")],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("arxiv", _ArxivAdapter)
    except Exception as e:
        logger.debug(f"Could not register arxiv extractor: {e}")

    # RSS
    try:
        from fetcher.extractors import RSSExtractor
        class _RSSAdapter(BaseExtractor):
            name = "rss"
            async def extract(self, session, source):
                re_ = RSSExtractor()
                raw = await re_.extract(session, source)
                st = source.get("type", "web")  # 默认 web 长度
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "RSS"),
                    "source_type": st,
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit(st)],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("rss", _RSSAdapter)
    except Exception as e:
        logger.debug(f"Could not register rss extractor: {e}")

    # Web
    try:
        from fetcher.extractors import WebExtractor
        class _WebAdapter(BaseExtractor):
            name = "web"
            async def extract(self, session, source):
                we = WebExtractor()
                raw = await we.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "Web"),
                    "source_type": "web",
                    "feed_url": source.get("url"),
                    "content_preview": BaseExtractor.truncate_content(
                        r.get("summary", ""), "web"),
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("web", _WebAdapter)
    except Exception as e:
        logger.debug(f"Could not register web extractor: {e}")

    # HuggingFace — 复用 RSS extractor
    try:
        from fetcher.extractors import RSSExtractor
        class _HFAdapter(BaseExtractor):
            name = "hf"
            async def extract(self, session, source):
                re_ = RSSExtractor()
                raw = await re_.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "HuggingFace"),
                    "source_type": "hf",
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit("hf")],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("hf", _HFAdapter)
    except Exception as e:
        logger.debug(f"Could not register hf extractor: {e}")

    # Twitter — 复用 RSS
    try:
        from fetcher.extractors import TwitterExtractor
        class _TwitterAdapter(BaseExtractor):
            name = "twitter"
            async def extract(self, session, source):
                te = TwitterExtractor()
                raw = await te.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "Twitter"),
                    "source_type": "web",
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit("web")],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("twitter", _TwitterAdapter)
        register_extractor("nitter_rss", _TwitterAdapter)
    except Exception as e:
        logger.debug(f"Could not register twitter extractor: {e}")

    # WeChat — 需要 we-mp-rss 服务运行
    try:
        from fetcher.wechat_extractor import WechatExtractor
        register_extractor("wechat", WechatExtractor)
    except Exception as e:
        logger.debug(f"Could not register wechat extractor: {e}")

    # RSSHub — 复用 RSS
    try:
        from fetcher.extractors import RSSExtractor
        class _RSSHubAdapter(BaseExtractor):
            name = "rsshub"
            async def extract(self, session, source):
                re_ = RSSExtractor()
                raw = await re_.extract(session, source)
                return BaseExtractor.batch_validate([{
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", ""),
                    "published": r.get("published"),
                    "author": r.get("author"),
                    "source_name": source.get("name", "RSSHub"),
                    "source_type": "web",
                    "feed_url": source.get("url"),
                    "content_preview": r.get("summary", "")[:BaseExtractor.get_preview_limit("web")],
                    "raw_extra": {k: v for k, v in r.items() if k not in {"url", "title", "summary", "published", "author"}},
                } for r in raw])
        register_extractor("rsshub", _RSSHubAdapter)
    except Exception as e:
        logger.debug(f"Could not register rsshub extractor: {e}")

# 模块加载时注册
_register_builtins()


# ── IngestionManager ──────────────────────────────────────────────

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
        并发抓取所有活跃源。

        Args:
            sources: sources.yaml 中的源配置列表

        Returns:
            标准化 RawArticle 列表
        """
        active = [s for s in sources if s.get("active", True)]
        logger.info(f"IngestionManager: {len(active)} active sources, concurrency={self.concurrency}")

        connector = aiohttp.TCPConnector(limit=self.concurrency)
        sem = asyncio.Semaphore(self.concurrency)

        stats = {"success": 0, "failed": 0, "total": 0}

        async with aiohttp.ClientSession(connector=connector) as session:
            async def fetch_one(source: dict) -> list[RawArticle]:
                source_type = source.get("type", "rss")
                extractor = get_extractor_class(source_type)
                stats["total"] += 1

                if extractor is None:
                    logger.warning(f"No extractor for type '{source_type}': {source.get('name', '?')}")
                    stats["failed"] += 1
                    return []

                async with sem:
                    try:
                        articles = await asyncio.wait_for(
                            extractor.extract(session, source),
                            timeout=self.timeout,
                        )
                        if articles:
                            logger.info(
                                f"[{extractor.name}] {source.get('name', '?')}: "
                                f"{len(articles)} articles"
                            )
                            stats["success"] += 1
                        else:
                            stats["failed"] += 1
                        return articles
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[{extractor.name}] {source.get('name', '?')}: "
                            f"timeout after {self.timeout}s"
                        )
                        stats["failed"] += 1
                        return []
                    except Exception as e:
                        logger.warning(
                            f"[{extractor.name}] {source.get('name', '?')}: {e}"
                        )
                        stats["failed"] += 1
                        return []

            tasks = [fetch_one(s) for s in active]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # 汇总
        all_articles: list[RawArticle] = []
        for r in results:
            if isinstance(r, list):
                all_articles.extend(r)
            elif isinstance(r, Exception):
                logger.error(f"IngestionManager unhandled error: {r}")

        logger.info(
            f"IngestionManager done: {len(all_articles)} articles, "
            f"stats={stats}"
        )
        return all_articles

    async def fetch_single(self, source: dict) -> list[RawArticle]:
        """
        抓取单个源（调试用）。
        """
        return await self.fetch([source])

    @staticmethod
    def list_extractors() -> dict[str, str]:
        """列出所有已注册的 extractor。"""
        return {
            st: cls.name
            for st, cls in _EXTRACTOR_CLASSES.items()
        }
