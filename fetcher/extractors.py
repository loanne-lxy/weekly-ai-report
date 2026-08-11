"""Extractors — thin wrappers around mature libraries.

Each extractor: ~15 lines. Uses arxiv, PyGithub, feedparser, trafilatura.
No raw HTTP for sources that have dedicated libraries.
"""
import logging
from typing import Optional
import aiohttp
from aiohttp import ClientTimeout
import feedparser
import arxiv  # py-arxiv wrapper
import trafilatura
from github import Github

logger = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=30)


def _parse_iso_date(entry) -> str:
    """feedparser entry → ISO 8601."""
    for attr in ["published_parsed", "updated_parsed"]:
        tp = getattr(entry, attr, None)
        if tp:
            from datetime import datetime, timezone
            return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(dt) -> str:
    """datetime → ISO 8601."""
    if isinstance(dt, str):
        return dt
    try:
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return ""


# ── RSS ──────────────────────────────────────────────────────────

class RSSExtractor:
    """RSS/Atom feeds via feedparser (mature, battle-tested)."""
    name = "rss"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        url = source["url"]
        try:
            async with session.get(url, timeout=_TIMEOUT,
                                   headers={"User-Agent": source.get("user_agent", "Weekly-AI-Report-Agent/1.0"),
                                            "Accept": "application/rss+xml"}) as resp:
                feed = feedparser.parse(await resp.text())
            return [{"title": e.get("title", ""),
                     "url": e.get("link", ""),
                     "summary": e.get("summary", e.get("description", "")),
                     "published": _parse_iso_date(e),
                     "author": e.get("author")}
                    for e in feed.entries[:source.get("max_results", 20)]]
        except Exception as e:
            logger.warning(f"RSS [{source.get('name', '?')}]: {e}")
            return []


# ── arXiv ────────────────────────────────────────────────────────

class ArxivExtractor:
    """arXiv via py-arxiv library (official wrapper)."""
    name = "arxiv"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        topic = source.get("arxiv_topic", source.get("url", "").split("/")[-1] if "arxiv" in source.get("url", "") else "cs.AI")
        max_results = source.get("max_results", 20)
        try:
            search = arxiv.Search(
                query=f"cat:{topic}",
                max_results=max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            items = []
            for p in arxiv.Client().results(search):
                items.append({
                    "title": p.title.replace("\n", " ").strip(),
                    "url": p.entry_id,
                    "summary": p.summary.replace("\n", " ")[:2000],
                    "published": _parse_datetime(p.published),
                    "author": ", ".join(str(a) for a in p.authors[:3]),
                })
            return items
        except Exception as e:
            logger.warning(f"arXiv [{source.get('name', '?')}]: {e}")
            return []


# ── GitHub ───────────────────────────────────────────────────────

class GitHubExtractor:
    """GitHub via PyGithub library (official wrapper)."""
    name = "github"
    _token: Optional[str] = None

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        subtype = source.get("github_subtype", "github_trending")
        token = source.get("github_token") or self._token
        g = Github(token) if token else Github()

        if subtype == "github_repo":
            return await self._repo(g, source)
        elif subtype == "github_org":
            return await self._org(g, source)
        elif subtype == "github_user":
            return await self._user(g, source)
        elif subtype == "github_trending":
            return await self._trending(g)
        return []

    async def _repo(self, g, source) -> list[dict]:
        owner, repo = source.get("github_owner", ""), source.get("github_repo", "")
        if not owner or not repo:
            return []
        gh_repo = g.get_repo(f"{owner}/{repo}")
        items = []
        try:
            for rel in gh_repo.get_releases(per_page=10):
                items.append({
                    "title": f"[Release] {rel.tag_name}: {rel.title}",
                    "url": rel.html_url,
                    "summary": (rel.body or "")[:2000],
                    "published": _parse_datetime(rel.created_at),
                    "author": owner,
                })
        except Exception:
            pass
        try:
            for commit in gh_repo.get_commits(per_page=10):
                if commit.commit.message:
                    items.append({
                        "title": commit.commit.message[:100],
                        "url": commit.html_url,
                        "summary": commit.commit.message,
                        "published": _parse_datetime(commit.commit.author.date) if commit.commit.author else "",
                        "author": (commit.commit.author.name if commit.commit.author else owner),
                    })
        except Exception:
            pass
        return items

    async def _org(self, g, source) -> list[dict]:
        org_name = source.get("github_org", "")
        if not org_name:
            return []
        org = g.get_organization(org_name)
        items = []
        for r in org.get_repos(sort="updated", direction="desc")[0:10]:
            for commit in r.get_commits(per_page=2):
                if commit.commit.message:
                    items.append({
                        "title": f"[{r.full_name}] {commit.commit.message[:80]}",
                        "url": commit.html_url,
                        "summary": commit.commit.message,
                        "published": _parse_datetime(commit.commit.author.date) if commit.commit.author else "",
                        "author": r.full_name,
                    })
        return items

    async def _user(self, g, source) -> list[dict]:
        user_name = source.get("github_user", "")
        if not user_name:
            return []
        user = g.get_user(user_name)
        items = []
        for r in user.get_repos(sort="updated")[0:10]:
            items.append({
                "title": r.full_name,
                "url": r.html_url,
                "summary": (r.description or "")[:2000],
                "published": _parse_datetime(r.pushed_at) if r.pushed_at else "",
                "author": user_name,
            })
        return items

    async def _trending(self, g) -> list[dict]:
        """GitHub Trending via search API."""
        from datetime import datetime, timezone
        since = datetime.now(timezone.utc).replace(year=datetime.now().year).strftime("%Y-%m-%d")
        items = []
        try:
            search = g.search_repositories(
                query=f"stars:>100 pushed:>{since}",
                sort="stars", order="desc", per_page=20
            )
            for r in search[:20]:
                s = f"Stars: {r.stargazers_count}"
                if r.language:
                    s += f" | {r.language}"
                if r.description:
                    s += f" — {r.description[:200]}"
                items.append({
                    "title": r.full_name,
                    "url": r.html_url,
                    "summary": s,
                    "published": _parse_datetime(r.pushed_at) if r.pushed_at else "",
                    "author": r.owner.login if r.owner else "",
                })
        except Exception as e:
            logger.warning(f"GitHub Trending: {e}")
        return items


# ── Web ──────────────────────────────────────────────────────────

class WebExtractor:
    """Web pages via trafilatura (best-in-class content extraction)."""
    name = "web"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        url = source["url"]
        try:
            async with session.get(url, timeout=_TIMEOUT,
                                   headers={"User-Agent": "Weekly-AI-Report-Agent/1.0"}) as resp:
                html = await resp.text()
            doc = trafilatura.extract_with_metadata(
                html, include_comments=False, include_tables=True, favor_precision=True,
            )
            if not doc or not doc.raw_text or len(doc.raw_text) < 100:
                return []
            published = ""
            if doc.date:
                try:
                    from dateutil.parser import parse as date_parse
                    from datetime import timezone
                    published = date_parse(doc.date, fuzzy=True).astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
            return [{
                "title": doc.title or source.get("name", ""),
                "url": url,
                "summary": doc.raw_text[:2000],
                "published": published,
            }]
        except Exception as e:
            logger.warning(f"Web [{source.get('name', '?')}]: {e}")
            return []


# ── Registry ──────────────────────────────────────────────────────

EXTRACTOR_REGISTRY = {
    "rss": RSSExtractor(),
    "web": WebExtractor(),
    "arxiv": ArxivExtractor(),
    "github": GitHubExtractor(),
}


def get_extractor(source_type: str):
    return EXTRACTOR_REGISTRY.get(source_type, RSSExtractor())
