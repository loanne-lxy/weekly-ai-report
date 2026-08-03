"""筛选与摘要 — 关键词预过滤 + LLM 分类 + 批量摘要"""
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
        """LLM 逐条分类到五大领域"""
        categories = self.filter_config["categories"]
        classified = []
        for a in articles[:80]:  # 限制单次分类数量
            cat = self.llm.classify(
                a.get("title", ""), a.get("summary", ""), categories
            )
            if cat and cat != "NONE":
                a["category"] = cat
                classified.append(a)
        logger.info(
            f"Classified: {len(articles)} → {len(classified)} in-category"
        )
        return classified

    def summarize_batch(self, articles: list[dict]) -> list[dict]:
        """LLM 批量摘要"""
        for a in articles:
            a["ai_summary"] = self.llm.summarize(
                a.get("title", ""), a.get("summary", "")
            )
        return articles
