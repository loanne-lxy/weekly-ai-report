"""
Connector Registry — 统一 Connector 注册与路由。

职责：
  1. 集中管理所有 Connector 类
  2. 按 connector type 路由到正确的实现
  3. 提供 Connector 元数据（名称、描述、支持的平台等）
  4. 支持动态注册（未来可扩展）

架构:
  source.connector → ConnectorRegistry.get(connector) → Extractor instance
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectorMeta:
    """Connector 元数据."""
    name: str
    display_name: str
    description: str
    supported_platforms: list[str]
    requires_api_key: bool = False
    config_fields: list[str] = field(default_factory=list)
    priority: int = 5


class ConnectorRegistry:
    """
    统一 Connector 注册中心（模块级单例）。

    Usage:
        registry = connector_registry
        meta = registry.get_meta("rss")
        extractor = registry.get_extractor("rss")
    """

    def __init__(self):
        self._metas: dict[str, ConnectorMeta] = {}
        self._extractors: dict[str, Any] = {}
        self._initialized: bool = False
        self.initialize()

    def initialize(self):
        """注册所有内置 Connector."""
        if self._initialized:
            return
        self._initialized = True

        # RSS Connector
        self.register(
            ConnectorMeta(
                name="rss",
                display_name="RSS/Atom Feed",
                description="RSS/Atom feed 解析，支持 feedparser",
                supported_platforms=["web", "rss", "atom"],
                config_fields=["endpoint", "max_days"],
                priority=7,
            )
        )

        # arXiv Connector
        self.register(
            ConnectorMeta(
                name="arxiv",
                display_name="arXiv API",
                description="arXiv 论文检索，支持按类别过滤",
                supported_platforms=["arxiv.org"],
                config_fields=["endpoint", "max_results"],
                priority=7,
            )
        )

        # GitHub Connector
        self.register(
            ConnectorMeta(
                name="github",
                display_name="GitHub API",
                description="GitHub Releases/Trending，支持 PyGithub API",
                supported_platforms=["github.com"],
                config_fields=["github_owner", "github_repo", "github_subtype"],
                priority=7,
            )
        )

        # Web Connector
        self.register(
            ConnectorMeta(
                name="web",
                display_name="Web Page",
                description="通用网页提取，trafilatura + Crawl4AI fallback",
                supported_platforms=["web"],
                config_fields=["endpoint", "user_agent"],
                priority=5,
            )
        )

        # Exa Search Connector
        self.register(
            ConnectorMeta(
                name="exa_search",
                display_name="Exa Neural Search",
                description="Exa AI 搜索，需要 API key",
                supported_platforms=["exa.ai"],
                requires_api_key=True,
                config_fields=["query", "max_results", "exa_api_key"],
                priority=7,
            )
        )

        # Load extractors
        from fetcher.extractors import (
            RSSExtractor, ArxivExtractor, GitHubExtractor,
            WebExtractor, ExaExtractor,
        )
        self._extractors["rss"] = RSSExtractor()
        self._extractors["arxiv"] = ArxivExtractor()
        self._extractors["github"] = GitHubExtractor()
        self._extractors["web"] = WebExtractor()
        self._extractors["exa_search"] = ExaExtractor()

        logger.info(
            f"ConnectorRegistry initialized: "
            f"{len(self._metas)} connectors registered"
        )

    def register(self, meta: ConnectorMeta) -> None:
        """注册一个 Connector 元数据."""
        self._metas[meta.name] = meta

    def get_meta(self, name: str) -> ConnectorMeta | None:
        """获取 Connector 元数据."""
        return self._metas.get(name)

    def get_extractor(self, name: str) -> Any:
        """获取 Connector 实例."""
        extractor = self._extractors.get(name)
        if extractor is None:
            logger.warning(
                f"Unknown connector '{name}', falling back to RSS"
            )
            return self._extractors.get("rss")
        return extractor

    def list_connectors(self) -> list[dict[str, Any]]:
        """列出所有注册的 Connector."""
        return [
            {
                "name": m.name,
                "display_name": m.display_name,
                "description": m.description,
                "supported_platforms": m.supported_platforms,
                "requires_api_key": m.requires_api_key,
                "config_fields": m.config_fields,
                "priority": m.priority,
            }
            for m in sorted(self._metas.values(), key=lambda x: -x.priority)
        ]

    def is_supported(self, connector: str) -> bool:
        """检查 connector 是否被支持."""
        return connector in self._metas

    def available_names(self) -> list[str]:
        """返回所有支持的 connector 名称列表."""
        return list(self._metas.keys())


# Module-level singleton
connector_registry = ConnectorRegistry()
