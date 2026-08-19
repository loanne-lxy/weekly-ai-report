"""Blacklist filter — zero-token pre-filter using keyword matching.

Drops articles matching noise keywords before they reach any LLM stage.
"""
import re
import logging

logger = logging.getLogger(__name__)


def _default_blacklist():
    """Fallback — should be configured in config.yaml under filter.blacklist_keywords."""
    return []


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
