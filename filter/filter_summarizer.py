"""Filtering & enrichment — keyword pre-filter + unified LLM curator evaluation"""
import asyncio
import json
import logging
from pathlib import Path
from models.llm_client import LLMClient
from dedup.curator_cache import CuratorCache

logger = logging.getLogger(__name__)

# Load modular prompt components from references/
PROMPT_DIR = Path(__file__).parent.parent / "references"


def _load_prompt(filename: str) -> str:
    """Load a prompt module from references/ directory"""
    path = PROMPT_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    logger.warning(f"Prompt file not found: {path}")
    return ""


CURATION_RULES = _load_prompt("curation-rules.md")
DIGEST_PROMPT = _load_prompt("digest-prompt.md")

CURATOR_SYSTEM = (
    "You are a senior technology news curator and intelligence analyst "
    "specializing in tracking global AI frontiers and industrial technology evolution. "
    "Analyze the given news article and output ONLY valid JSON without markdown fences."
)

CURATOR_USER_TEMPLATE = """{rules}

{digest}

# Input
Title: {title}
Source: {source_name}
Summary: {summary}
URL: {url}

Return ONLY valid JSON (no markdown fences):"""


class FilterSummarizer:
    def __init__(self, llm: LLMClient, config: dict):
        self.llm = llm
        self.filter_config = config["filter"]
        self.cache = CuratorCache()

    def keyword_pre_filter(self, articles: list[dict]) -> list[dict]:
        """Fast keyword pre-filter — no LLM calls"""
        keywords = self.filter_config.get("pre_filter_keywords", [])
        result = []
        for a in articles:
            text = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if any(kw.lower() in text for kw in keywords):
                result.append(a)
        logger.info(f"Pre-filter: {len(articles)} -> {len(result)}")
        return result

    def curate_batch(self, articles: list[dict]) -> list[dict]:
        """Unified curator: relevance + classification + enrichment in one LLM call"""
        return asyncio.run(self._curate_async(articles))

    async def _curate_async(self, articles: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(3)
        curated = []

        async def _do_one(a: dict):
            async with sem:
                title = a.get("title", "")[:300]
                summary = a.get("summary", "")[:800]
                source_name = a.get("source_name", "")

                # Check cache first
                cached = self.cache.get(title, summary)
                if cached:
                    a.update(cached)
                    curated.append(a)
                    return

                # Cache miss — call LLM
                loop = asyncio.get_running_loop()
                user_prompt = CURATOR_USER_TEMPLATE.format(
                    rules=CURATION_RULES,
                    digest=DIGEST_PROMPT,
                    title=title,
                    source_name=source_name,
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
                        a["priority_score"] = int(data.get("priority_score", 3))
                        raw_cat = data.get("primary_category", "LLM")
                        # Normalize English category names to Chinese
                        cat_map = {
                            "Design Simulation": "设计仿真",
                            "Digital Twin": "数字孪生",
                            "AI for Science": "AI for Science",
                            "LLM": "LLM",
                            "Agent": "Agent",
                        }
                        cat = cat_map.get(raw_cat, raw_cat)
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

                        # Store in cache for future reuse
                        self.cache.set(title, summary, data, source_name)

                        curated.append(a)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"JSON parse failed for [{a.get('title','')[:60]}]: {e}")

        await asyncio.gather(*[_do_one(a) for a in articles[:120]])
        logger.info(
            f"Curator: {len(articles)} → {len(curated)} relevant "
            f"(cache: {self.cache.stats['hits']} hits, {self.cache.stats['misses']} misses)"
        )
        return curated
