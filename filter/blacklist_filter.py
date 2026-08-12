"""Blacklist filter — zero-token pre-filter using keyword matching.

Drops articles matching noise keywords before they reach any LLM stage.
"""
import re
import logging

logger = logging.getLogger(__name__)


def _default_blacklist():
    return [
        "融资", "融资成功", "上市", "上市成功", "IPO",
        "股票", "涨停", "跌停", "股价",
        "小白教程", "零基础", "入门教程", "手把手教你",
        "套壳", "炒作",
        "SEO", "NFT", "crypto", "web3",
    ]


class BlacklistFilter:
    def __init__(self, config: dict):
        keywords = (
            config.get("filter", {})
            .get("blacklist_keywords", _default_blacklist())
        )
        # Build single regex for speed
        pattern = "|".join(re.escape(kw) for kw in keywords)
        self.re = re.compile(pattern, re.IGNORECASE)

    def filter(self, articles: list[dict]) -> list[dict]:
        kept = []
        dropped = 0
        for a in articles:
            text = (a.get("title", "") + " " + a.get("summary", "")[:200]).lower()
            if self.re.search(text):
                dropped += 1
            else:
                kept.append(a)
        if dropped:
            logger.info(f"Blacklist: {len(articles)} -> {len(kept)} (dropped {dropped})")
        return kept
