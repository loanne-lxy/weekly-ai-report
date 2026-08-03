"""统一抓取模块 — RSS / 网页 / Nitter RSS"""
import asyncio
import logging
import aiohttp
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

USER_AGENT = "Weekly-AI-Report-Agent/1.0"


async def fetch_rss(
    session: aiohttp.ClientSession, source: dict
) -> list[dict]:
    """抓取 RSS 订阅源"""
    articles = []
    try:
        async with session.get(
            source["url"], timeout=30, headers={"User-Agent": USER_AGENT}
        ) as resp:
            text = await resp.text()
        feed = feedparser.parse(text)
        for entry in feed.entries[:20]:
            articles.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": entry.get("summary", entry.get("description", "")),
                    "published": _parse_date(entry),
                    "source_name": source["name"],
                    "source_category": source["category"],
                }
            )
    except Exception as e:
        logger.warning(f"RSS failed [{source['name']}]: {e}")
    return articles


async def fetch_web(
    session: aiohttp.ClientSession, source: dict
) -> list[dict]:
    """抓取普通网页（提取标题和段落文本）"""
    articles = []
    try:
        async with session.get(
            source["url"], timeout=30, headers={"User-Agent": USER_AGENT}
        ) as resp:
            html = await resp.text()
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.string if soup.title else source["name"]
        text = " ".join(p.get_text() for p in soup.find_all("p")[:30])
        if len(text) > 100:
            articles.append(
                {
                    "title": title.strip(),
                    "url": source["url"],
                    "summary": text[:2000],
                    "published": datetime.now(timezone.utc).isoformat(),
                    "source_name": source["name"],
                    "source_category": source["category"],
                }
            )
    except Exception as e:
        logger.warning(f"Web failed [{source['name']}]: {e}")
    return articles


async def fetch_nitter_rss(
    session: aiohttp.ClientSession, source: dict
) -> list[dict]:
    """抓取 Nitter RSS（Twitter 替代源）"""
    return await fetch_rss(session, source)


async def fetch_all(sources: list[dict], concurrency: int = 10) -> list[dict]:
    """并发抓取所有信息源"""
    FETCH_MAP = {
        "rss": fetch_rss,
        "web": fetch_web,
        "nitter_rss": fetch_nitter_rss,
        "rsshub": fetch_rss,
    }

    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(source: dict) -> list[dict]:
            async with sem:
                fetcher = FETCH_MAP.get(source.get("type", "rss"), fetch_rss)
                return await fetcher(session, source)

        tasks = [
            fetch_one(s) for s in sources if s.get("active", True)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
    return all_articles


def _parse_date(entry) -> str:
    for attr in ["published_parsed", "updated_parsed"]:
        tp = getattr(entry, attr, None)
        if tp:
            return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()
