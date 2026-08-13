"""从高分文章提取 outbound links，推荐为新源（零 Token 成本）"""
import re
import logging
from urllib.parse import urlparse
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

# 排除平台类域名（个人博客/机构网站才是好源）
_BLOCKED = {
    'twitter.com', 'x.com', 'youtube.com', 'github.com',
    'wikipedia.org', 'arxiv.org', 'bilibili.com',
    'zhihu.com', 'weibo.com', 'qq.com', 'weixin.qq.com',
    'weixin.qq.com', 'mp.weixin.qq.com',
    'medium.com', 'substack.com', 'dev.to',
    'linkedin.com', 'facebook.com', 'instagram.com',
    'reddit.com', 'hackernews.com', 'news.ycombinator.com',
    'amazon.com', 'google.com', 'microsoft.com',
    'npmjs.com', 'pypi.org', 'crates.io',
}


def _normalize_domain(url: str) -> str:
    """URL → 规范化根域名"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or parsed.netloc
        # 去掉 www/blog/news 前缀
        parts = hostname.split('.')
        if parts[0] in ('www', 'blog', 'news', 'dev', 'docs'):
            parts = parts[1:]
        return '.'.join(parts) if parts else hostname
    except Exception:
        return ''


def mine_links(articles: list[dict], existing_domains: set[str]) -> list[dict]:
    """从 priority_score >= 8.0 的文章提取 outbound links

    Args:
        articles: 策展后的文章列表
        existing_domains: 已有源的域名集合，避免重复推荐

    Returns:
        新源候选列表
    """
    candidates: dict[str, dict] = {}

    for a in articles:
        if a.get('priority_score', 0) < 8.0:
            continue

        # 从正文/摘要提取 URL
        text = (
            a.get('content_preview', '')
            + ' ' + a.get('ai_summary', '')
            + ' ' + a.get('summary', '')
            + ' ' + a.get('tldr', '')
        )

        urls = re.findall(r'https?://[^\s<>"\')\]]+', text)
        for url in urls:
            domain = _normalize_domain(url)
            if not domain or domain in _BLOCKED or domain in existing_domains:
                continue
            # 去掉尾部路径
            clean_url = f"https://{domain}"

            if domain not in candidates:
                candidates[domain] = {
                    'domain': domain,
                    'urls': {clean_url},
                    'referenced_by': [],
                }
            candidates[domain]['urls'].add(clean_url)
            candidates[domain]['referenced_by'].append(
                a.get('chinese_title', a.get('title', ''))[:40]
            )

    results = []
    for domain, info in candidates.items():
        results.append({
            'name': domain,
            'url': list(info['urls'])[0],
            'type': 'web',
            'category': 'LLM',  # 由 curator 后续修正
            'weight': min(5 + len(info['urls']), 8),
            'discovered_by': 'link_miner',
            'referenced_by': list(set(info['referenced_by']))[:3],
        })
        logger.info(
            f"Link miner: {domain} (referenced by {len(info['urls'])} "
            f"high-score articles)"
        )

    return results
