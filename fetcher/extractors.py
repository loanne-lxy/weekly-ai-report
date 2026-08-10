"""Lightweight extractors — one class per source type, each ~30 lines

Each extractor only implements extract(session, source) → list[RawItem].
Retry, dedup, cache, normalize are handled by pipeline.py.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import feedparser
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
USER_AGENT = "Weekly-AI-Report-Agent/1.0"


def _parse_date(entry) -> str:
    for attr in ["published_parsed", "updated_parsed"]:
        tp = getattr(entry, attr, None)
        if tp:
            return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


# ── Raw Item ─────────────────────────────────────────────────────

class RawItem(dict):
    """{title, url, summary, published, source_name, source_category, source_type}"""
    pass


# ── RSS ───────────────────────────────────────────────────────────

class RSSExtractor:
    name = "rss"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[RawItem]:
        items = []
        try:
            async with session.get(
                source["url"], timeout=30,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"}
            ) as resp:
                text = await resp.text()
            feed = feedparser.parse(text)
            for entry in feed.entries[:20]:
                items.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": entry.get("summary", entry.get("description", "")),
                    "published": _parse_date(entry),
                })
        except Exception as e:
            logger.warning(f"RSS [{source.get('name','?')}]: {e}")
        return items


# ── Web (trafilatura) ─────────────────────────────────────────────

class WebExtractor:
    name = "web"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[RawItem]:
        try:
            import trafilatura

            async with session.get(
                source["url"], timeout=30,
                headers={"User-Agent": USER_AGENT}
            ) as resp:
                html = await resp.text()

            # trafilatura 2.x: extract_with_metadata returns Document(title, raw_text, date, ...)
            doc = trafilatura.extract_with_metadata(
                html, include_comments=False, include_tables=True,
                favor_precision=True,
            )
            if doc is None or not doc.raw_text or len(doc.raw_text) < 100:
                return []

            title = doc.title or source.get("name", "")
            published = ""
            if doc.date:
                try:
                    from dateutil.parser import parse as date_parse
                    published = date_parse(doc.date, fuzzy=True).astimezone(timezone.utc).isoformat()
                except Exception:
                    published = datetime.now(timezone.utc).isoformat()
            else:
                published = datetime.now(timezone.utc).isoformat()

            return [{
                "title": title,
                "url": source["url"],
                "summary": doc.raw_text[:2000],
                "published": published,
            }]
        except ImportError:
            logger.warning("Web [trafilatura]: not installed, pip install trafilatura")
            return []
        except Exception as e:
            logger.warning(f"Web [{source.get('name','?')}]: {e}")
        return []


# ── arXiv ─────────────────────────────────────────────────────────

class ArxivExtractor:
    name = "arxiv"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[RawItem]:
        topic = source.get("arxiv_topic", "cs.AI")
        max_results = source.get("max_results", 20)
        api_url = (
            f"http://export.arxiv.org/api/query?"
            f"search_query=cat:{topic}&start=0&max_results={max_results}"
            f"&sortBy=submittedDate&sortOrder=descending"
        )
        items = []
        try:
            async with session.get(api_url, timeout=30) as resp:
                text = await resp.text()
            feed = feedparser.parse(text)
            for entry in feed.entries[:max_results]:
                items.append({
                    "title": entry.get("title", "").replace("\n", " ").strip(),
                    "url": entry.get("id", ""),
                    "summary": entry.get("summary", "").replace("\n", " ")[:2000],
                    "published": _parse_date(entry),
                })
        except Exception as e:
            logger.warning(f"ArXiv [{source.get('name','?')}]: {e}")
        return items


# ── GitHub ────────────────────────────────────────────────────────

class GitHubExtractor:
    name = "github"
    GITHUB_API = "https://api.github.com"
    GITHUB_TOKEN: Optional[str] = None

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[RawItem]:
        subtype = source.get("github_subtype", "github_trending")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        token = source.get("github_token") or self.GITHUB_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if subtype == "github_repo":
            return await self._repo(session, source, headers)
        elif subtype == "github_org":
            return await self._org(session, source, headers)
        elif subtype == "github_user":
            return await self._user(session, source, headers)
        elif subtype == "github_trending":
            return await self._trending(session, headers)
        return []

    async def _atom(self, session, url: str, headers: dict) -> list[RawItem]:
        try:
            h = {**headers, "Accept": "application/atom+xml"}
            async with session.get(url, timeout=30, headers=h) as resp:
                if resp.status != 200:
                    return []
                feed = feedparser.parse(await resp.text())
            return [{"title": e.get("title", ""), "url": e.get("link", ""),
                     "summary": e.get("summary", ""), "published": _parse_date(e)}
                    for e in feed.entries[:20]]
        except Exception:
            return []

    async def _api(self, session, url: str, headers: dict) -> Optional[list | dict]:
        try:
            async with session.get(url, timeout=30, headers=headers) as resp:
                return await resp.json() if resp.status == 200 else None
        except Exception:
            return None

    async def _repo(self, session, source, headers):
        owner, repo = source.get("github_owner", ""), source.get("github_repo", "")
        if not owner or not repo:
            return []
        branch = source.get("github_branch", "main")
        rel, com = await asyncio.gather(
            self._atom(session, f"https://github.com/{owner}/{repo}/releases.atom", headers),
            self._atom(session, f"https://github.com/{owner}/{repo}/commits/{branch}.atom", headers)
        )
        return rel + com

    async def _org(self, session, source, headers):
        org = source.get("github_org", "")
        if not org:
            return []
        repos = await self._api(session, f"{self.GITHUB_API}/orgs/{org}/repos?sort=updated&per_page=10", headers)
        if not repos or not isinstance(repos, list):
            return []
        tasks = [self._atom(session, f"https://github.com/{r.get('owner',{}).get('login',org)}/{r['name']}/commits/{r.get('default_branch','main')}.atom", headers) for r in repos[:10] if isinstance(r, dict) and r.get('name')]
        results = await asyncio.gather(*tasks)
        return [a for r in results for a in r]

    async def _user(self, session, source, headers):
        user = source.get("github_user", "")
        if not user:
            return []
        repos = await self._api(session, f"{self.GITHUB_API}/users/{user}/repos?sort=updated&per_page=10", headers)
        if not repos or not isinstance(repos, list):
            return []
        return [{"title": r.get("full_name", r.get("name", "")),
                 "url": r.get("html_url", ""),
                 "summary": r.get("description") or "",
                 "published": r.get("updated_at") or r.get("pushed_at") or ""}
                for r in repos if isinstance(r, dict)]

    async def _trending(self, session, headers):
        data = await self._api(session,
            f"{self.GITHUB_API}/search/repositories?q=stars:>100+pushed:>2026-01-01&sort=stars&order=desc&per_page=20",
            headers)
        if not data or not isinstance(data, dict):
            return []
        items = []
        for r in data.get("items", []):
            if not isinstance(r, dict):
                continue
            s = f"Star {r.get('stargazers_count',0)}"
            if r.get("language"):
                s += f" | {r['language']}"
            if r.get("description"):
                s += f" — {r['description']}"
            items.append({"title": r.get("full_name", ""), "url": r.get("html_url", ""),
                          "summary": s[:2000], "published": r.get("updated_at", "")})
        return items


# ── Twitter ───────────────────────────────────────────────────────

class TwitterExtractor:
    name = "twitter"
    INSTANCES = ["https://rsshub.app", "https://nitter.tiekoetter.com"]

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[RawItem]:
        username = source.get("twitter_user", source.get("url", "").rstrip("/").split("/")[-1])
        if not username:
            return []
        for instance in self.INSTANCES:
            try:
                url = f"{instance}/twitter/user/{username}"
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
                        feed = feedparser.parse(await resp.text())
                        if feed.entries:
                            return [{"title": self._clean_title(e.get("title", "")),
                                     "url": e.get("link", ""),
                                     "summary": e.get("summary", "")[:2000],
                                     "published": _parse_date(e)}
                                    for e in feed.entries[:20]]
            except Exception:
                continue
        return []

    @staticmethod
    def _clean_title(title: str) -> str:
        if title.startswith("@"):
            parts = title.split(":", 1)
            return parts[1].strip() if len(parts) > 1 else title
        return title


# ── Registry ──────────────────────────────────────────────────────

EXTRACTOR_REGISTRY = {
    "rss": RSSExtractor(),
    "web": WebExtractor(),
    "arxiv": ArxivExtractor(),
    "github": GitHubExtractor(),
    "twitter": TwitterExtractor(),
    "nitter_rss": TwitterExtractor(),  # legacy
    "rsshub": RSSExtractor(),          # legacy
}


def get_extractor(source_type: str):
    return EXTRACTOR_REGISTRY.get(source_type, RSSExtractor())
