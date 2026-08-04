"""Filtering & enrichment — keyword pre-filter + unified LLM curator evaluation"""
import asyncio
import json
import logging
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

CURATOR_PROMPT = """# Role
You are a senior technology news curator and intelligence analyst specializing in tracking global AI frontiers and industrial technology evolution. Analyze the given news article and output ONLY valid JSON.

# Categories
1. **LLM** — Core Large Language Model Technology: pre-training, fine-tuning, architecture (MoE, Mamba, Attention variants), alignment (RLHF/DPO), reasoning, open-weight releases, token/latency optimization, multimodal API updates. NOT application stories.
2. **Agent** — AI Autonomous Agents: agent architectures, multi-agent collaboration, tool calling (MCP), memory, task planning/execution, agent frameworks (LangChain, CrewAI, AutoGen), coding/browser agents, safety guardrails.
3. **AI for Science** — AI-driven Scientific Discovery: biomedicine (AIDD, protein), materials science, quantum chemistry, weather, PINN, theorem proving.
4. **Design Simulation** — AI-assisted Engineering Design: generative design, CAD/CAE, physics simulation, chip EDA+AI.
5. **Digital Twin** — Industrial Digital Twins: real-time rendering (Omniverse), CPS, IoT+AI modeling, cloud platforms, standards.

# Tasks
1. **Relevance**: Does it belong to one of the 5 domains? Filter out: pure PR, marketing hype without technical detail, duplicative reporting, generic entertainment tech.
2. **Priority**: Score 1-5. Boost LLM/Agent content.
3. **Category**: Primary + optional secondary.
4. **Summary**: Structured extraction.

# Output JSON (ONLY valid JSON, no markdown):
{{
  "is_relevant": true/false,
  "priority_score": 1-5,
  "primary_category": "One of: LLM, Agent, AI for Science, Design Simulation, Digital Twin",
  "secondary_category": "Optional second domain or null",
  "chinese_title": "Concise Chinese headline under 25 chars",
  "tldr": "One-sentence summary under 50 words",
  "key_insights": ["Up to 3 critical points"],
  "why_it_matters": "1-2 sentences on industry/dev significance",
  "tags": ["tag1", "tag2"]
}}

Title: {title}
Source: {source_name}
Summary: {summary}
URL: {url}

JSON:"""


class FilterSummarizer:
    def __init__(self, llm: LLMClient, config: dict):
        self.llm = llm
        self.filter_config = config["filter"]

    def keyword_pre_filter(self, articles: list[dict]) -> list[dict]:
        """Fast keyword pre-filter — no LLM calls"""
        keywords = self.filter_config.get("pre_filter_keywords", [])
        result = []
        for a in articles:
            text = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if any(kw.lower() in text for kw in keywords):
                result.append(a)
        logger.info(f"Pre-filter: {len(articles)} → {len(result)}")
        return result

    def curate_batch(self, articles: list[dict]) -> list[dict]:
        """Unified curator: relevance + classification + enrichment in one LLM call per article"""
        return asyncio.run(self._curate_async(articles))

    async def _curate_async(self, articles: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(5)
        curated = []

        async def _do_one(a: dict):
            async with sem:
                loop = asyncio.get_running_loop()
                prompt = CURATOR_PROMPT.format(
                    title=a.get("title", "")[:300],
                    source_name=a.get("source_name", ""),
                    summary=a.get("summary", "")[:800],
                    url=a.get("url", ""),
                )
                response = await loop.run_in_executor(
                    None, self.llm.chat,
                    "You are a senior technology news curator. Return ONLY valid JSON without markdown fences.",
                    prompt,
                )
                try:
                    # Clean markdown fences
                    cleaned = response.strip()
                    for fence in ["```json", "```"]:
                        cleaned = cleaned.removeprefix(fence).removesuffix(fence).strip()
                    data = json.loads(cleaned)

                    if data.get("is_relevant", False):
                        a["priority_score"] = int(data.get("priority_score", 3))
                        a["category"] = data.get("primary_category", "LLM")
                        a["secondary_category"] = data.get("secondary_category")
                        a["chinese_title"] = data.get("chinese_title", a.get("title", ""))
                        a["tldr"] = data.get("tldr", "")
                        a["key_insights"] = data.get("key_insights", [])
                        a["why_it_matters"] = data.get("why_it_matters", "")
                        a["tags"] = data.get("tags", [])
                        a["importance"] = a["priority_score"]  # for backward compat
                        a["ai_summary"] = a["tldr"]  # for backward compat
                        curated.append(a)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"JSON parse failed for [{a.get('title','')[:60]}]: {e}")

        await asyncio.gather(*[_do_one(a) for a in articles[:80]])
        logger.info(f"Curator: {len(articles)} → {len(curated)} relevant")
        return curated
