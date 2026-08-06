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
    """Fetch GitHub activity via Atom feeds and REST API.

    Supported subtypes (source["github_subtype"]):
      - github_repo:  releases + commits for a single repo
      - github_org:   repos for an org, then latest commits per repo
      - github_user:  repos for a user
      - github_trending:  high-star repos pushed recently
    """

    name = "github"
    GITHUB_API = "https://api.github.com"

    async def fetch(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        subtype = source.get("github_subtype", "github_repo")
        token = self._get_token()
        headers = self._build_headers(token)

        if subtype == "github_repo":
            return await self._fetch_repo(session, source, headers)
        elif subtype == "github_org":
            return await self._fetch_org(session, source, headers)
        elif subtype == "github_user":
            return await self._fetch_user(session, source, headers)
        elif subtype == "github_trending":
            return await self._fetch_trending(session, source, headers)
        else:
            logger.warning(f"GitHub: unknown subtype '{subtype}' for [{source.get('name', '?')}]")
            return []

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_token() -> Optional[str]:
        import os
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        return token if token else None

    @staticmethod
    def _build_headers(token: Optional[str]) -> dict:
        h = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _fetch_atom(self, session, url: str, source: dict, headers: dict) -> list[dict]:
        """Fetch and parse an Atom feed, returning unified articles."""
        articles = []
        try:
            feed_headers = {**headers, "Accept": "application/atom+xml, application/xml, text/xml"}
            async with session.get(url, timeout=30, headers=feed_headers) as resp:
                if resp.status != 200:
                    logger.warning(f"GitHub Atom [{url}]: HTTP {resp.status}")
                    return []
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
            logger.warning(f"GitHub Atom [{url}]: {e}")
        return articles

    async def _fetch_json(self, session, url: str, headers: dict) -> Optional[list | dict]:
        """Fetch a GitHub REST API endpoint and return parsed JSON."""
        try:
            async with session.get(url, timeout=30, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"GitHub API [{url}]: HTTP {resp.status}")
                return None
        except Exception as e:
            logger.warning(f"GitHub API [{url}]: {e}")
            return None

    async def _fetch_commits_atom(self, session, owner: str, repo: str, branch: str,
                                  source: dict, headers: dict) -> list[dict]:
        url = f"https://github.com/{owner}/{repo}/commits/{branch}.atom"
        return await self._fetch_atom(session, url, source, headers)

    async def _fetch_releases_atom(self, session, owner: str, repo: str,
                                   source: dict, headers: dict) -> list[dict]:
        url = f"https://github.com/{owner}/{repo}/releases.atom"
        return await self._fetch_atom(session, url, source, headers)

    # ── subtype handlers ─────────────────────────────────────────

    async def _fetch_repo(self, session, source: dict, headers: dict) -> list[dict]:
        """github_repo: releases + commits for a single repo."""
        owner = source.get("github_owner", "")
        repo = source.get("github_repo", "")
        branch = source.get("github_branch", "main")
        if not owner or not repo:
            logger.warning(f"GitHub repo: missing github_owner or github_repo in [{source.get('name', '?')}]")
            return []

        releases, commits = await asyncio.gather(
            self._fetch_releases_atom(session, owner, repo, source, headers),
            self._fetch_commits_atom(session, owner, repo, branch, source, headers),
        )
        return releases + commits

    async def _fetch_org(self, session, source: dict, headers: dict) -> list[dict]:
        """github_org: list repos for an org, then fetch latest commits per repo."""
        org = source.get("github_org", "")
        if not org:
            logger.warning(f"GitHub org: missing github_org in [{source.get('name', '?')}]")
            return []

        repos_url = f"{self.GITHUB_API}/orgs/{org}/repos?sort=updated&per_page=10"
        repos = await self._fetch_json(session, repos_url, headers)
        if not repos or not isinstance(repos, list):
            return []

        # Fetch commits for each repo concurrently
        async def commits_for(repo_obj: dict) -> list[dict]:
            owner = repo_obj.get("owner", {}).get("login", org)
            name = repo_obj.get("name", "")
            branch = repo_obj.get("default_branch", "main")
            if not name:
                return []
            return await self._fetch_commits_atom(session, owner, name, branch, source, headers)

        tasks = [commits_for(r) for r in repos[:10]]
        results = await asyncio.gather(*tasks)
        all_articles = []
        for r in results:
            all_articles.extend(r)
        return all_articles

    async def _fetch_user(self, session, source: dict, headers: dict) -> list[dict]:
        """github_user: list repos for a user."""
        user = source.get("github_user", "")
        if not user:
            logger.warning(f"GitHub user: missing github_user in [{source.get('name', '?')}]")
            return []

        repos_url = f"{self.GITHUB_API}/users/{user}/repos?sort=updated&per_page=10"
        repos = await self._fetch_json(session, repos_url, headers)
        if not repos or not isinstance(repos, list):
            return []

        articles = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            articles.append(self.make_article(
                source,
                title=repo.get("full_name", repo.get("name", "")),
                url=repo.get("html_url", ""),
                summary=repo.get("description") or "",
                published=repo.get("updated_at") or repo.get("pushed_at") or "",
            ))
        return articles

    async def _fetch_trending(self, session, source: dict, headers: dict) -> list[dict]:
        """github_trending: high-star repos pushed since 2026-01-01, sorted by stars."""
        q = "stars:>100+pushed:>2026-01-01"
        search_url = f"{self.GITHUB_API}/search/repositories?q={q}&sort=stars&order=desc&per_page=20"
        data = await self._fetch_json(session, search_url, headers)
        if not data or not isinstance(data, dict):
            return []

        items = data.get("items", [])
        if not isinstance(items, list):
            return []

        articles = []
        for repo in items:
            if not isinstance(repo, dict):
                continue
            desc = repo.get("description") or ""
            stars = repo.get("stargazers_count", 0)
            lang = repo.get("language") or ""
            summary = f"⭐ {stars}"
            if lang:
                summary += f" | {lang}"
            if desc:
                summary += f" — {desc}"
            articles.append(self.make_article(
                source,
                title=repo.get("full_name", repo.get("name", "")),
                url=repo.get("html_url", ""),
                summary=summary[:2000],
                published=repo.get("updated_at") or repo.get("pushed_at") or "",
            ))
        return articles


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
