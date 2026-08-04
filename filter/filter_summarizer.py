"""筛选与摘要 — 关键词预过滤 + LLM 分类 + 并发摘要/评分/中文标题"""
import asyncio
import logging
import json
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

ENRICH_PROMPT = """Analyze the following AI news article. Return ONLY raw JSON (no markdown fences):

{{
  "chinese_title": "Accurate Chinese title within 25 characters",
  "summary": "Concise Chinese summary within 80 characters, highlighting key tech/breakthrough/data",
  "importance": integer 1-10, scoring criteria:
    10: Landmark breakthrough (new model architecture, paradigm shift, industry disruption)
    8-9: Major release (GPT-5/Claude-level, top-conference Best Paper)
    6-7: Significant advance (new method/tool, big-tech strategy)
    4-5: Incremental improvement (fine-tuning, minor benchmark gains)
    1-3: General news (blog post, opinion, application case)
}}

Title: {title}
Original summary: {summary}

JSON:"""


class FilterSummarizer:
    def __init__(self, llm: LLMClient, config: dict):
        self.llm = llm
        self.filter_config = config["filter"]

    def keyword_pre_filter(self, articles: list[dict]) -> list[dict]:
        keywords = self.filter_config.get("pre_filter_keywords", [])
        result = []
        for a in articles:
            text = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if any(kw.lower() in text for kw in keywords):
                result.append(a)
        logger.info(f"Pre-filter: {len(articles)} → {len(result)}")
        return result

    def classify_batch(self, articles: list[dict]) -> list[dict]:
        return asyncio.run(self._classify_async(articles))

    async def _classify_async(self, articles: list[dict]) -> list[dict]:
        categories = self.filter_config["categories"]
        sem = asyncio.Semaphore(5)
        classified = []

        async def _do_one(a: dict):
            async with sem:
                loop = asyncio.get_running_loop()
                cat = await loop.run_in_executor(
                    None, self.llm.classify,
                    a.get("title", ""), a.get("summary", ""), categories,
                )
                if cat and cat != "NONE":
                    a["category"] = cat
                    classified.append(a)

        await asyncio.gather(*[_do_one(a) for a in articles[:80]])
        logger.info(f"Classified: {len(articles)} → {len(classified)} in-category")
        return classified

    def enrich_batch(self, articles: list[dict]) -> list[dict]:
        """并发生成中文标题 + 摘要 + 重要性评分"""
        return asyncio.run(self._enrich_async(articles))

    async def _enrich_async(self, articles: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(5)

        async def _do_one(a: dict):
            async with sem:
                loop = asyncio.get_running_loop()
                prompt = ENRICH_PROMPT.format(
                    title=a.get("title", "")[:200],
                    summary=a.get("summary", "")[:500],
                )
                response = await loop.run_in_executor(
                    None, self.llm.chat,
                    "You are an AI analysis engine. Return ONLY raw JSON without markdown fences.",
                    prompt,
                )
                try:
                    data = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
                    a["chinese_title"] = data.get("chinese_title", a.get("title", ""))
                    a["ai_summary"] = data.get("summary", a.get("ai_summary", ""))
                    a["importance"] = int(data.get("importance", 5))
                except (json.JSONDecodeError, ValueError):
                    a["chinese_title"] = a.get("title", "")
                    a["ai_summary"] = response[:100]
                    a["importance"] = 5

        await asyncio.gather(*[_do_one(a) for a in articles])
        return articles
