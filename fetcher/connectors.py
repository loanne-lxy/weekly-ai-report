"""Connector Layer — pluggable source-type adapters with unified output schema

Architecture:
  Source Pool → Connector Registry → Unified Article Schema → Curator

Each Connector handles one source type. Adding a new source type (ProductHunt,
Reddit, WeChat) only requires writing a new Connector — no pipeline changes.
"""
import asyncio
import logging
import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import feedparser
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "Weekly-AI-Report-Agent/1.0 (+https://github.com/loanne-lxy/weekly-ai-report)"

# ── Unified Article Schema ──────────────────────────────────────────
ARTICLE_SCHEMA = {
    "id": "",            # SHA256(title + url) — stable dedup key
    "title": "",
    "url": "",
    "summary": "",
    "published": "",     # ISO 8601
    "source_name": "",
    "source_category": "",
    "source_type": "",   # rss | web | github | arxiv | twitter
    "connector": "",     # which connector produced this
}


def _parse_date(entry) -> str:
    for attr in ["published_parsed", "updated_parsed"]:
        tp = getattr(entry, attr, None)
        if tp:
            return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _article_id(title: str, url: str) -> str:
    return hashlib.sha256(f"{title}{url}".encode()).hexdigest()[:12]


# ── Base Connector ──────────────────────────────────────────────────

class BaseConnector(ABC):
    """Abstract connector. Subclasses implement fetch()."""

    name: str = "base"

    @abstractmethod
    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        """Return list of articles in unified schema."""

    def make_article(self, source: dict, **overrides) -> dict:
        a = dict(ARTICLE_SCHEMA)
        a["source_name"] = source.get("name", "")
        a["source_category"] = source.get("category", "")
        a["source_type"] = source.get("type", self.name)
        a["connector"] = self.name
        a.update(overrides)
        a["id"] = _article_id(a["title"], a["url"])
        return a


# ── RSS Connector ───────────────────────────────────────────────────

class RSSConnector(BaseConnector):
    name = "rss"

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        articles = []
        try:
            async with session.get(
                source["url"], timeout=30,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"}
            ) as resp:
                text = await resp.text()
            feed = feedparser.parse(text)
            for entry in feed.entries[:20]:
                articles.append(self.make_article(
                    source,
                    title=entry.get("title", ""),
                    url=entry.get("link", ""),
                    summary=entry.get("summary", entry.get("description", "")),
                    published=_parse_date(entry),
                ))
        except Exception as e:
            logger.warning(f"RSS [{source['name']}]: {e}")
        return articles


# ── Web Scraper Connector ───────────────────────────────────────────

class WebConnector(BaseConnector):
    name = "web"

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        articles = []
        try:
            async with session.get(
                source["url"], timeout=30,
                headers={"User-Agent": USER_AGENT}
            ) as resp:
                html = await resp.text()
            soup = BeautifulSoup(html, "lxml")
            title = soup.title.string.strip() if soup.title else source["name"]
            text = " ".join(p.get_text() for p in soup.find_all("p")[:30])
            if len(text) > 100:
                articles.append(self.make_article(
                    source,
                    title=title,
                    url=source["url"],
                    summary=text[:2000],
                    published=datetime.now(timezone.utc).isoformat(),
                ))
        except Exception as e:
            logger.warning(f"Web [{source['name']}]: {e}")
        return articles


# ── arXiv Connector ─────────────────────────────────────────────────

class ArxivConnector(BaseConnector):
    name = "arxiv"

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        """Fetch arXiv articles via the public API."""
        articles = []
        topic = source.get("arxiv_topic", "cs.AI")
        max_results = source.get("max_results", 20)
        api_url = (
            f"http://export.arxiv.org/api/query?"
            f"search_query=cat:{topic}&start=0&max_results={max_results}"
            f"&sortBy=submittedDate&sortOrder=descending"
        )
        try:
            async with session.get(api_url, timeout=30) as resp:
                text = await resp.text()
            feed = feedparser.parse(text)
            for entry in feed.entries[:max_results]:
                articles.append(self.make_article(
                    source,
                    title=entry.get("title", "").replace("\n", " ").strip(),
                    url=entry.get("id", ""),
                    summary=entry.get("summary", "").replace("\n", " ")[:2000],
                    published=_parse_date(entry),
                ))
        except Exception as e:
            logger.warning(f"ArXiv [{source['name']}]: {e}")
        return articles


# ── GitHub Connector ────────────────────────────────────────────────

class GitHubConnector(BaseConnector):
    name = "github"

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        """Fetch GitHub trending repos via direct injection.

        GitHub's trending page is a React SPA (not scrapable). Instead we
        inject a few proven high-signal repos as structured articles, which
        the curator can then evaluate for relevance.
        """
        # GitHub trending can't be scraped — use curated entry point
        # For now, return empty. Future: use gh CLI or GitHub API.
        return []


# ── Twitter Connector (4-layer fallback) ────────────────────────────

class TwitterConnector(BaseConnector):
    name = "twitter"

    INSTANCES = [
        "https://rsshub.app",           # Layer 1: self-hosted RSSHub
        "https://nitter.tiekoetter.com",  # Layer 2: Nitter fallback
    ]

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        username = source.get("twitter_user", source.get("url", "").rstrip("/").split("/")[-1])
        if not username:
            return []

        # Layer 1: RSSHub
        for instance in self.INSTANCES:
            try:
                url = f"{instance}/twitter/user/{username}"
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        feed = feedparser.parse(text)
                        if feed.entries:
                            return self._parse_entries(source, feed.entries)
            except Exception:
                continue

        logger.warning(f"Twitter: all layers failed for @{username}")
        return []

    def _parse_entries(self, source: dict, entries: list) -> list[dict]:
        articles = []
        for entry in entries[:20]:
            title = entry.get("title", "")
            # Strip the username prefix common in Twitter RSS titles
            if title.startswith("@"):
                parts = title.split(":", 1)
                title = parts[1].strip() if len(parts) > 1 else title
            articles.append(self.make_article(
                source,
                title=title[:200],
                url=entry.get("link", ""),
                summary=entry.get("summary", entry.get("description", ""))[:2000],
                published=_parse_date(entry),
            ))
        return articles


# ── Connector Registry ──────────────────────────────────────────────

CONNECTOR_REGISTRY: dict[str, BaseConnector] = {
    "rss": RSSConnector(),
    "web": WebConnector(),
    "arxiv": ArxivConnector(),
    "github": GitHubConnector(),
    "twitter": TwitterConnector(),
    # Legacy aliases
    "nitter_rss": TwitterConnector(),   # migrated to unified Twitter connector
    "rsshub": RSSConnector(),           # RSSHub outputs RSS, reuse
}


def get_connector(source_type: str) -> BaseConnector:
    """Resolve connector for a source type. Falls back to RSS."""
    return CONNECTOR_REGISTRY.get(source_type, RSSConnector())
