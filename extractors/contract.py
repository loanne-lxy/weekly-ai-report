"""
统一数据契约 — 系统核心对象的定义与关系。

Source ─→ CandidateSource ─→ URL ─→ RawArticle ─→ CuratedArticle ─→ Event ─→ Claim/Evidence

Design principles:
  1. Source / URL / Article 三者按 source_id 或 url 关联，不互相嵌套引用。
  2. published 字段统一 ISO 8601 字符串（如 "2026-08-15T10:30:00+00:00"），
     不做 datetime 对象，避免时区隐式转换。
  3. url 在进入下游前统一 canonicalize（见 helpers）。
  4. Event / Claim / Evidence 为后置可选层，不影响现有 pipeline 召回率。
  5. 持久化层（DB / YAML / JSON）独立管理，本模块只做内存验证。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def normalize_domain(url: str) -> str:
    """
    Canonicalize URL → lowercase domain without scheme/www/path.

    Examples:
      "https://www.OPENAI.COM/blog/rss.xml" → "openai.com"
      "https://github.com/org/repo"          → "github.com"
    """
    try:
        parsed = urlparse(url.lower().strip())
        domain = parsed.hostname or ""
        return domain.lstrip("www.") or domain
    except Exception:
        return url.lower().strip()


def make_source_id(source_type: str, url: str, **kwargs: Any) -> str:
    """
    Deterministic source_id. Strategies by type:

    RSS / Web / arXiv:  '{type}:{domain}'  (if unique)
                         '{type}:{domain}:{path_hash}'  (if domain has collisions)
    GitHub:             '{type}:{owner}/{repo}'  or  '{type}:trending'
    Exa search:         '{type}:{query_hash[:8]}'

    Callers may pass github_owner, github_repo, github_subtype, query
    via **kwargs for types without a url.
    """
    if source_type == "github":
        subtype = kwargs.get("github_subtype", "")
        if subtype == "github_trending":
            return "github:trending"
        owner = kwargs.get("github_owner", "unknown")
        repo = kwargs.get("github_repo", "unknown")
        return f"github:{owner}/{repo}"

    if source_type == "exa_search":
        query = (kwargs.get("query") or "").strip().lower()
        q_hash = hashlib.md5(query.encode()).hexdigest()[:8] if query else "empty"
        return f"exa_search:{q_hash}"

    # RSS / Web / arXiv — domain-based
    domain = normalize_domain(url)
    # For arXiv domains, include path to disambiguate categories
    if source_type in ("rss", "web", "arxiv") and domain.endswith("arxiv.org"):
        try:
            path = urlparse(url).path.rstrip("/")
            path_key = path.split("/")[-1]  # e.g. "cs.SY", "physics.comp-ph"
            if path_key:
                return f"{source_type}:{domain}:{path_key}"
        except Exception:
            pass
    return f"{source_type}:{domain}"


def canonicalize_url(url: str) -> str:
    """
    Normalize a URL for dedup: lowercase, strip trailing slash, remove fragment.

    Returns the cleaned URL string.
    """
    url = url.strip().lower()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        canonical = parsed._replace(path=path, fragment="").geturl()
        return canonical
    except Exception:
        return url


def content_hash(*, url: str, title: str, summary: str = "") -> str:
    """
    SHA-256 hash of (url + title + summary) for dedup / change detection.
    Returns first 16 hex chars.
    """
    raw = f"{url}||{title}||{summary}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def to_iso8601(dt: datetime | str | None) -> str | None:
    """
    Convert datetime / ISO string → normalized ISO 8601 string.
    Returns None for empty/None input.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return None
        # Already ISO-like — normalize Z suffix
        return s.replace("Z", "+00:00")
    return dt.isoformat()


# ══════════════════════════════════════════════════════════════════
# 1. Source 层
# ══════════════════════════════════════════════════════════════════

class Source(BaseModel):
    """长期监控的信息源。对应 sources.yaml 中的一项。"""
    id: str = Field(
        ...,
        description="唯一标识, 格式 '{type}:{domain}', 由 make_source_id() 生成",
    )
    name: str = Field(..., description="显示名称")
    url: str = Field(..., description="Feed / Repo / Site URL")
    type: str = Field(
        ...,
        description="源类型: rss | github | arxiv | web | exa_search",
    )
    category: str | None = Field(None, description="主分类 (如 'LLM')")
    default_category: str | None = Field(
        None,
        description="[DEPRECATED] Use 'category' instead. Kept for backward compat.",
    )
    weight: int = Field(5, description="抓取优先级/权重")
    active: bool = Field(True, description="是否启用")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展字段: note, feed_url 等",
    )


class CandidateSource(BaseModel):
    """系统自动发现、尚未批准加入的信息源。"""
    url: str = Field(..., description="候选源 URL（待 canonicalize）")
    discovered_via: str = Field(
        ...,
        description="发现方式: 'link_mining' | 'exa_search' | 'llm'",
    )
    category_hint: str | None = Field(None, description="LLM 推断的分类")
    discovered_at: str = Field(..., description="发现时间, ISO 8601")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceState(BaseModel):
    """Source 最近的运行状态。暂存于内存，不直接持久化。"""
    source_id: str = Field(..., description="关联 Source.id")
    articles_this_week: int = Field(0, description="本周产出文章数")
    streak_failures: int = Field(0, description="连续失败次数")
    eval_score: float = Field(5.0, description="综合评估分 0-10")
    last_fetched: str | None = Field(None, description="最后抓取时间, ISO 8601")
    last_success: str | None = Field(None, description="最后成功时间, ISO 8601")


# ══════════════════════════════════════════════════════════════════
# 2. URL 记录层
# ══════════════════════════════════════════════════════════════════

class URLRecord(BaseModel):
    """URL 去重记录。用于 Hard Dedup + 变化检测。"""
    url: str = Field(..., description="canonicalized URL")
    source_id: str = Field(..., description="来自哪个 Source")
    first_seen: str = Field(..., description="首次出现时间, ISO 8601")
    last_seen: str = Field(..., description="最近一次出现, ISO 8601")
    date_bucket: str = Field(..., description="时间桶, 如 '2026-W33'")
    content_hash: str | None = Field(
        None,
        description="content_hash(url, title, summary)[:16], 防标题党",
    )


# ══════════════════════════════════════════════════════════════════
# 3. Article 层
# ══════════════════════════════════════════════════════════════════

class RawArticle(BaseModel):
    """
    抓取层标准输出 — 纯原始数据，无 LLM 加工。

    url 必须是 canonicalize_url() 处理过的。
    published 必须是 ISO 8601 字符串或 None。
    """
    url: str = Field(..., description="canonicalized URL，唯一标识")
    title: str = Field(..., description="原始标题")
    summary: str | None = Field(None, description="原始摘要 / RSS description")
    published: str | None = Field(None, description="发布时间, ISO 8601")
    author: str | None = Field(None, description="作者名")
    source_id: str = Field(..., description="关联 Source.id")
    source_name: str = Field(..., description="来源名称")
    source_type: str = Field(..., description="平台类型: rss | github | web | arxiv | exa_search")
    default_category: str | None = Field(
        None,
        description="[DEPRECATED] Use 'category' instead. Kept for backward compat.",
    )
    content_preview: str | None = Field(
        None,
        description="正文前 500 字符，供 Curator 参考",
    )
    raw_extra: dict[str, Any] = Field(
        default_factory=dict,
        description="源平台特有字段，保留原始信息",
    )

    model_config = {"extra": "forbid"}


class CuratedArticle(RawArticle):
    """
    LLM 策展层输出 — 在 RawArticle 上扩展 LLM 生成字段。

    key_insights 保留给前端渲染。
    claims / evidence 用于内部知识图谱/检索。
    """
    chinese_title: str | None = Field(None, description="LLM 生成的中文标题")
    ai_summary: str | None = Field(None, description="LLM 生成的中文摘要")
    category: str | None = Field(
        None,
        description="LLM 分类: LLM / Agent / AI for Science / 设计仿真 / 数字孪生",
    )
    priority_score: int | None = Field(None, description="LLM 评分 1-10")
    key_insights: list[str] = Field(
        default_factory=list,
        description="LLM 提取的关键洞察 (1-3 条), 供前端渲染",
    )
    why_it_matters: str | None = Field(None, description="LLM 重要性说明")
    tags: list[str] = Field(default_factory=list, description="LLM 标签")
    carried_over: bool = Field(False, description="是否从上周回填")
    claims: list["Claim"] = Field(
        default_factory=list,
        description="结构化事实陈述 (内部知识图谱)",
    )
    evidence: list["Evidence"] = Field(
        default_factory=list,
        description="支撑 Claim 的证据",
    )

    model_config = {"extra": "forbid"}


# ══════════════════════════════════════════════════════════════════
# 4. Event 层 (多篇文章聚类)
# ══════════════════════════════════════════════════════════════════

class Event(BaseModel):
    """
    多篇文章共同报道的同一个事件。

    当前作为后置可选聚合层：在 Curator 输出后，对 CuratedArticle 做
    语义聚类（≥0.92 相似度），将同源事件的文章聚合到一个 Event 中。

    不影响现有 pipeline 召回率。
    """
    event_id: str = Field(..., description="语义聚类 ID (SHA-256[:16])")
    title: str = Field(..., description="事件标题")
    description: str | None = Field(None, description="事件描述摘要")
    published: str | None = Field(None, description="最早一篇的发布时间, ISO 8601")
    category: str | None = Field(None, description="聚类后的分类")
    priority_score: float | None = Field(None, description="最高评分")
    articles: list[CuratedArticle] = Field(
        default_factory=list,
        description="属于该事件的所有 CuratedArticle",
    )
    claims: list["Claim"] = Field(
        default_factory=list,
        description="从该事件提取的 Claim 集合",
    )
    evidence: list["Evidence"] = Field(
        default_factory=list,
        description="从该事件提取的 Evidence 集合",
    )


# ══════════════════════════════════════════════════════════════════
# 5. Claim / Evidence
# ══════════════════════════════════════════════════════════════════

class Claim(BaseModel):
    """
    结构化的事实陈述，用于内部知识图谱/检索。

    与 key_insights 共存：key_insights 给前端渲染，Claim 做交叉验证。
    """
    text: str = Field(..., description="事实陈述")
    confidence: float | None = Field(None, description="置信度 0-1")
    extracted_from: str | None = Field(None, description="来源文章 URL")


class Evidence(BaseModel):
    """支撑 Claim 的证据。"""
    url: str = Field(..., description="证据来源 URL")
    text: str | None = Field(None, description="证据原文片段")
    evidence_type: str = Field(
        ...,
        description="证据类型: 'article' | 'data' | 'screenshot' | 'code'",
    )


# ══════════════════════════════════════════════════════════════════
# Backward compatibility aliases
# ══════════════════════════════════════════════════════════════════

# Old code references RawArticle / CuratedArticle directly — still works.
# New code should use the helpers for canonicalization.

__all__ = [
    # Helpers
    "normalize_domain",
    "make_source_id",
    "canonicalize_url",
    "content_hash",
    "to_iso8601",
    # Source
    "Source",
    "CandidateSource",
    "SourceState",
    # URL
    "URLRecord",
    # Article
    "RawArticle",
    "CuratedArticle",
    # Event
    "Event",
    # Claim / Evidence
    "Claim",
    "Evidence",
]
