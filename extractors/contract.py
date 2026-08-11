"""
统一数据契约 — 抓取层与策展层之间的标准化接口。

RawArticle:  抓取层输出，纯原始数据，无 LLM 加工。
CuratedArticle: 策展层输出，在 RawArticle 上扩展 LLM 生成字段。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 抓取层输出
# ──────────────────────────────────────────────

class RawArticle(BaseModel):
    """
    平台适配器标准输出 — 所有 extractor.extract() 必须返回此结构。
    只包含从源平台直接采集并标准化的原始数据，不含任何 LLM 生成内容。
    """
    url: str = Field(..., description="文章 URL，作为唯一标识供下游去重")
    title: str = Field(..., description="原始标题")
    summary: str | None = Field(None, description="原始摘要 / RSS description / commit message")
    published: str | None = Field(None, description="发布时间，ISO 8601 格式 (YYYY-MM-DDTHH:MM:SS+TZ)")
    author: str | None = Field(None, description="作者名")
    source_name: str = Field(..., description="来源名称，如 'HuggingFace Blog', 'OpenAI Blog'")
    source_type: str = Field(
        ...,
        description="来源平台类型: github | hf | wechat | web | arxiv",
        pattern=r"^(github|hf|wechat|web|arxiv)$",
    )
    feed_url: str | None = Field(None, description="RSS/Atom feed URL，可追溯原始源")
    content_preview: str | None = Field(
        None,
        description="正文前 500 字符，供下游 LLM 策展时参考上下文",
    )
    raw_extra: dict[str, Any] = Field(
        default_factory=dict,
        description="源平台特有字段，不强制下游处理，保留原始信息",
    )

    model_config = {"extra": "forbid"}


# ──────────────────────────────────────────────
# 策展层输出（继承 RawArticle）
# ──────────────────────────────────────────────

class CuratedArticle(RawArticle):
    """
    LLM 策展层输出 — 在 RawArticle 基础上扩展策展字段，供下游渲染使用。
    """
    chinese_title: str | None = Field(None, description="LLM 生成的中文标题")
    ai_summary: str | None = Field(None, description="LLM 生成的中文摘要")
    category: str | None = Field(None, description="LLM 分类: LLM / Agent / AI for Science / 设计仿真 / 数字孪生")
    priority_score: int | None = Field(None, description="LLM 评分 1-10")
    key_insights: list[str] = Field(
        default_factory=list,
        description="LLM 提取的关键洞察 (1-3 条)",
    )
    why_it_matters: str | None = Field(None, description="LLM 判断的重要性说明")
    tags: list[str] = Field(default_factory=list, description="LLM 生成的标签")
    carried_over: bool = Field(False, description="是否从上周回填的旧文章")

    model_config = {"extra": "forbid"}
