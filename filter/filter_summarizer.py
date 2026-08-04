"""筛选与摘要 — 关键词预过滤 + LLM 分类 + 并发摘要/评分/中文标题"""
import asyncio
import logging
import json
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

ENRICH_PROMPT = """分析以下AI资讯，返回JSON（不要markdown代码块，只返回纯JSON）:

{{
  "chinese_title": "25字以内中文标题，准确概括核心内容",
  "summary": "80字以内中文摘要，点出关键技术/突破/数据",
  "importance": 1-10的整数，评分标准:
    10: 里程碑式突破(新模型架构/范式改变/行业颠覆)
    8-9: 重大发布(GPT-5/Claude级别/顶级会议Best Paper)
    6-7: 重要进展(新方法/新工具/大公司战略)
    4-5: 增量改进(微调/benchmark小幅提升)
    1-3: 一般资讯(博客/观点/应用案例)
}}

标题: {title}
原文摘要: {summary}

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
                    "你是AI资讯分析引擎。只返回纯JSON，不包含markdown代码块。",
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
