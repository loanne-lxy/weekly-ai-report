"""
抽象基类 — 所有 extractor 必须继承此类并实现 extract()。

职责：
1. 定义 extract() 接口
2. 内置 RawArticle 校验，脏数据在抓取层就被拦截
3. 提供常用工具方法（日期解析、URL 清洗等）
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import aiohttp

from extractors.contract import RawArticle

logger = logging.getLogger(__name__)

USER_AGENT = "Weekly-AI-Report-Agent/1.0"


class BaseExtractor(ABC):
    """
    Extractor 抽象基类。

    子类实现 extract(session, source) → list[RawArticle]
    返回的 RawArticle 会自动通过 Pydantic 校验。
    """

    name: str = "base"  # 子类覆盖

    @abstractmethod
    async def extract(
        self, session: aiohttp.ClientSession, source: dict
    ) -> list[RawArticle]:
        """
        从源抓取文章并返回标准化 RawArticle 列表。

        Args:
            session: aiohttp 会话，已配置连接池
            source: 源配置字典，包含 url, name, type 等

        Returns:
            RawArticle 列表，通过 Pydantic 校验
        """
        ...

    # ── 工具方法 ──────────────────────────────────────────────

    @staticmethod
    def validate(raw: dict[str, Any]) -> RawArticle | None:
        """
        将原始 dict 转为 RawArticle，失败返回 None 并记录警告。
        """
        try:
            return RawArticle(**raw)
        except Exception as e:
            logger.warning(f"RawArticle validation failed: {e}, data: {raw.get('url', 'no-url')}")
            return None

    @staticmethod
    def batch_validate(items: list[dict]) -> list[RawArticle]:
        """批量校验，过滤无效条目。"""
        return [a for item in items if (a := BaseExtractor.validate(item)) is not None]

    @staticmethod
    def parse_date(entry: Any, attr_names: list[str] | None = None) -> str | None:
        """
        从 feedparser entry 解析日期 → ISO 8601。
        """
        if attr_names is None:
            attr_names = ["published_parsed", "updated_parsed"]

        for attr in attr_names:
            tp = getattr(entry, attr, None)
            if tp:
                try:
                    return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
        return None

    @staticmethod
    def sanitize_text(text: str | None, max_len: int = 2000) -> str | None:
        """清洗文本：去空白、截断。"""
        if not text:
            return None
        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned[:max_len] if cleaned else None

    @staticmethod
    def sanitize_url(url: str | None) -> str | None:
        """清洗 URL：去空白，补协议。"""
        if not url:
            return None
        url = url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    # ── 按 source_type 差异化的 content_preview 长度 ─────────

    CONTENT_PREVIEW_LIMITS: dict[str, int] = {
        "arxiv": 2000,  # 摘要+引言+方法概述
        "web": 1200,    # 博客核心论点在前几段
        "wechat": 1000, # 公众号文章核心在前两段
        "github": 600,  # release/commit message 本身短
        "hf": 600,      # blog 和 release 通常短
    }
    DEFAULT_PREVIEW_LIMIT = 800  # 未知类型 fallback

    @classmethod
    def get_preview_limit(cls, source_type: str) -> int:
        return cls.CONTENT_PREVIEW_LIMITS.get(source_type, cls.DEFAULT_PREVIEW_LIMIT)

    @classmethod
    def truncate_content(cls, text: str | None, source_type: str) -> str | None:
        """从 HTML 或纯文本中提取前 N 字符作为 content_preview。"""
        if not text:
            return None
        # 简单去除 HTML 标签
        clean = re.sub(r"<[^>]+>", " ", text).strip()
        clean = re.sub(r"\s+", " ", clean).strip()
        limit = cls.get_preview_limit(source_type)
        return clean[:limit] if clean else None
