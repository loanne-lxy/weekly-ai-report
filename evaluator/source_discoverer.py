"""Source pool auto-discovery — agent autonomously finds new sources"""
import logging
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

DISCOVERY_PROMPT = """You are an AI news source discovery assistant. Based on the titles and sources of the most relevant articles this week,
recommend 3 new RSS feeds, blogs, or websites that would provide similar high-quality content.
Return ONLY URLs, one per line, no explanation.

Top articles this week:
{articles}

New source URLs:"""


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
