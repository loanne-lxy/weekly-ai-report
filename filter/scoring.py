"""Evidence-weighted event ranking.

Normalized technical scores (85%) plus publisher/freshness context (15%),
multiplied by evidence relevance and time decay. Counts and popularity do not
establish confidence. Publisher reference scores are editorial heuristics.
"""
from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Source quality whitelist (domain → 0-1) ──────────────────────
_SOURCE_QUALITY = {
    # Tier 1: Top-tier venues
    "openai.com": 0.95,
    "deepmind.google": 0.95,
    "ai.google": 0.95,
    "arxiv.org": 0.9,
    "nvidia.com": 0.9,
    "microsoft.com": 0.9,
    "meta.com": 0.9,
    "anthropic.com": 0.95,
    "huggingface.co": 0.9,
    "nelp.com": 0.9,
    "mistral.ai": 0.9,
    "x.ai": 0.9,
    # Conferences
    "neurips.cc": 0.9,
    "iclr.cc": 0.9,
    "cvpr": 0.9,
    "aclanthology.org": 0.85,
    "ieee.org": 0.85,
    "acm.org": 0.85,
    "ijcai.org": 0.85,
    "icml.cc": 0.85,
    "aaai.org": 0.85,
    "emnlp.org": 0.85,
    # News/Media
    "techcrunch.com": 0.7,
    "theverge.com": 0.7,
    "wired.com": 0.7,
    "venturebeat.com": 0.65,
    "reuters.com": 0.8,
    "bloomberg.com": 0.8,
    "bntnews.com": 0.7,
    # General
    "github.com": 0.8,
    "medium.com": 0.5,
    "substack.com": 0.4,
    "twitter.com": 0.3,
    "x.com": 0.3,
    "reddit.com": 0.2,
}

_DEFAULT_SOURCE_SCORE = 0.35  # Unknown sources

# Half-life configs by event type hint
_BREAKING_HALF_LIFE = 3.0   # days
_DEEP_HALF_LIFE = 14.0      # days


def _parse_published(iso_str):
    from filter.quality import parse_date
    return parse_date(iso_str)


def _source_quality_score(source_ids: list[str]) -> float:
    """Average quality score for the event's sources."""
    if not source_ids:
        return _DEFAULT_SOURCE_SCORE

    scores = []
    for sid in source_ids:
        # Extract domain from source_id like "rss:openai.com" or "exa_search:abc123"
        parts = sid.split(":", 1)
        domain = parts[-1] if len(parts) > 1 else sid
        scores.append(max((v for host, v in _SOURCE_QUALITY.items() if domain.lower() == host or domain.lower().endswith("." + host)), default=_DEFAULT_SOURCE_SCORE))

    return sum(scores) / len(scores) if scores else _DEFAULT_SOURCE_SCORE


def np_max(vals: list[float]) -> float:
    return max(vals) if vals else 0.0


def compute_objective_metrics(
    articles: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, float]:
    """
    Compute objective metrics for an event's article cluster.

    Returns dict with coverage, recency, source_quality, source_diversity,
    article_quality. All values in [0, 1].
    """
    if now is None:
        now = datetime.now(timezone.utc)

    n = len(articles)
    if n == 0:
        return {
            "coverage": 0.0,
            "recency": 0.0,
            "source_quality": _DEFAULT_SOURCE_SCORE,
            "source_diversity": 0.0,
            "article_quality": 0.0,
            "time_span_days": 0.0,
        }

    # Coverage: how many articles in this event (saturates at 5)
    coverage = min(n / 5.0, 1.0)

    # Recency: sigmoid with 48h half-life
    published_times = [
        _parse_published(a.get("published")) for a in articles
    ]
    valid_times = [t for t in published_times if t is not None]

    if valid_times:
        # Use the most recent article's time
        latest = max(valid_times)
        hours_ago = (now - latest).total_seconds() / 3600.0
        # Sigmoid: 1 / (1 + exp(k * hours_ago)), k chosen so recency=0.5 at 48h
        k = math.log(2) / 48.0
        recency = math.exp(-math.log(2) * max(hours_ago, 0) / 48.0)
    else:
        # No published date — assume very recent
        recency = 0.0

    # Source Quality
    from filter.quality import publisher
    source_ids = list({publisher(a) for a in articles})
    source_quality = _source_quality_score(source_ids)

    # Source Diversity: unique domains / total articles
    source_diversity = len(source_ids) / n if n > 0 else 0.0

    # Article Quality: average (len(content) / len(summary)) ratio, capped
    quality_scores = []
    for a in articles:
        summary_len = len(a.get("summary", a.get("content_preview", "")) or "")
        content_len = len(a.get("content_preview", a.get("summary", "")) or "")
        if summary_len > 0:
            ratio = min(content_len / summary_len, 5.0) / 5.0
            quality_scores.append(ratio)

    article_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0

    # Time span (for decay selection)
    time_span_days = 0.0
    if len(valid_times) >= 2:
        span = (max(valid_times) - min(valid_times)).total_seconds() / 86400.0
        time_span_days = span

    return {
        "coverage": round(coverage, 4),
        "recency": round(recency, 4),
        "source_quality": round(source_quality, 4),
        "source_diversity": round(source_diversity, 4),
        "article_quality": round(article_quality, 4),
        "time_span_days": round(time_span_days, 2),
    }


def compute_time_decay(
    articles: list[dict[str, Any]],
    half_life: float = _BREAKING_HALF_LIFE,
    now: datetime | None = None,
) -> float:
    """
    Compute time decay factor for an event.

    Returns factor in (0, 1] where 1 = no decay.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    published_times = [
        _parse_published(a.get("published")) for a in articles
    ]
    valid_times = [t for t in published_times if t is not None]

    if not valid_times:
        return 0.5  # Unknown time cannot imply freshness

    # Use earliest article as event start
    earliest = min(valid_times)
    days_ago = (now - earliest).total_seconds() / 86400.0

    # exp(-ln2 * days / half_life)
    decay = math.exp(-math.log(2) * days_ago / half_life)
    return max(decay, 0.01)  # Floor at 1%


def select_half_life(
    category: str, time_span_days: float, importance: float = 5
) -> float:
    """
    Select decay half-life based on event characteristics.

    Breaking news / trending → 3 days (strong decay)
    Deep analysis / trends → 14 days (weak decay)
    """
    # Heuristics for "breaking" vs "deep":
    # - Short time span + many sources = breaking news
    # - Long time span + academic sources = deep analysis
    # - LLM importance >= 0.7 = likely deeper content
    is_breaking = time_span_days < 2.0
    is_deep = time_span_days >= 2.0 or importance >= 0.7

    if is_deep:
        return _DEEP_HALF_LIFE
    return _BREAKING_HALF_LIFE


def compute_value_score(scores):
    """Research value independent of publisher, evidence confidence and age."""
    from filter.quality import score_number
    return round(0.4 * score_number(scores.get("importance"))
                 + 0.3 * score_number(scores.get("novelty"))
                 + 0.3 * score_number(scores.get("impact")), 4)


def compute_final_score(llm_scores, obj_metrics, article_count, time_decay=1.0):
    from filter.quality import score_number
    # Normalize each component ONCE. Count does not establish truth or confidence.
    llm_part = (0.40 * score_number(llm_scores.get("importance"))
                + 0.30 * score_number(llm_scores.get("novelty"))
                + 0.30 * score_number(llm_scores.get("impact")))
    quality = obj_metrics.get("source_quality", _DEFAULT_SOURCE_SCORE)
    obj_part = 0.7 * quality + 0.3 * obj_metrics.get("recency", 0)
    confidence = score_number(llm_scores.get("evidence_relevance"), 0.0)
    final_score = (0.85 * llm_part + 0.15 * obj_part) * confidence * time_decay
    return {"final_score": round(final_score, 4), "llm_part": round(llm_part, 4),
            "obj_part": round(obj_part, 4), "confidence": round(confidence, 4),
            "time_decay": round(time_decay, 4)}


def _trending_popularity(articles: list[dict[str, Any]]) -> float:
    """GitHub Trending rank → popularity boost (0-0.3), added to importance.

    Uses the best (lowest) trending rank across the event's articles.
    rank 1 → +0.30, rank 10 → +0.03 (linear). 0 when no trending article.
    ponytail: rank only (extractor caps at top-10, stars-gained parse is broken);
    swap to a stars-based score once extractors.py star extraction is fixed.
    """
    best = None
    for a in articles:
        r = (a.get("raw_extra") or {}).get("trending_rank")
        if isinstance(r, int) and 1 <= r <= 10:
            if best is None or r < best:
                best = r
    if best is None:
        return 0.0
    return round(0.30 * (11 - best) / 10.0, 4)


def score_event(
    articles: list[dict[str, Any]],
    llm_scores: dict[str, float],
    category: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Full scoring pipeline for one event.

    Returns enriched dict with all intermediate scores and rationale.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Objective metrics
    obj = compute_objective_metrics(articles, now)

    # 2. Time decay
    importance_raw = llm_scores.get("importance", 0.5)
    half_life = 14.0 if any("arxiv" in a.get("source_name", "").lower() or "arxiv.org" in a.get("url", "") for a in articles) else select_half_life(category, obj["time_span_days"], importance_raw)
    decay = compute_time_decay(articles, half_life, now)

    # 3. Final score
    final = compute_final_score(
        llm_scores, obj, len(articles), decay
    )

    return {
        # LLM scores
        "importance": llm_scores.get("importance", 0.5),
        "importance_rationale": llm_scores.get("importance_rationale", ""),
        "novelty": llm_scores.get("novelty", 0.5),
        "novelty_rationale": llm_scores.get("novelty_rationale", ""),
        "impact": llm_scores.get("impact", 0.5),
        "impact_rationale": llm_scores.get("impact_rationale", ""),
        # Evidence articles referenced by LLM
        "evidence_articles": llm_scores.get("evidence_articles", []),
        # Objective metrics
        "coverage": obj["coverage"],
        "recency": obj["recency"],
        "source_quality": obj["source_quality"],
        "source_diversity": obj["source_diversity"],
        "article_quality": obj["article_quality"],
        # Final
        "final_score": final["final_score"],
        "llm_part": final["llm_part"],
        "value_score": compute_value_score(llm_scores),
        "obj_part": final["obj_part"],
        "confidence": final["confidence"],
        "time_decay": final["time_decay"],
        "half_life_days": half_life,
    }


def sort_events(
    events: list[dict[str, Any]],
    top_n_per_category: int = 50,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Sort events: group by category → sort by final_score desc → take top N.

    Tiebreakers: more sources > more recent > more articles.
    Also detect cross-domain events.
    """
    # Group by category
    groups: dict[str, list[dict[str, Any]]] = {}
    for evt in events:
        cat = evt.get("category", "Uncategorized")
        groups.setdefault(cat, []).append(evt)

    sorted_events = []
    cross_domain: list[dict[str, Any]] = []

    for cat, group in groups.items():
        # Sort: primary = final_score, tiebreakers
        group.sort(
            key=lambda e: (
                e.get("final_score", 0),
                e.get("source_diversity", 0),  # More sources wins ties
                e.get("recency", 0),           # More recent wins ties
            ),
            reverse=True,
        )

        # Detect cross-domain: check if rationale mentions multiple categories
        for evt in group:
            if _is_cross_domain(evt):
                evt["cross_domain"] = True
                cross_domain.append(evt)

        sorted_events.extend(group[:top_n_per_category])

    # Cross-domain events go into a separate recommended block
    # (already in sorted_events, just tagged)
    logger.info(
        f"Sorted {len(sorted_events)} events, "
        f"{len(cross_domain)} cross-domain flagged"
    )

    return sorted_events, cross_domain


def _is_cross_domain(evt: dict[str, Any]) -> bool:
    """
    Heuristic: if LLM rationales mention multiple category keywords,
    flag as cross-domain.
    """
    categories = ["LLM", "Agent", "AI for Science", "设计仿真", "数字孪生",
                   "Design Simulation", "Digital Twin"]

    rationales = " ".join([
        evt.get("importance_rationale", ""),
        evt.get("novelty_rationale", ""),
        evt.get("impact_rationale", ""),
    ]).lower()

    # Count how many category keywords appear in rationales
    matched = sum(1 for cat in categories if cat.lower() in rationales)
    return matched >= 2
