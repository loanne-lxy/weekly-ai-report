"""Source pool auto-discovery — agent autonomously finds new sources"""
import json as _json
import logging
import re
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

DISCOVERY_PROMPT = """\
你正在分析本周高质量的 AI 技术文章，从中提取值得关注的信息源。

本周 Top 文章：
{articles}

请推荐 3 个新信息源（RSS feed 或技术博客）。要求：
1. 必须是真实存在的 RSS feed 或博客首页
2. 排除：arxiv.org, github.com, twitter.com/x.com, reddit.com, hackernews.com
3. 排除：知乎、微博、微信公众号、B站、字节、腾讯新闻等中文内容平台
4. 排除：已知的大流量媒体（techcrunch, theverge, wired, cnn, bbc 等）
5. 偏好：研究机构 blog、个人技术博客、垂直领域 newsletter 的 RSS

严格输出 JSON 数组，不要任何其他文字：
[
  {"endpoint": "https://example.com/feed.xml", "connector": "rss", "category": "LLM"},
  ...
]
"""

# Fallback: well-known quality sources organized by category — used when LLM fails
KNOWN_SOURCES = [
    # LLM / Agent
    {"endpoint": "https://lilianweng.github.io/index.xml", "connector": "rss", "category": "LLM"},
    {"endpoint": "https://jalammar.github.io/feed.xml", "connector": "rss", "category": "LLM"},
    {"endpoint": "https://huggingface.co/blog/feed.xml", "connector": "rss", "category": "LLM"},
    {"endpoint": "https://openai.com/blog/rss.xml", "connector": "rss", "category": "LLM"},
    {"endpoint": "https://www.anthropic.com/news/rss", "connector": "rss", "category": "LLM"},
    # Agent / Systems
    {"endpoint": "https://blog.langchain.dev/rss.xml", "connector": "rss", "category": "Agent"},
    {"endpoint": "https://www.aurelio.ai/feed/", "connector": "rss", "category": "Agent"},
    # AI for Science
    {"endpoint": "https://www.assemblyai.com/blog/rss.xml", "connector": "rss", "category": "AI for Science"},
    {"endpoint": "https://deepmind.com/blog/rss.xml", "connector": "rss", "category": "AI for Science"},
    # VLM / Multimodal
    {"endpoint": "https://www.embodied.com/blog/feed", "connector": "rss", "category": "Multimodal"},
    # Newsletters / curated
    {"endpoint": "https://simonwillison.net/atom/feed.xml", "connector": "rss", "category": "LLM"},
    {"endpoint": "https://newsletter.platformengineering.com/feed", "connector": "rss", "category": "Agent"},
]


def _extract_json_array(text: str) -> list:
    """Extract JSON array from LLM response, stripping markdown and prose."""
    # Strip markdown code block fences
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text.strip(), flags=re.MULTILINE)

    # Try direct parse
    try:
        parsed = _json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except _json.JSONDecodeError:
        pass

    # Try to find [...] in the text
    bracket_match = re.search(r'\[[\s\S]*\]', text)
    if bracket_match:
        try:
            parsed = _json.loads(bracket_match.group())
            if isinstance(parsed, list):
                return parsed
        except _json.JSONDecodeError:
            pass

    # Last resort: try each line as JSON object
    results = []
    for line in text.split('\n'):
        line = line.strip().lstrip('- ').strip()
        if not line or line in ('[', ']'):
            continue
        try:
            obj = _json.loads(line)
            if isinstance(obj, dict):
                results.append(obj)
        except _json.JSONDecodeError:
            pass
    return results


def _infer_type(url: str) -> str:
    """Infer connector from URL pattern."""
    if 'github.com/' in url:
        return 'github'
    if any(ext in url.lower() for ext in ('.rss', '.xml', '/feed', '/rss/', 'hnrss.org')):
        return 'rss'
    return 'rss'  # default


def _infer_category(articles: list[dict]) -> str:
    """Infer category from the articles that triggered this discovery."""
    cats = {}
    for a in articles:
        c = a.get('category', 'LLM')
        cats[c] = cats.get(c, 0) + 1
    if cats:
        return max(cats, key=cats.get)
    return 'LLM'


class SourceDiscoverer:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._fail_count = 0  # Track consecutive failures

    def discover(self, articles: list[dict], existing_endpoints: set[str]) -> list[dict]:
        """Based on top articles, recommend new info sources via LLM + fallback."""
        top_articles = sorted(
            articles, key=lambda a: a.get('priority_score', 0), reverse=True
        )[:5]

        if not top_articles:
            logger.info("No articles to base discovery on")
            return []

        article_text = "\n".join(
            f"- [{a.get('category', '')}] {a.get('title', '')[:100]} "
            f"(score: {a.get('priority_score', '?')})"
            for a in top_articles
        )

        new_sources = []

        # ── Try LLM discovery ────────────────────────────────
        try:
            response = self.llm.chat(
                system_prompt=(
                    "你是一个 AI 信息源发现引擎。根据高质量文章推荐相关 RSS feed 或技术博客。"
                ),
                user_prompt=DISCOVERY_PROMPT.format(articles=article_text),
            )

            candidates = _extract_json_array(response)
            if candidates:
                self._fail_count = 0  # Reset on success
            else:
                raise ValueError(f"LLM returned no parseable JSON: {response[:200]}")

            for obj in candidates:
                endpoint = (obj.get('endpoint', '') or obj.get('url', '')).strip()
                if not endpoint.startswith('http') or endpoint in existing_endpoints:
                    continue

                new_sources.append({
                    'name': self._extract_domain(endpoint),
                    'endpoint': endpoint,
                    'connector': obj.get('connector', _infer_type(endpoint)),
                    'category': obj.get('category', _infer_category(top_articles)),
                    'weight': 5,
                    'discovered_by': 'llm_agent',
                })
                logger.info(
                    f"LLM Discovered: {endpoint} "
                    f"(conn={obj.get('connector', 'rss')}, cat={obj.get('category', 'LLM')})"
                )

        except Exception as e:
            self._fail_count += 1
            logger.warning(f"LLM source discovery failed ({self._fail_count} consecutive): {e}")
            new_sources = []  # Clear any partial results on failure

        # ── Fallback: known quality sources ──────────────────
        if not new_sources:
            logger.info(f"Fallback: trying {len(KNOWN_SOURCES)} known sources...")
            for src in KNOWN_SOURCES:
                if src['endpoint'] not in existing_endpoints:
                    new_sources.append({
                        'name': self._extract_domain(src['endpoint']),
                        'endpoint': src['endpoint'],
                        'connector': src['connector'],
                        'category': src['category'],
                        'weight': 3,  # Lower weight for fallback sources
                        'discovered_by': 'known_fallback',
                    })

        return new_sources

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract readable domain from URL."""
        try:
            return url.split('://')[-1].split('/')[0]
        except Exception:
            return url
