"""Extractors — lightweight wrappers for data sources.

每个 connector 职责:
  1. 知道这个数据源应该怎么构造请求 (FetchRequest)
  2. 解析返回的内容 → 标准 dict 格式 (url, title, summary, published, author)

HTTP 基础设施 (retry, timeout, rate limit, ETag) 全部由 FetchManager 统一处理。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp
import arxiv
import feedparser
import trafilatura

logger = logging.getLogger(__name__)

# ── Type alias ──────────────────────────────────────────────────────

ExtractedArticle = dict[str, Any]


# ── RSS / Atom ──────────────────────────────────────────────────────

class RSSExtractor:
    """RSS/Atom feeds via feedparser — only articles from last 7 days."""
    name = "rss"

    async def extract(
        self,
        session: aiohttp.ClientSession,
        source: dict,
        fetch_manager: Any = None,
    ) -> list[ExtractedArticle]:
        endpoint = source.get("endpoint") or source.get("url", "")
        if not endpoint:
            return []

        from datetime import datetime, timezone, timedelta

        lookback_days = source.get("max_days", 7)
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        try:
            if fetch_manager:
                result = await fetch_manager.fetch_bytes(endpoint)
            else:
                result = await _legacy_fetch(session, endpoint)

            if not result.ok:
                logger.warning(
                    f"RSS [{source.get('name', '?')}]: HTTP {result.status} {result.error}"
                )
                return []

            feed = feedparser.parse(result.content)
            articles = []

            # Extract feed-level metadata
            feed_url = feed.feed.get("link", "") or feed.get("feed", {}).get("link", "")
            feed_title = feed.feed.get("title", "")

            for entry in feed.entries:
                pub_dt = None
                pub = entry.get("published", "") or entry.get("updated", "")
                if pub:
                    try:
                        from dateutil import parser as dateutil_parser
                        pub_dt = dateutil_parser.parse(pub)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pub_dt = None
                if pub_dt and pub_dt < cutoff:
                    continue

                # Collect entry links (alternatives)
                entry_links = []
                for alt in getattr(entry, "links", []) or []:
                    if isinstance(alt, dict) and alt.get("href"):
                        entry_links.append({
                            "href": alt["href"],
                            "rel": alt.get("rel", "alternate"),
                            "type": alt.get("type", ""),
                        })

                articles.append({
                    "url": entry.get("link", ""),
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "") or entry.get("description", ""),
                    "published": pub,
                    "author": entry.get("author", ""),
                    "raw_extra": {
                        "feed_url": feed_url,
                        "feed_title": feed_title,
                        "categories": [
                            str(t) for t in entry.get("tags", [])
                        ],
                        "entry_links": entry_links[:5],  # Limit
                        "source": entry.get("source", {}).get("value", "")
                        if hasattr(entry.get("source"), "value") else "",
                    },
                })

            logger.debug(
                f"RSS [{source.get('name', '?')}]: {len(articles)} articles "
                f"(last {lookback_days}d)"
            )
            return articles

        except Exception as e:
            logger.warning(f"RSS [{source.get('name', '?')}]: {e}")
            return []


# ── arXiv ───────────────────────────────────────────────────────────

class ArxivExtractor:
    """arXiv API via py-arxiv (uses its own HTTP, no aiohttp needed)."""
    name = "arxiv"

    async def extract(
        self,
        session: aiohttp.ClientSession,
        source: dict,
        fetch_manager: Any = None,
    ) -> list[ExtractedArticle]:
        endpoint = source.get("endpoint") or source.get("url", "")
        if not endpoint:
            return []

        try:
            loop = asyncio.get_event_loop()

            # Parse arXiv RSS URL to get category
            # e.g. https://rss.arxiv.org/rss/cs.SY → cs.SY
            #      https://arxiv.org/rss/cs.AI   → cs.AI
            from urllib.parse import urlparse as _urlparse
            parsed_path = _urlparse(endpoint).path.rstrip("/")
            category = parsed_path.split("/")[-1] if parsed_path else ""
            if not category:
                logger.warning(
                    f"arXiv [{source.get('name', '?')}] "
                    f"cannot extract category from {endpoint}"
                )
                return []

            max_results = source.get("max_results", 10)

            search = arxiv.Search(
                query=f"cat:{category}",
                max_results=max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            # arxiv 4.x: client.results(search) is a sync iterator with blocking HTTP
            client = arxiv.Client()
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None, lambda: list(client.results(search))
            )
            articles = []
            for paper in results:
                # Published time
                published = None
                if paper.updated:
                    published = str(paper.updated)
                elif paper.published:
                    published = str(paper.published)

                articles.append({
                    "url": paper.entry_id,
                    "title": paper.title,
                    "summary": paper.summary[:3000],  # Limit abstract length
                    "published": published,
                    "author": ", ".join(
                        str(a) for a in paper.authors[:5]
                    ),  # Limit authors
                    "raw_extra": {
                        "arxiv_id": paper.entry_id.split("/")[-1]
                        if "/" in paper.entry_id else paper.entry_id,
                        "categories": [str(c) for c in paper.categories],
                        "pdf_url": paper.pdf_url,
                        "html_url": paper.entry_id.replace(
                            "abs", "html"
                        ) if "abs" in paper.entry_id else paper.entry_id,
                        "update_date": str(paper.updated)
                        if paper.updated else None,
                        "comment": paper.comment if paper.comment else None,
                        "journal_ref": paper.journal_ref
                        if paper.journal_ref else None,
                        "doi": paper.doi if paper.doi else None,
                    },
                })

            logger.info(
                f"arXiv [{source.get('name', '?')}]: {len(articles)} papers "
                f"(cat:{category}, max:{max_results})"
            )
            return articles

        except Exception as e:
            logger.warning(f"arXiv [{source.get('name', '?')}]: {e}")
            return []


# ── GitHub ──────────────────────────────────────────────────────────

class GitHubExtractor:
    """GitHub releases, issues, commits, and trending."""
    name = "github"

    async def extract(
        self,
        session: aiohttp.ClientSession,
        source: dict,
        fetch_manager: Any = None,
    ) -> list[ExtractedArticle]:
        try:
            subtype = source.get("github_subtype", "github_repo")

            if subtype == "github_trending":
                return await self._fetch_trending(session, source, fetch_manager)
            else:
                owner = source.get("github_owner", "")
                repo = source.get("github_repo", "")
                if not owner or not repo:
                    return []
                return await self._fetch_repo(session, owner, repo, source, fetch_manager)

        except Exception as e:
            logger.warning(f"GitHub [{source.get('name', '?')}]: {e}")
            return []

    async def _fetch_repo(
        self, session, owner, repo, source, fetch_manager,
    ):
        """Fetch repo releases using PyGithub (manages its own HTTP)."""
        from github import Github, RateLimitExceededException

        # Disable PyGithub retry to avoid blocking for minutes on 403
        g = Github(retry=None)
        try:
            r = g.get_repo(f"{owner}/{repo}")
        except RateLimitExceededException:
            logger.warning(
                f"[GitHub] {owner}/{repo}: rate limit exceeded — skipping"
            )
            return []
        articles = []

        from datetime import datetime, timezone, timedelta
        since_date = datetime.now(timezone.utc) - timedelta(days=7)

        try:
            for release in r.get_releases():
                if release.created_at < since_date:
                    continue

                # Get download links for assets
                assets = []
                if release.assets:
                    for asset in release.assets[:10]:  # Limit
                        assets.append({
                            "name": asset.name,
                            "url": asset.browser_download_url,
                            "size": asset.size,
                            "downloads": asset.download_count,
                        })

                articles.append({
                    "url": release.html_url,
                    "title": f"Release: {release.title or release.tag_name}",
                    "summary": (release.body or "")[:3000],  # Limit body
                    "published": str(release.created_at),
                    "author": release.author.login if release.author else "",
                    "raw_extra": {
                        "github_owner": owner,
                        "github_repo": repo,
                        "tag_name": release.tag_name,
                        "prerelease": release.prerelease,
                        "draft": release.draft,
                        "name": release.title or "",
                        "published_at": str(release.published_at)
                        if release.published_at else None,
                        "zipball_url": release.zipball_url,
                        "tarball_url": release.tarball_url,
                        "assets": assets,
                    },
                })
        except Exception as e:
            logger.warning(f"[GitHub] {owner}/{repo} releases: {e}")

        return articles

    async def _fetch_trending(
        self, session, source, fetch_manager,
    ):
        """Fetch GitHub trending page via FetchManager (BeautifulSoup)."""
        try:
            url = "https://github.com/trending"
            if fetch_manager:
                result = await fetch_manager.fetch_text(url)
            else:
                result = await _legacy_text_fetch(session, url)

            if not result.ok:
                return []

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(result.text, "html.parser")
            repos = soup.select("article.Box-row h2")
            articles = []
            for h2 in repos[:10]:
                link = h2.find("a")
                if not link:
                    continue

                repo_url = f"https://github.com{link['href']}"
                title = link.get_text(strip=True)

                # Extract description (p tag under h2)
                summary = ""
                desc = h2.find_next_sibling("p")
                if desc:
                    summary = desc.get_text(strip=True)[:1000]

                # Extract language
                language = ""
                stars_gained = ""
                forks = ""
                parent = h2.parent
                if parent:
                    lang_elem = parent.find(
                        "span", class_="d-inline-block",
                        attrs={"itemprop": "programmingLanguage"}
                    )
                    if lang_elem:
                        language = lang_elem.get_text(strip=True)

                    # Extract stars/stars-gained
                    star_elem = parent.find("svg", aria_label="Stars")
                    if star_elem:
                        star_link = star_elem.find_parent("a")
                        if star_link:
                            stars_gained = star_link.get_text(strip=True)

                    # Extract forks
                    fork_elem = parent.find("svg", aria_label="Forks")
                    if fork_elem:
                        fork_link = fork_elem.find_parent("a")
                        if fork_link:
                            forks = fork_link.get_text(strip=True)

                # Extract today/this_week/this_month tab
                period = "today"
                active_tab = soup.select_one(".Link--primary")
                if active_tab:
                    tab_text = active_tab.get_text(strip=True).lower()
                    if "week" in tab_text:
                        period = "this_week"
                    elif "month" in tab_text:
                        period = "this_month"

                # Parse owner/repo from URL
                repo_parts = repo_url.rstrip("/").split("/")[-2:]

                articles.append({
                    "url": repo_url,
                    "title": title,
                    "summary": summary,
                    "published": "",
                    "author": "",
                    "raw_extra": {
                        "language": language,
                        "stars_gained": stars_gained,
                        "forks": forks,
                        "trending_period": period,
                        "repo_owner": repo_parts[0] if len(repo_parts) >= 2 else "",
                        "repo_name": repo_parts[1] if len(repo_parts) >= 2 else "",
                    },
                })

            logger.info(
                f"GitHub Trending: {len(articles)} repos "
                f"(period={period if 'period' in dir() else 'today'})"
            )
            return articles

        except Exception as e:
            logger.warning(f"GitHub Trending: {e}")
            return []


# ── Web ─────────────────────────────────────────────────────────────

class WebExtractor:
    """Web pages: trafilatura (static) → Crawl4AI (JS-rendered fallback).

    Pipeline:
      1. FetchManager handles HTTP + retry + cache
      2. trafilatura: static extraction, fast
      3. If trafilatura < 100 chars, fallback to Crawl4AI
    """
    name = "web"
    _crawl4ai_ready = False
    _web_crawler = None

    @classmethod
    def _ensure_crawl4ai(cls):
        """Lazy import Crawl4AI — skip if not installed."""
        if cls._crawl4ai_ready:
            return cls._web_crawler
        try:
            from crawl4ai import AsyncWebCrawler
            cls._web_crawler = AsyncWebCrawler
            cls._crawl4ai_ready = True
            logger.info("Crawl4AI loaded — JS-rendered fallback enabled.")
        except ImportError:
            logger.warning(
                "Crawl4AI not installed — JS-rendered fallback disabled. "
                "Install: pip install crawl4ai && crawl4ai-setup"
            )
            return None

    async def _crawl4ai_fallback(
        self, url: str, user_agent: str
    ) -> list[ExtractedArticle]:
        """Fallback: use Crawl4AI for JS-rendered pages."""
        Crawler = self._ensure_crawl4ai()
        if Crawler is None:
            return []
        try:
            async with Crawler() as crawler:
                result = await crawler.arun(
                    url,
                    bypass_cache=True,
                    css_selector="article, main, .content, body",
                )
                if result.success and result.markdown:
                    title = result.metadata.get("title", "") or "Untitled"
                    return [{
                        "url": url,
                        "title": title,
                        "summary": result.markdown[:2000],
                        "published": "",
                        "author": result.metadata.get("author", ""),
                    }]
        except Exception as e:
            logger.debug(f"Crawl4AI fallback failed for {url[:80]}: {e}")
        return []

    async def extract(
        self,
        session: aiohttp.ClientSession,
        source: dict,
        fetch_manager: Any = None,
    ) -> list[ExtractedArticle]:
        endpoint = source.get("endpoint") or source.get("url", "")
        if not endpoint:
            return []
        user_agent = source.get("user_agent", "Mozilla/5.0 Weekly-AI-Report/1.0")

        # ── Step 1: Fetch via FetchManager (with retry/cache) ──
        try:
            if fetch_manager:
                result = await fetch_manager.fetch_text(
                    endpoint,
                    headers={"User-Agent": user_agent},
                    timeout=20,
                )
            else:
                result = await _legacy_text_fetch(
                    session, endpoint, headers={"User-Agent": user_agent}
                )

            if not result.ok:
                logger.debug(
                    f"Web [{source.get('name', '?')}] fetch failed: {result.error}"
                )
                return []

            meta = trafilatura.extract_with_metadata(
                result.text,
                include_comments=False,
                include_tables=True,
            )
            if meta and meta.text and len(meta.text) > 100:
                return [{
                    "url": endpoint,
                    "title": meta.title or "Untitled",
                    "summary": meta.text[:2000],
                    "published": meta.date or "",
                    "author": meta.author or "",
                }]
        except Exception as e:
            logger.debug(
                f"Web [{source.get('name', '?')}] trafilatura failed: {e}"
            )

        # ── Step 2: Crawl4AI fallback (JS-rendered) ──
        logger.info(
            f"Web [{source.get('name', '?')}] "
            f"trafilatura got too little content, trying Crawl4AI..."
        )
        return await self._crawl4ai_fallback(endpoint, user_agent)


# ── Exa Search ──────────────────────────────────────────────────────

class ExaExtractor:
    """
    Exa Neural Search: query-based web search → specific article URLs
    → deep-fetch via FetchManager (trafilatura) / Crawl4AI fallback.
    """
    name = "exa_search"

    async def _extract_url(
        self,
        fetch_manager,
        url: str,
        user_agent: str,
    ) -> list[ExtractedArticle]:
        """Fetch a single URL via FetchManager + trafilatura."""
        try:
            result = await fetch_manager.fetch_text(
                url,
                headers={"User-Agent": user_agent},
                timeout=15,
            )
            if not result.ok:
                return []

            meta = trafilatura.extract_with_metadata(
                result.text,
                include_comments=False,
                include_tables=True,
            )
            if meta and meta.text and len(meta.text) > 100:
                return [{
                    "url": url,
                    "title": meta.title or "Untitled",
                    "summary": meta.text[:2000],
                    "published": meta.date or "",
                    "author": meta.author or "",
                }]
        except Exception:
            pass
        return []

    async def extract(
        self,
        session: aiohttp.ClientSession,
        source: dict,
        existing_urls: set | None = None,
        fetch_manager: Any = None,
    ) -> list[ExtractedArticle]:
        query = source.get("query")
        if not query:
            logger.warning(
                f"Exa source [{source.get('name', '?')}] missing 'query' field"
            )
            return []

        api_key = source.get("exa_api_key") or os.environ.get("EXA_API_KEY")
        if not api_key:
            logger.warning(
                "EXA_API_KEY not set — skipping all Exa search sources. "
                "Get one at https://dashboard.exa.ai/"
            )
            return []

        try:
            from exa_py import AsyncExa
            client = AsyncExa(api_key=api_key)

            max_results = source.get("max_results", 10)
            logger.info(f"Exa searching: '{query}' (max {max_results})")

            result = await client.search(
                query,
                type="neural",
                num_results=max_results,
                start_published_date="2025-01-01",
            )

            if not result.results:
                return []

            # ── Pre-dedup: filter already-seen URLs ──
            new_urls = [
                r for r in result.results
                if existing_urls is None or r.url not in existing_urls
            ]
            skipped = len(result.results) - len(new_urls)
            if skipped:
                logger.info(
                    f"Exa '{query}': skipped {skipped} URL(s) "
                    f"already in stable sources"
                )

            if not new_urls:
                return []

            user_agent = source.get(
                "user_agent", "Weekly-AI-Report-Agent/1.0"
            )
            web_extractor = WebExtractor()

            # ── Step 1: Batch trafilatura via FetchManager ──
            trafilatura_ok: list[ExtractedArticle] = []
            trafilatura_fail: list[tuple[Any, str]] = []

            for r in new_urls:
                content = await self._extract_url(
                    fetch_manager, r.url, user_agent
                )
                if content:
                    for article in content:
                        article["raw_extra"] = {
                            "exa_score": getattr(r, "score", None),
                            "exa_author": getattr(r, "author", None),
                            "exa_published_date": getattr(
                                r, "published_date", None
                            ),
                            "exa_method": "trafilatura",
                        }
                    trafilatura_ok.extend(content)
                else:
                    trafilatura_fail.append((r, r.url))

            # ── Step 2: Crawl4AI fallback for failed URLs ──
            crawl4ai_ok: list[ExtractedArticle] = []
            if trafilatura_fail:
                logger.info(
                    f"Exa '{query}': "
                    f"{len(trafilatura_fail)} URLs need Crawl4AI fallback"
                )
                for r, url in trafilatura_fail:
                    try:
                        content = await asyncio.wait_for(
                            web_extractor._crawl4ai_fallback(
                                url, user_agent
                            ),
                            timeout=60,
                        )
                        for article in content:
                            article["raw_extra"] = {
                                "exa_score": getattr(r, "score", None),
                                "exa_author": getattr(r, "author", None),
                                "exa_published_date": getattr(
                                    r, "published_date", None
                                ),
                                "exa_method": "crawl4ai",
                            }
                        crawl4ai_ok.extend(content)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Exa '{query}': Crawl4AI timeout for "
                            f"{url[:80]}"
                        )
                    except Exception as e:
                        logger.debug(
                            f"Exa '{query}': Crawl4AI failed for "
                            f"{url[:80]}: {e}"
                        )

            articles = trafilatura_ok + crawl4ai_ok
            logger.info(
                f"Exa '{query}': {len(result.results)} URLs found, "
                f"{len(articles)} fetched "
                f"({len(trafilatura_ok)} trafilatura, "
                f"{len(crawl4ai_ok)} crawl4ai)"
            )
            return articles

        except Exception as e:
            logger.warning(f"Exa Search '{query}': {e}")
            return []


# ── Legacy helpers (for extractors still using raw aiohttp) ────────

async def _legacy_fetch(session: aiohttp.ClientSession, url: str) -> Any:
    """Legacy: fetch bytes directly. Returns a FetchResult-like object."""
    from aiohttp import ClientTimeout

    result_cls = None
    try:
        from fetcher.fetch_manager import FetchResult
        result_cls = FetchResult
    except ImportError:
        pass

    async with session.get(
        url, timeout=ClientTimeout(total=30)
    ) as resp:
        data = await resp.read()
        if result_cls:
            return result_cls(
                url=url, status=resp.status,
                content=data, text=data.decode("utf-8", errors="replace"),
            )
        return type(
            "_FetchResult", (),
            {
                "url": url, "status": resp.status,
                "content": data,
                "text": data.decode("utf-8", errors="replace"),
                "ok": 200 <= resp.status < 300,
            },
        )()


async def _legacy_text_fetch(
    session: aiohttp.ClientSession, url: str, headers: dict | None = None
) -> Any:
    """Legacy: fetch text directly."""
    from aiohttp import ClientTimeout

    result_cls = None
    try:
        from fetcher.fetch_manager import FetchResult
        result_cls = FetchResult
    except ImportError:
        pass

    async with session.get(
        url, timeout=ClientTimeout(total=20),
        headers=headers or {},
    ) as resp:
        text = await resp.text()
        if result_cls:
            return result_cls(
                url=url, status=resp.status,
                content=text.encode(), text=text,
            )
        return type(
            "_FetchResult", (),
            {
                "url": url, "status": resp.status,
                "content": text.encode(), "text": text,
                "ok": 200 <= resp.status < 300,
            },
        )()


# ── Registry ────────────────────────────────────────────────────────

EXTRACTOR_REGISTRY = {
    "rss": RSSExtractor(),
    "web": WebExtractor(),
    "arxiv": ArxivExtractor(),
    "github": GitHubExtractor(),
    "exa_search": ExaExtractor(),
}


def get_extractor(source_type: str):
    """Get an extractor by connector type. Falls back to RSS."""
    return EXTRACTOR_REGISTRY.get(source_type, RSSExtractor())
