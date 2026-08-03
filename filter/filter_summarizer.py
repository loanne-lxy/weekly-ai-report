"""筛选与摘要 — 关键词预过滤 + LLM 分类 + 批量摘要"""
import asyncio
import logging
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)


class FilterSummarizer:
    def __init__(self, llm: LLMClient, config: dict):
        self.llm = llm
        self.filter_config = config["filter"]

    def keyword_pre_filter(self, articles: list[dict]) -> list[dict]:
        """关键词预过滤 — 快速筛掉完全不相关的"""
        keywords = self.filter_config.get("pre_filter_keywords", [])
        result = []
        for a in articles:
            text = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if any(kw.lower() in text for kw in keywords):
                result.append(a)
        logger.info(f"Pre-filter: {len(articles)} → {len(result)}")
        return result

    def classify_batch(self, articles: list[dict]) -> list[dict]:
        """LLM 并发分类到五大领域"""
        return asyncio.run(self._classify_async(articles))

    async def _classify_async(self, articles: list[dict]) -> list[dict]:
        categories = self.filter_config["categories"]
        sem = asyncio.Semaphore(5)
        classified = []

        async def _do_one(a: dict):
            async with sem:
                loop = asyncio.get_running_loop()
                cat = await loop.run_in_executor(
                    None,
                    self.llm.classify,
                    a.get("title", ""),
                    a.get("summary", ""),
                    categories,
                )
                if cat and cat != "NONE":
                    a["category"] = cat
                    classified.append(a)

        await asyncio.gather(*[_do_one(a) for a in articles[:80]])
        logger.info(
            f"Classified: {len(articles)} → {len(classified)} in-category"
        )
        return classified

    def summarize_batch(self, articles: list[dict]) -> list[dict]:
        """LLM 并发批量摘要"""
        return asyncio.run(self._summarize_async(articles))

    async def _summarize_async(self, articles: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(5)  # 最多5个并发

        async def _do_one(a: dict):
            async with sem:
                loop = asyncio.get_running_loop()
                a["ai_summary"] = await loop.run_in_executor(
                    None,
                    self.llm.summarize,
                    a.get("title", ""),
                    a.get("summary", ""),
                )

        await asyncio.gather(*[_do_one(a) for a in articles])
        return articles
