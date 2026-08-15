"""Extractors — lightweight wrappers for data sources.
Each extractor: ~15 lines. Uses arxiv, PyGithub, feedparser, trafilatura.
No raw HTTP for sources that have dedicated libraries.
"""
import asyncio
import os
import logging
from typing import Optional
import aiohttp
from aiohttp import ClientTimeout
import feedparser
import arxiv  # py-arxiv wrapper
import trafilatura
from github import Github

logger = logging.getLogger(__name__)

# ── RSS ──────────────────────────────────────────────────────────

class RSSExtractor:
    """RSS/Atom feeds via feedparser — only articles from last 7 days."""
    name = "rss"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        url = source.get("url", "")
        if not url:
            return []
        from datetime import datetime, timezone, timedelta
        # 默认只抓近 7 天内容
        lookback_days = source.get("max_days", 7)
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        try:
            async with session.get(url, timeout=ClientTimeout(total=30)) as resp:
                data = await resp.read()
            feed = feedparser.parse(data)
            articles = []
            for entry in feed.entries:
                # 日期过滤
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
                    continue  # 跳过 7 天前的文章
                articles.append({
                    "url": entry.get("link", ""),
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "") or entry.get("description", ""),
                    "published": pub,
                    "author": entry.get("author", ""),
                })
            logger.debug(f"RSS [{source.get('name', '?')}]: {len(articles)} articles (last {lookback_days}d)")
            return articles
        except Exception as e:
            logger.warning(f"RSS [{source.get('name', '?')}]: {e}")
            return []


# ── arXiv ────────────────────────────────────────────────────────

class ArxivExtractor:
    """arXiv API via py-arxiv."""
    name = "arxiv"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        url = source.get("url", "")
        if not url:
            return []
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            # Parse arXiv RSS URL to get category
            # e.g. https://rss.arxiv.org/rss/cs.SY
            category = url.split("/")[-1]
            max_results = source.get("max_results", 10)
            search = arxiv.Search(
                query=f"cat:{category}",
                max_results=max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            results = list(loop.run_in_executor(None, search.results()))
            articles = []
            for paper in results:
                articles.append({
                    "url": paper.entry_id,
                    "title": paper.title,
                    "summary": paper.summary,
                    "published": str(paper.published),
                    "author": ", ".join(str(a) for a in paper.authors),
                })
            return articles
        except Exception as e:
            logger.warning(f"arXiv [{source.get('name', '?')}]: {e}")
            return []


# ── GitHub ───────────────────────────────────────────────────────

class GitHubExtractor:
    """GitHub releases, issues, commits, and trending."""
    name = "github"

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        try:
            subtype = source.get("github_subtype", "github_repo")

            if subtype == "github_trending":
                return await self._fetch_trending(session, source)
            else:
                owner = source.get("github_owner", "")
                repo = source.get("github_repo", "")
                if not owner or not repo:
                    return []
                return await self._fetch_repo(session, owner, repo, source)
        except Exception as e:
            logger.warning(f"GitHub [{source.get('name', '?')}]: {e}")
            return []

    async def _fetch_repo(self, session, owner, repo, source):
        g = Github()
        r = g.get_repo(f"{owner}/{repo}")
        articles = []

        # Releases only (commits removed — too many API calls for weekly report)
        try:
            from datetime import datetime, timezone, timedelta
            since_date = datetime.now(timezone.utc) - timedelta(days=7)
            for release in r.get_releases():
                if release.created_at < since_date:
                    continue
                articles.append({
                    "url": release.html_url,
                    "title": f"Release: {release.title or release.tag_name}",
                    "summary": release.body or "",
                    "published": str(release.created_at),
                    "author": release.author.login if release.author else "",
                })
        except Exception as e:
            logger.warning(f"[GitHub] {owner}/{repo} releases: {e}")

        return articles

    async def _fetch_trending(self, session, source):
        try:
            async with session.get(
                "https://github.com/trending",
                timeout=ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            repos = soup.select("article.Box-row h2")
            articles = []
            for h2 in repos[:10]:
                link = h2.find("a")
                if link:
                    articles.append({
                        "url": f"https://github.com{link['href']}",
                        "title": link.get_text(strip=True),
                        "summary": "",
                        "published": "",
                        "author": "",
                    })
            return articles
        except Exception as e:
            logger.warning(f"GitHub Trending: {e}")
            return []


# ── Web ──────────────────────────────────────────────────────────

class WebExtractor:
    """Web pages: trafilatura (static) → Crawl4AI (JS-rendered fallback).

    Pipeline:
      1. trafilatura: 纯静态提取，速度快，零浏览器开销。
      2. 如果 trafilatura 内容 < 100 字符，降级到 Crawl4AI（无头浏览器）。
      3. Crawl4AI 输出 Markdown，适合直接喂给 LLM。
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
            logger.warning("Crawl4AI not installed — JS-rendered fallback disabled. "
                           "Install: pip install crawl4ai && crawl4ai-setup")
            return None

    async def _crawl4ai_fallback(self, url: str, user_agent: str) -> list[dict]:
        """Fallback: use Crawl4AI for JS-rendered pages."""
        Crawler = self._ensure_crawl4ai()
        if Crawler is None:
            return []
        try:
            import asyncio
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

    async def extract(self, session: aiohttp.ClientSession, source: dict) -> list[dict]:
        url = source.get("url", "")
        if not url:
            return []
        user_agent = source.get("user_agent", "Mozilla/5.0 Weekly-AI-Report/1.0")

        # ── Step 1: trafilatura (static, fast) ──
        try:
            async with session.get(
                url,
                timeout=ClientTimeout(total=20),
                headers={"User-Agent": user_agent},
            ) as resp:
                html = await resp.text()

            meta = trafilatura.extract_with_metadata(
                html,
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
        except Exception as e:
            logger.debug(f"Web [{source.get('name', '?')}] trafilatura failed: {e}")

        # ── Step 2: Crawl4AI fallback (JS-rendered) ──
        logger.info(f"Web [{source.get('name', '?')}] trafilatura got too little content, trying Crawl4AI...")
        return await self._crawl4ai_fallback(url, user_agent)


# ── Exa Search ───────────────────────────────────────────────────

class ExaExtractor:
    """
    Exa Neural Search: query-based web search → specific article URLs
    → deep-fetch via WebExtractor (trafilatura/Crawl4AI).

    Unlike RSS (which gives you a feed), Exa searches the entire web
    for the most recent, relevant articles matching a keyword query.
    """
    name = "exa_search"

    async def _trafilatura_only(self, session: aiohttp.ClientSession, url: str,
                                 user_agent: str = "Weekly-AI-Report-Agent/1.0") -> list[dict]:
        """Pure trafilatura fetch — no Crawl4AI fallback. Fast."""
        try:
            async with session.get(
                url,
                timeout=ClientTimeout(total=15),
                headers={"User-Agent": user_agent},
            ) as resp:
                html = await resp.text()
            meta = trafilatura.extract_with_metadata(
                html,
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

    async def extract(self, session: aiohttp.ClientSession, source: dict,
                      existing_urls: set | None = None) -> list[dict]:
        query = source.get("query")
        if not query:
            logger.warning(f"Exa source [{source.get('name', '?')}] missing 'query' field")
            return []

        api_key = source.get("exa_api_key") or os.environ.get("EXA_API_KEY")
        if not api_key:
            logger.warning("EXA_API_KEY not set — skipping all Exa search sources. "
                           "Get one at https://dashboard.exa.ai/")
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

            # ── 预去重：过滤掉已经在稳定源中出现过的 URL ──
            new_urls = [r for r in result.results
                        if existing_urls is None or r.url not in existing_urls]
            skipped = len(result.results) - len(new_urls)
            if skipped:
                logger.info(f"Exa '{query}': skipped {skipped} URL(s) already in stable sources")

            if not new_urls:
                return []

            user_agent = source.get("user_agent", "Weekly-AI-Report-Agent/1.0")

            # ── Step 1: 批量 trafilatura 快速抓取 ──
            trafilatura_ok: list[dict] = []
            trafilatura_fail: list[tuple[dict, str]] = []  # (result_obj, url)

            for r in new_urls:
                content = await self._trafilatura_only(session, r.url, user_agent)
                if content:
                    for article in content:
                        article["raw_extra"] = {
                            "exa_score": getattr(r, "score", None),
                            "exa_author": getattr(r, "author", None),
                            "exa_published_date": getattr(r, "published_date", None),
                            "exa_method": "trafilatura",
                        }
                    trafilatura_ok.extend(content)
                else:
                    trafilatura_fail.append((r, r.url))

            # ── Step 2: trafilatura 失败的，用 Crawl4AI 渲染 ──
            crawl4ai_ok: list[dict] = []
            if trafilatura_fail:
                logger.info(
                    f"Exa '{query}': {len(trafilatura_fail)} URLs need Crawl4AI fallback"
                )
                web_extractor = WebExtractor()
                for r, url in trafilatura_fail:
                    temp_source = {
                        "name": f"Exa-{source.get('name', '?')}",
                        "url": url,
                        "type": "web",
                        "user_agent": user_agent,
                    }
                    try:
                        content = await asyncio.wait_for(
                            web_extractor.extract(session, temp_source),
                            timeout=60,
                        )
                        for article in content:
                            article["raw_extra"] = {
                                "exa_score": getattr(r, "score", None),
                                "exa_author": getattr(r, "author", None),
                                "exa_published_date": getattr(r, "published_date", None),
                                "exa_method": "crawl4ai",
                            }
                        crawl4ai_ok.extend(content)
                    except asyncio.TimeoutError:
                        logger.warning(f"Exa '{query}': Crawl4AI timeout for {url[:80]}")
                    except Exception as e:
                        logger.debug(f"Exa '{query}': Crawl4AI failed for {url[:80]}: {e}")

            articles = trafilatura_ok + crawl4ai_ok
            logger.info(
                f"Exa '{query}': {len(result.results)} URLs found, "
                f"{len(articles)} fetched "
                f"({len(trafilatura_ok)} trafilatura, {len(crawl4ai_ok)} crawl4ai)"
            )
            return articles

        except Exception as e:
            logger.warning(f"Exa Search '{query}': {e}")
            return []


# ── Registry ──────────────────────────────────────────────────────

EXTRACTOR_REGISTRY = {
    "rss": RSSExtractor(),
    "web": WebExtractor(),
    "arxiv": ArxivExtractor(),
    "github": GitHubExtractor(),
    "exa_search": ExaExtractor(),
}


def get_extractor(source_type: str):
    return EXTRACTOR_REGISTRY.get(source_type, RSSExtractor())
