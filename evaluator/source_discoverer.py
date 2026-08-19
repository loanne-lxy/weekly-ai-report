"""Source pool auto-discovery — agent autonomously finds new sources"""
import logging
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

DISCOVERY_PROMPT = """You are an AI news source discovery assistant. Based on the titles and sources of the most relevant articles this week,
recommend up to 3 new information sources that would provide similar high-quality content.

For each source return a JSON object with:
- endpoint: the RSS feed or blog URL
- connector: one of "rss", "web", "github_repo"
- category: one of "LLM", "Agent", "AI4Science", "DesignSimulation", "DigitalTwin"

Example output (one JSON per line, no markdown):
{"endpoint": "https://example.com/feed.xml", "connector": "rss", "category": "LLM"}

Top articles this week:
{articles}

New sources:"""


def _infer_type(url: str) -> str:
    """Infer source type from URL pattern."""
    if 'github.com/' in url:
        return 'github_repo'
    if any(ext in url.lower() for ext in ('.rss', '.xml', '/feed', 'hnrss.org')):
        return 'rss'
    return 'rss'  # default


def _infer_category(articles: list[dict]) -> str:
    """Infer category from the articles that triggered this discovery."""
    cats = {}
    for a in articles:
        c = a.get('category', 'LLM')
        cats[c] = cats.get(c, 0) + 1
    # Return the dominant category
    if cats:
        return max(cats, key=cats.get)
    return 'LLM'


class SourceDiscoverer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def discover(self, articles: list[dict], existing_urls: set[str]) -> list[dict]:
        """基于本周热门文章，让LLM推荐新信息源（结构化输出）"""
        # 取各类别评分最高的文章作为上下文
        top_articles = sorted(
            articles, key=lambda a: a.get('priority_score', 0), reverse=True
        )[:5]

        if not top_articles:
            logger.info("No articles to base discovery on")
            return []

        article_text = "\n".join(
            f"- [{a.get('category', '')}] {a.get('title', '')[:100]} (score: {a.get('priority_score', '?')})"
            for a in top_articles
        )

        response = self.llm.chat(
            system_prompt=(
                "你是AI信息源发现引擎。根据本周高质量文章，推荐新信息源。"
                "返回 JSON 格式，每个源一行。type 选 rss/web/github_repo，"
                "category 选 LLM/Agent/AI for Science/设计仿真/数字孪生。"
            ),
            user_prompt=DISCOVERY_PROMPT.format(articles=article_text),
        )

        new_sources = []
        import json as _json
        for line in response.strip().split("\n"):
            line = line.strip().lstrip('-').strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
                endpoint = obj.get('endpoint', obj.get('url', '')).strip()
                if not endpoint.startswith('http') or endpoint in existing_urls:
                    continue
                new_sources.append({
                    'name': endpoint.split('//')[-1].split('/')[0],
                    'endpoint': endpoint,
                    'connector': obj.get('connector', obj.get('type', _infer_type(endpoint))),
                    'category': obj.get('category', _infer_category(top_articles)),
                    'weight': 5,
                    'discovered_by': 'agent',
                })
                logger.info(
                    f"Discovered: {endpoint} (connector={obj.get('connector', 'rss')}, "
                    f"cat={obj.get('category', 'LLM')})"
                )
            except Exception:
                # Fallback: try plain URL
                url = line.strip()
                if url.startswith('http') and url not in existing_urls:
                    new_sources.append({
                        'name': url.split('//')[-1].split('/')[0],
                        'endpoint': url,
                        'connector': _infer_type(url),
                        'category': _infer_category(top_articles),
                        'weight': 5,
                        'discovered_by': 'agent',
                    })
                    logger.info(f"Discovered (fallback): {url}")

        return new_sources
