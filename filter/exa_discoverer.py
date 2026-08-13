"""主动搜索扩展 — 用 Exa API 搜索前沿技术博客（策略 3）"""
import os
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 排除通用平台域名
_BLOCKED = {
    'twitter.com', 'x.com', 'youtube.com', 'github.com',
    'wikipedia.org', 'arxiv.org', 'bilibili.com',
    'zhihu.com', 'weibo.com', 'qq.com', 'weixin.qq.com',
    'mp.weixin.qq.com',
    'medium.com', 'substack.com', 'dev.to',
    'linkedin.com', 'facebook.com', 'instagram.com',
    'reddit.com', 'news.ycombinator.com',
    'amazon.com', 'google.com', 'microsoft.com',
    'huggingface.co', 'paperswithcode.com',
    'openai.com', 'anthropic.com',
    'npmjs.com', 'pypi.org', 'crates.io',
    'blog.google', 'research.google',
    'nvidia.com', 'blogs.nvidia.com',
}

# Exa 搜索类别（优先技术博客）
_VALID_CONTENT_TYPES = ('article', 'blog')


def _normalize_domain(url: str) -> str:
    """URL → 规范化根域名"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or parsed.netloc
        parts = hostname.split('.')
        if parts[0] in ('www', 'blog', 'news', 'dev', 'docs'):
            parts = parts[1:]
        return '.'.join(parts) if parts else hostname
    except Exception:
        return ''


def _extract_keywords(articles: list[dict]) -> list[str]:
    """从高分文章的 tags + category + title 中提取搜索关键词

    不需要外部 trends 输入，直接从文章数据中提炼。
    """
    # 1. 统计高频 category（作为搜索信号）
    cats = {}
    for a in articles:
        c = a.get('category', '')
        cats[c] = cats.get(c, 0) + 1

    # 2. 统计高频 tags（作为搜索信号）
    tags = {}
    for a in articles:
        for t in a.get('tags', []):
            tags[t] = tags.get(t, 0) + 1

    keywords = []
    seen = set()

    # 优先用高频 tag（具体技术词）
    for tag, count in sorted(tags.items(), key=lambda x: -x[1]):
        if tag not in seen and len(keywords) < 5:
            seen.add(tag)
            keywords.append(tag)

    # 补充 category
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        if cat and cat not in seen and len(keywords) < 5:
            seen.add(cat)
            keywords.append(cat)

    return keywords[:5]


def discover(
    articles: list[dict],
    existing_domains: set[str],
    api_key: str | None = None,
) -> list[dict]:
    """基于本周文章，用 Exa API 主动搜索高质量技术博客

    Args:
        articles: 本周文章列表
        existing_domains: 已有源域名，避免重复
        api_key: Exa API key (default: EXA_API_KEY env)

    Returns:
        新源候选列表
    """
    api_key = api_key or os.environ.get('EXA_API_KEY', '')
    if not api_key:
        logger.warning("Exa discovery: EXA_API_KEY not set, skipping")
        return []

    keywords = _extract_keywords(articles)
    if not keywords:
        logger.info("Exa discovery: no keywords extracted")
        return []

    logger.info(f"Exa discovery: searching for keywords: {keywords}")

    try:
        from exa_py import Exa
        client = Exa(api_key=api_key)
    except ImportError:
        logger.warning("Exa discovery: exa-py not installed (pip install exa-py)")
        return []

    new_sources = []
    found_domains: dict[str, dict] = {}

    for kw in keywords[:3]:  # 最多搜 3 个关键词，避免配额浪费
        query = f"{kw} AI technical blog analysis"
        try:
            results = client.search(
                query,
                type="neural",
                num_results=10,
                start_published_date="2025-01-01",
            )
        except Exception as e:
            logger.warning(f"Exa search failed for '{kw}': {e}")
            continue

        for r in (results.results or []):
            domain = _normalize_domain(r.url)
            if not domain or domain in _BLOCKED or domain in existing_domains:
                continue
            if domain in found_domains:
                found_domains[domain]['count'] += 1
                continue

            found_domains[domain] = {
                'name': domain,
                'url': r.url,
                'type': 'web',
                'category': 'LLM',  # 由 curator 后续修正
                'weight': min(5 + int(r.score or 0), 8),
                'discovered_by': 'exa_search',
                'exa_score': r.score,
                'count': 1,
            }
            logger.info(
                f"Exa: found {domain} (score={r.score:.2f}, "
                f"title={r.title[:50]})"
            )

    # 只保留出现 ≥1 次的域名（可调整阈值）
    for domain, info in found_domains.items():
        if info['count'] >= 1:
            del info['exa_score']
            del info['count']
            new_sources.append(info)

    logger.info(f"Exa discovery: {len(new_sources)} new sources from {len(keywords)} keywords")
    return new_sources
