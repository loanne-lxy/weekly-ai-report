"""Shared report clock, article hygiene and evidence quality gates."""
from __future__ import annotations
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse


def parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                dt = parsedate_to_datetime(str(value))
            except (ValueError, TypeError, OverflowError):
                return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def report_window(cutoff=None):
    end = parse_date(cutoff) or datetime.now(timezone.utc)
    return end - timedelta(days=7), end


def clean_content(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?s)^\s*---\s*\n.*?\n---\s*\n", "", text)
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    boilerplate = re.compile(r"^(accept (all )?cookies|cookie settings|subscribe to our newsletter|skip to (main )?content|all rights reserved|sign in|log in)$", re.I)
    return "\n".join(dict.fromkeys(line for line in lines if line and not boilerplate.match(line))).strip()


def publisher(article):
    return (urlparse(article.get("url", "")).hostname or "unknown").lower().removeprefix("www.")


def prepare_articles(articles, cutoff=None):
    start, end = report_window(cutoff)
    accepted, rejected = [], []
    for raw in articles:
        a = dict(raw)
        a["title"] = clean_content(a.get("title"))
        a["summary"] = clean_content(a.get("summary") or a.get("content_preview"))
        a["content_preview"] = a["summary"]
        pub = parse_date(a.get("published"))
        reasons = []
        if pub and not start <= pub <= end:
            reasons.append("outside_report_window")
        if len(a["summary"]) < 60 or a["summary"].strip().lower() == a["title"].strip().lower():
            reasons.append("insufficient_body")
        if not a.get("url") or not a["title"]:
            reasons.append("missing_identity")
        if pub:
            a["published"] = pub.isoformat()
        a["date_status"] = "verified" if pub else "unknown"
        a["publisher_domain"] = publisher(a)
        extra = a.get("raw_extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        a["raw_extra"] = {**extra, "date_status": a["date_status"], "publisher_domain": a["publisher_domain"]}
        if reasons:
            rejected.append({"url": a.get("url"), "title": a["title"], "reasons": reasons})
        else:
            accepted.append(a)
    return accepted, rejected


def score_number(value, default=0.0):
    try:
        import math
        number = float(value)
        return max(0.0, min(1.0, number)) if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


async def recover_short_bodies(articles):
    """Retry article extraction before quarantining empty RSS/site-shell content."""
    import asyncio
    import requests
    import trafilatura
    sem = asyncio.Semaphore(5)

    def extract(a):
        response = requests.get(a["url"], timeout=25, headers={"User-Agent": "Weekly-AI-Report-Agent/1.0"})
        response.raise_for_status()
        if "text/html" not in response.headers.get("Content-Type", "text/html"):
            return None
        # Bound extraction work; do not use navigation/site-shell text as evidence.
        return trafilatura.bare_extraction(response.text[:3000000], url=a["url"],
                   include_comments=False, include_tables=False, favor_precision=True, with_metadata=True)

    async def one(raw):
        a = dict(raw)
        text = clean_content(a.get("summary") or a.get("content_preview"))
        if len(text) >= 60 or not a.get("url") or (a.get("raw_extra") or {}).get("body_recovery"):
            return a
        async with sem:
            try:
                doc = await asyncio.to_thread(extract, a)
                body = clean_content(getattr(doc, "text", "") if doc else "")
                if len(body) >= 120:
                    a["summary"] = body[:12000]
                    a["content_preview"] = body[:12000]
                    if not parse_date(a.get("published")) and getattr(doc, "date", None):
                        a["published"] = doc.date
                    if len(text) < 25 and getattr(doc, "title", None):
                        a["title"] = doc.title
                    a["raw_extra"] = {**(a.get("raw_extra") or {}), "body_recovery": "trafilatura"}
                else:
                    a["raw_extra"] = {**(a.get("raw_extra") or {}), "body_recovery": "insufficient"}
            except Exception as exc:
                a["raw_extra"] = {**(a.get("raw_extra") or {}), "body_recovery": type(exc).__name__}
        return a
    return await asyncio.gather(*(one(a) for a in articles))
