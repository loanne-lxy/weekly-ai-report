"""
Event Curator — 事件级策展与评分 + Top-3 精选文章。

输入：Cluster 好的 Event 列表（每个 Event 包含若干文章）
输出：对每个 Event 进行：
  1. 相关性判断 (is_relevant)
  2. 中文标题/摘要
  3. 分类 (category)
  4. 评分 (importance, novelty, impact 0-1)
  5. 从簇内选出 Top-3 最优质文章索引 ← 这就是去重结果

Prompt 来源：references/ 目录下的 Markdown 文件，启动时加载。

设计原则：
  - 以 Event 为单位批量送 LLM (BATCH_SIZE=2)
  - 每个 Event 只保留 Top-3 文章，其余丢弃
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

BATCH_SIZE = 2

# ── Prompt 文件路径（相对于项目根目录）─────────────────────────
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "references"
_SYSTEM_PROMPT_FILE = _PROMPTS_DIR / "event_curator_system.md"
_USER_TEMPLATE_FILE = _PROMPTS_DIR / "event_curator_user.md"


def _load_prompt_file(path: Path) -> str:
    """Load prompt from Markdown file, stripping frontmatter if present."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    content = path.read_text(encoding="utf-8")
    # Strip YAML frontmatter (--- ... ---) if present
    if content.startswith("---"):
        content = content.split("---", 2)[2]
    return content.strip()


# 启动时加载（避免每次请求都读文件）
EVENT_CURATOR_SYSTEM = _load_prompt_file(_SYSTEM_PROMPT_FILE)
EVENT_CURATOR_USER_TEMPLATE = _load_prompt_file(_USER_TEMPLATE_FILE)

# ── Event block template ─────────────────────────────────────────
_EVENT_TEMPLATE = """--- Event {idx} ---
Bucket: {bucket}
Cluster stats: {stats_text}
Articles (indices 0-based):
{articles_text}"""


class EventCurator:
    """事件级策展器 — LLM 评分 + 分类 + Top-3 精选文章."""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    @staticmethod
    def _fallback_event(
        evt: dict[str, Any], embeddings: np.ndarray = None
    ) -> dict[str, Any]:
        """Fallback when LLM output is invalid — pick nearest-to-centroid."""
        from event_clustering import pick_nearest_to_centroid

        indices = evt.get("article_indices", [])
        emb = embeddings if embeddings is not None and embeddings.size > 0 else np.zeros((0, 0), dtype=np.float32)
        top = pick_nearest_to_centroid(indices, emb)
        return {
            **evt,
            "is_relevant": True,
            "event_title": evt.get("title", ""),
            "event_summary": (evt.get("summary", "") or "")[:500],
            "category": evt.get("category", "LLM"),
            "importance": 0.5,
            "importance_rationale": "(fallback: no LLM score)",
            "novelty": 0.5,
            "novelty_rationale": "(fallback: no LLM score)",
            "impact": 0.5,
            "impact_rationale": "(fallback: no LLM score)",
            "key_insights": [],
            "tags": [],
            "top_articles": top,
            "evidence_articles": [],
        }

    def _pick_fallback_top(self, evt, embeddings):
        """Vector centroid fallback for top_articles."""
        from event_clustering import pick_nearest_to_centroid

        emb = embeddings if embeddings is not None and embeddings.size > 0 else np.zeros((0, 0), dtype=np.float32)
        return pick_nearest_to_centroid(
            evt.get("article_indices", []),
            emb,
            top_k=3,
        )

    async def curate_events(
        self, events: list[dict[str, Any]], embeddings: np.ndarray = None
    ) -> list[dict[str, Any]]:
        if not events:
            return []

        sem = asyncio.Semaphore(2)
        loop = asyncio.get_running_loop()

        batches = [events[i:i + BATCH_SIZE] for i in range(0, len(events), BATCH_SIZE)]
        logger.info(f"Event Curator: {len(events)} events in {len(batches)} batches")

        async def _do_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            async with sem:
                # Build prompt
                event_blocks = []
                # Track display→original index mapping per event in batch
                batch_remaps: list[dict[int, int]] = []

                for idx, evt in enumerate(batch):
                    articles = evt.get("articles", [])

                    # Pre-filter: if >20 articles, pick the 20 nearest-to-centroid
                    # to avoid token overflow.
                    display_articles = list(articles)
                    # reverse_remap: display_idx → original_event_idx
                    reverse_remap = {i: i for i in range(len(articles))}

                    if len(articles) > 20 and embeddings is not None and embeddings.size > 0:
                        from event_clustering import pick_nearest_to_centroid

                        top_20_global = pick_nearest_to_centroid(
                            evt.get("article_indices", []),
                            embeddings,
                            top_k=20,
                        )
                        top_20_set = set(top_20_global)
                        filtered = [
                            (i, a) for i, a in enumerate(articles)
                            if i < len(evt.get("article_indices", []))
                            and evt["article_indices"][i] in top_20_set
                        ]
                        if filtered:
                            display_articles = [a for _, a in filtered]
                            reverse_remap = {new: old for new, (old, _) in enumerate(filtered)}
                        else:
                            display_articles = articles[:20]

                    batch_remaps.append(reverse_remap)

                    # Compute cluster stats for prompt injection
                    total_count = len(articles)
                    unique_sources = len(set(
                        a.get("source_id", "") for a in articles
                    ))
                    published_strs = [a.get("published") for a in articles if a.get("published")]
                    if published_strs:
                        from datetime import datetime, timezone as tz
                        try:
                            dts = []
                            for s in published_strs:
                                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=tz.utc)
                                dts.append(dt)
                            span_hours = (max(dts) - min(dts)).total_seconds() / 3600
                            if span_hours < 1:
                                span_hours = 0
                        except Exception:
                            span_hours = 0
                    else:
                        span_hours = 0

                    stats_text = (
                        f"Total: {total_count} articles | "
                        f"{unique_sources} sources | "
                        f"span: {span_hours:.1f}h"
                    )

                    articles_text = ""
                    for i, a in enumerate(display_articles[:20]):
                        title = a.get("title", "")
                        summary = (a.get("summary", a.get("content_preview", "")) or "")[:400]
                        source = a.get("source_name", a.get("source_id", "Unknown"))
                        articles_text += f"  [{i}] {title} (from {source})\n      {summary}\n"
                    event_blocks.append(_EVENT_TEMPLATE.format(
                        idx=idx,
                        bucket=evt.get("bucket", "social"),
                        stats_text=stats_text,
                        articles_text=articles_text,
                    ))

                user_prompt = EVENT_CURATOR_USER_TEMPLATE.format(
                    batch_size=len(batch),
                    events="\n\n".join(event_blocks),
                )

                response = await loop.run_in_executor(
                    None, self.llm.chat, EVENT_CURATOR_SYSTEM, user_prompt,
                )

                # Parse
                try:
                    cleaned = response.strip()
                    for fence in ["```json", "```"]:
                        cleaned = cleaned.removeprefix(fence).removesuffix(fence).strip()
                    results = json.loads(cleaned)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Event Curator batch JSON parse failed: {e}")
                    return [self._fallback_event(evt, embeddings) for evt in batch]

                if isinstance(results, dict):
                    results = [results]

                curated = []
                for i, evt in enumerate(batch):
                    if i >= len(results) or not isinstance(results[i], dict):
                        curated.append(self._fallback_event(evt, embeddings))
                        continue

                    data = results[i]
                    cat_map = {
                        "Design Simulation": "设计仿真",
                        "Digital Twin": "数字孪生",
                        "AI for Science": "AI for Science",
                        "LLM": "LLM",
                        "Agent": "Agent",
                    }
                    raw_cat = data.get("category", "LLM")

                    # top_articles fallback: if LLM returned empty, use centroid
                    top_display = data.get("top_articles", [])
                    if not top_display:
                        top = self._pick_fallback_top(evt, embeddings)
                    else:
                        # Remap display indices → original event indices
                        rev_map = batch_remaps[i] if i < len(batch_remaps) else {j: j for j in range(len(evt.get("articles", [])))}
                        top = [rev_map.get(di, di) for di in top_display if di in rev_map]

                    # Validate evidence_articles: must be valid indices in the display set
                    display_count = len(display_articles) if 'display_articles' in dir() else len(evt.get("articles", []))
                    raw_evidence = data.get("evidence_articles", [])
                    valid_evidence: list[int] = []
                    for eid in raw_evidence:
                        if isinstance(eid, int) and 0 <= eid < display_count:
                            # Remap to original index
                            rev_map = batch_remaps[i] if i < len(batch_remaps) else {j: j for j in range(display_count)}
                            orig_idx = rev_map.get(eid, eid)
                            valid_evidence.append(orig_idx)
                    if raw_evidence and not valid_evidence:
                        logger.warning(
                            f"Event {idx}: evidence_articles {raw_evidence} "
                            f"all invalid for {display_count} articles — dropped"
                        )

                    curated.append({
                        **evt,
                        "is_relevant": data.get("is_relevant", False),
                        "event_title": data.get("event_title", evt.get("title", "")),
                        "event_summary": data.get("event_summary", evt.get("summary", ""))[:500],
                        "category": cat_map.get(raw_cat, raw_cat),
                        # 0-1 float scores
                        "importance": max(0, min(1, float(data.get("importance", 0.5)))),
                        "importance_rationale": (data.get("importance_rationale") or "")[:100],
                        "novelty": max(0, min(1, float(data.get("novelty", 0.5)))),
                        "novelty_rationale": (data.get("novelty_rationale") or "")[:100],
                        "impact": max(0, min(1, float(data.get("impact", 0.5)))),
                        "impact_rationale": (data.get("impact_rationale") or "")[:100],
                        "key_insights": data.get("key_insights", []),
                        "tags": data.get("tags", []),
                        "top_articles": top,
                        "evidence_articles": valid_evidence[:3],
                    })
                return curated

        batch_results = await asyncio.gather(*[_do_batch(b) for b in batches])
        all_events = [evt for batch in batch_results for evt in batch]

        # Filter relevant
        relevant = [e for e in all_events if e.get("is_relevant", True)]
        logger.info(f"Event Curator: {len(relevant)} relevant events (filtered from {len(all_events)})")

        return relevant
