"""从高分文章提取 outbound links，推荐为新源（零 Token 成本）"""
import re
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 排除平台类域名（个人博客/机构网站才是好源）
_BLOCKED = {
    'twitter.com', 'x.com', 'youtube.com', 'github.com',
    'wikipedia.org', 'arxiv.org', 'bilibili.com',
    'zhihu.com', 'weibo.com', 'qq.com', 'weixin.qq.com',
    'mp.weixin.qq.com',
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


def _dominant_category(referenced_by: list) -> str:
    """从引用文章列表推断主导领域"""
    cats = {}
    for a in referenced_by:
        c = a.get('category', 'LLM')
        cats[c] = cats.get(c, 0) + 1
    return max(cats, key=cats.get) if cats else 'LLM'


def mine_links(articles: list[dict], existing_domains: set[str]) -> list[dict]:
    """从 priority_score >= 8.0 的文章提取 outbound links

    Args:
        articles: 策展后的文章列表
        existing_domains: 已有源的域名集合，避免重复推荐

    Returns:
        新源候选列表，category 继承自引用文章的领域
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
            clean_url = f"https://{domain}"

            if domain not in candidates:
                candidates[domain] = {
                    'domain': domain,
                    'urls': {clean_url},
                    'ref_articles': [],  # 存文章对象用于推断 category
                }
            candidates[domain]['urls'].add(clean_url)
            candidates[domain]['ref_articles'].append(a)

    results = []
    for domain, info in candidates.items():
        category = _dominant_category(info['ref_articles'])
        results.append({
            'name': domain,
            'url': list(info['urls'])[0],
            'type': 'web',
            'category': category,
            'weight': min(5 + len(info['urls']), 8),
            'discovered_by': 'link_miner',
            'referenced_by': list(set(
                a.get('chinese_title', a.get('title', ''))[:40]
                for a in info['ref_articles']
            ))[:3],
        })
        logger.info(
            f"Link miner: {domain} (cat={category}, "
            f"refs={len(info['urls'])} high-score articles)"
        )

    return results
