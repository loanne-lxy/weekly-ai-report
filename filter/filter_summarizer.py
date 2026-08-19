"""Filtering & enrichment — unified LLM curator evaluation.

Batch mode: groups articles into batches of 5, sends one LLM call per batch.
5x fewer HTTP round-trips → ~5x faster.
"""
import asyncio
import json
import logging
from pathlib import Path
from models.llm_client import LLMClient
from dedup.curator_cache import CuratorCache
from extractors.contract import CuratedArticle

logger = logging.getLogger(__name__)

# Load modular prompt components from references/
PROMPT_DIR = Path(__file__).parent.parent / "references"


def _load_prompt(filename: str) -> str:
    path = PROMPT_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Prompt file not found: {path}")
    return ""


CURATION_RULES = _load_prompt("curation-rules.md")
DIGEST_PROMPT = _load_prompt("digest-prompt.md")

# Batch size per LLM call
BATCH_SIZE = 5
# Summary length for batch mode
BATCH_SUMMARY_LEN = 500

CURATOR_SYSTEM = (
    "You are a senior technology news curator and intelligence analyst "
    "specializing in tracking global AI frontiers and industrial technology evolution. "
    "You will be given multiple articles. For each, decide if it is relevant to AI frontiers "
    "and if so, produce a structured analysis.\n"
    "Classification prior: some articles carry a source hint like "
    "'(Source specializes in: X)'. Use this as a Bayesian prior — prefer "
    "category X when the article content is ambiguous, but override it when "
    "the content clearly belongs elsewhere. The article itself is always the "
    "ultimate authority, not the source.\n"
    "Output ONLY a valid JSON array without markdown fences."
)

# Template for a single article (fallback when batch fails)
CURATOR_USER_TEMPLATE_SINGLE = """{rules}

{digest}

# Input
Title: {title}
Source: {source_name}{prior_hint}
URL: {url}
Summary: {summary}

Return ONLY valid JSON (no markdown fences):"""

# Template for a single article inside a batch
_ARTICLE_TEMPLATE = """--- Article {idx} ---
Title: {title}
Source: {source_name}{prior_hint}
URL: {url}
Summary: {summary}"""

CURATOR_USER_TEMPLATE = """{rules}

{digest}

# Input — Batch of {batch_size} articles
{articles}

# Output format
Return ONLY a valid JSON array (no markdown fences). Each element corresponds to the article above in order:
[
  {{
    "article_index": 0,
    "is_relevant": true/false,
    "priority_score": 1-10,
    "primary_category": "LLM|Agent|AI for Science|Design Simulation|Digital Twin",
    "secondary_category": "...",
    "chinese_title": "...",
    "tldr": "...",
    "key_insights": ["..."],
    "why_it_matters": "...",
    "tags": ["..."]
  }},
  ...
]

If an article is NOT relevant, still include it with "is_relevant": false and minimal other fields."""

# English → Chinese category normalization
_CAT_MAP = {
    "Design Simulation": "设计仿真",
    "Digital Twin": "数字孪生",
    "AI for Science": "AI for Science",
    "LLM": "LLM",
    "Agent": "Agent",
}


def _prior_hint(article: dict) -> str:
    """Build source prior hint for LLM — uses 'category' field."""
    cat = article.get("category") or article.get("default_category")
    if cat:
        return f" (Source specializes in: {cat})"
    return ""


class FilterSummarizer:
    """LLM-powered curator: relevance + classification + enrichment (batch mode)."""

    def __init__(self, llm: LLMClient, config: dict):
        self.llm = llm
        self.cache = CuratorCache()

    def curate_batch(self, articles: list[dict]) -> list[dict]:
        """Sync wrapper for _curate_async."""
        return asyncio.run(self._curate_async(articles))

    async def _curate_async(self, articles: list[dict]) -> list[dict]:
        """
        Process all articles through LLM curator in BATCHES.

        - First, check cache for each article (hits are applied immediately).
        - Remaining uncached articles are grouped into batches of BATCH_SIZE.
        - Each batch is sent to LLM in one call, concurrency=3.

        Returns:
            list of curated article dicts
        """
        sem = asyncio.Semaphore(3)
        curated: list[dict] = []
        uncached: list[dict] = []  # (index_in_articles, article_dict)

        # ── Phase 1: Cache check ────────────────────────────────
        for a in articles:
            title = a.get("title", "")[:300]
            summary = a.get("summary", "")[:800]
            source_name = a.get("source_name", "")
            cached = self.cache.get(title, summary)
            if cached:
                self._apply_result(a, cached)
                curated.append(a)
            else:
                uncached.append(a)

        if not uncached:
            logger.info(f"Curator: all {len(articles)} articles from cache")
            return curated

        logger.info(
            f"Curator: {len(articles)} total, "
            f"{len(articles) - len(uncached)} cached, "
            f"{len(uncached)} need LLM ({(len(uncached) + BATCH_SIZE - 1) // BATCH_SIZE} batches)"
        )

        # ── Phase 2: Batch LLM calls ────────────────────────────
        batches = [
            uncached[i:i + BATCH_SIZE]
            for i in range(0, len(uncached), BATCH_SIZE)
        ]

        loop = asyncio.get_running_loop()

        async def _do_one(a: dict):
            """Process a single article via LLM (used as fallback when batch fails)."""
            async with sem:
                title = a.get("title", "")[:300]
                summary = a.get("summary", "")[:800]
                source_name = a.get("source_name", "")
                user_prompt = CURATOR_USER_TEMPLATE_SINGLE.format(
                    rules=CURATION_RULES,
                    digest=DIGEST_PROMPT,
                    title=title,
                    source_name=source_name,
                    prior_hint=_prior_hint(a),
                    summary=summary,
                    url=a.get("url", ""),
                )
                response = await loop.run_in_executor(
                    None, self.llm.chat, CURATOR_SYSTEM, user_prompt,
                )
                try:
                    cleaned = response.strip()
                    for fence in ["```json", "```"]:
                        cleaned = cleaned.removeprefix(fence).removesuffix(fence).strip()
                    data = json.loads(cleaned)
                    if data.get("is_relevant", False):
                        self._apply_result(a, data)
                        self.cache.set(title, summary, data, source_name)
                        curated.append(a)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"Single JSON parse failed for [{title[:60]}]: {e}")

        async def _do_batch(batch: list[dict]):
            async with sem:
                # Build batch prompt
                article_blocks = []
                for idx, a in enumerate(batch):
                    title = a.get("title", "")[:300]
                    summary = a.get("summary", "")[:BATCH_SUMMARY_LEN]
                    source_name = a.get("source_name", "")
                    prior = _prior_hint(a)
                    article_blocks.append(_ARTICLE_TEMPLATE.format(
                        idx=idx,
                        title=title,
                        source_name=source_name,
                        prior_hint=prior,
                        url=a.get("url", ""),
                        summary=summary,
                    ))

                user_prompt = CURATOR_USER_TEMPLATE.format(
                    rules=CURATION_RULES,
                    digest=DIGEST_PROMPT,
                    batch_size=len(batch),
                    articles="\n\n".join(article_blocks),
                )

                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None, self.llm.chat, CURATOR_SYSTEM, user_prompt,
                )

                # Parse response as JSON array
                try:
                    cleaned = response.strip()
                    for fence in ["```json", "```"]:
                        cleaned = cleaned.removeprefix(fence).removesuffix(fence).strip()
                    results = json.loads(cleaned)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Batch JSON parse failed ({len(batch)} articles): {e}. Fallback: process individually.")
                    # Fallback: process each article individually
                    for a in batch:
                        await _do_one(a)
                    return

                if isinstance(results, dict):
                    results = [results]

                # Apply results to corresponding articles
                for i, a in enumerate(batch):
                    if i >= len(results):
                        logger.warning(f"Batch result shorter than batch ({i}/{len(batch)})")
                        continue
                    data = results[i]
                    title = a.get("title", "")[:300]
                    summary = a.get("summary", "")[:800]
                    source_name = a.get("source_name", "")

                    # Validate article_index matches (optional safety check)
                    if data.get("article_index") is not None and data.get("article_index") != i:
                        logger.warning(f"article_index mismatch: expected {i}, got {data.get('article_index')} for [{title[:50]}]")

                    if data.get("is_relevant", False):
                        self._apply_result(a, data)
                        self.cache.set(title, summary, data, source_name)
                        curated.append(a)

        await asyncio.gather(*[_do_batch(b) for b in batches])

        logger.info(
            f"Curator: {len(articles)} -> {len(curated)} relevant "
            f"(cache: {self.cache.stats['hits']} hits, {self.cache.stats['misses']} misses)"
        )
        return curated

    @staticmethod
    def _apply_result(a: dict, data: dict):
        """Apply LLM result to article dict (mutates in-place)."""
        raw_cat = data.get("primary_category", "LLM")
        cat = _CAT_MAP.get(raw_cat, raw_cat)

        a["priority_score"] = int(data.get("priority_score", 3))
        a["primary_category"] = cat
        a["category"] = cat  # backward compat
        a["secondary_category"] = data.get("secondary_category")
        a["chinese_title"] = data.get("chinese_title", a.get("title", ""))
        a["tldr"] = data.get("tldr", "")
        a["key_insights"] = data.get("key_insights", [])
        a["why_it_matters"] = data.get("why_it_matters", "")
        a["tags"] = data.get("tags", [])
        a["original_title"] = data.get("original_title", a.get("title", ""))
        a["importance"] = a["priority_score"]
        a["ai_summary"] = a["tldr"]
