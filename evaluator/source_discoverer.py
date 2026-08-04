"""源池自动发现 — Agent 自主寻找新信息源"""
import logging
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

DISCOVERY_PROMPT = """你是AI资讯源发现助手。给出以下本周最相关的5篇文章的标题和来源，
推荐3个新的RSS/博客/网站作为信息源，这些源应该能提供类似的高质量内容。
只返回URL，每行一个，不要解释。

本周热门文章:
{articles}

新信息源URL:"""


class SourceDiscoverer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def discover(self, articles: list[dict], existing_urls: set[str]) -> list[dict]:
        """基于本周热门文章，让LLM推荐新信息源"""
        # 取各类别评分最高的文章作为上下文
        top_articles = sorted(
            articles, key=lambda a: len(a.get("ai_summary", "")), reverse=True
        )[:5]

        if not top_articles:
            logger.info("No articles to base discovery on")
            return []

        article_text = "\n".join(
            f"- [{a.get('category', '')}] {a.get('title', '')[:100]}"
            for a in top_articles
        )

        response = self.llm.chat(
            system_prompt="你是AI信息源发现引擎。",
            user_prompt=DISCOVERY_PROMPT.format(articles=article_text),
        )

        new_sources = []
        for line in response.strip().split("\n"):
            url = line.strip()
            if url.startswith("http") and url not in existing_urls:
                new_sources.append({
                    "name": url.split("//")[-1].split("/")[0],
                    "url": url,
                    "type": "rss",
                    "category": "LLM",
                    "weight": 5,
                    "discovered_by": "agent",
                })
                logger.info(f"Discovered new source: {url}")

        return new_sources
