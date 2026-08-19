"""
Source Resolver — URL → Source Type → Connector 映射。

职责：
  1. 接收任意 URL（raw string），识别它的类型
  2. 返回标准化的 Source 结构：canonical_url, source_type, connector
  3. URL Canonicalization（尾部 /、fragment、tracking params、大小写）

原则：
  - Resolver 不负责真正抓取内容
  - 只回答 "这个 URL 是什么类型？应该交给哪个 Connector？"
  - Exa 不做 Connector（它是搜索工具，不是 Source）

流程:
  raw URL
    → canonicalize
    → infer type (github/rss/arxiv/web)
    → resolve connector
    → return Standard Source
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Available connector registry
# ──────────────────────────────────────────────

AVAILABLE_CONNECTORS = {"rss", "web", "arxiv", "github", "exa_search"}


@dataclass(frozen=True)
class ResolvedSource:
    """Resolver 输出：标准化 Source 结构。"""
    canonical_url: str
    source_type: str        # github_repo | rss | atom | arxiv | web
    connector: str          # rss | web | arxiv | github | exa_search
    confidence: float = 1.0  # 0-1, 判断置信度
    hints: list[str] = field(default_factory=list)  # 识别依据


# ──────────────────────────────────────────────
# URL Canonicalization
# ──────────────────────────────────────────────

# Tracking parameters to strip
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_eid", "mc_cid", "ref", "utm_source",
    "yclid", "dclid", "irwid", "wbraid",
}


def canonicalize_url(url: str) -> str:
    """
    Normalize a URL for dedup / registry storage.

    Handles:
      - Trailing slashes on path
      - Fragment removal
      - Tracking parameter removal
      - Lowercase scheme + hostname
      - Default port removal (:80 / :443)
    """
    if not url:
        return ""

    url = url.strip()
    try:
        parsed = urlparse(url)

        # Lowercase scheme + hostname
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()

        # Remove default ports
        netloc = hostname
        if parsed.port:
            if not ((scheme == "http" and parsed.port == 80) or
                    (scheme == "https" and parsed.port == 443)):
                netloc = f"{hostname}:{parsed.port}"

        # Remove tracking parameters
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            cleaned = {k: v for k, v in qs.items() if k not in _TRACKING_PARAMS}
            query = urlencode([(k, v[0]) for k, v in cleaned.items()], doseq=True)
        else:
            query = ""

        # Strip trailing slashes from path (keep root /), lowercase, remove .git
        path = parsed.path.lower().rstrip("/") or "/"
        if path.endswith(".git"):
            path = path[:-4] or "/"

        # Remove fragment
        fragment = ""

        canonical = urlunparse((scheme, netloc, path, parsed.params, query, fragment))
        return canonical

    except Exception:
        return url.lower().strip()


# ──────────────────────────────────────────────
# Source type inference
# ──────────────────────────────────────────────

# GitHub patterns
_GITHUB_REPO_RE = re.compile(
    r"^https?://(www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
    re.IGNORECASE
)
_GITHUB_TRENDING_RE = re.compile(
    r"^https?://(www\.)?github\.com/trending",
    re.IGNORECASE
)

# arXiv patterns
_ARXIV_RSS_RE = re.compile(
    r"^https?://rss\.arxiv\.org/rss/([^/]+)$",
    re.IGNORECASE
)
_ARXIV_ABS_RE = re.compile(
    r"^https?://arxiv\.org/abs/(\d+\.\d+)$",
    re.IGNORECASE
)

# RSS/Atom patterns
_RSS_EXTENSIONS = {".rss", ".xml", ".atom", ".rdf", ".rss2"}
_RSS_PATH_HINTS = ["/feed/", "/rss/", "/atom/", "/feed.xml", "/rss.xml", "/atom.xml"]


def _infer_github(url: str) -> ResolvedSource | None:
    """Check if URL is a GitHub repo or trending."""
    m = _GITHUB_REPO_RE.match(url)
    if m:
        owner, repo = m.group(2).lower(), m.group(3).lower()
        canonical = f"https://github.com/{owner}/{repo}"
        return ResolvedSource(
            canonical_url=canonical,
            source_type="github_repo",
            connector="github",
            confidence=1.0,
            hints=["github_repo_pattern", f"owner={owner}", f"repo={repo}"],
        )

    if _GITHUB_TRENDING_RE.match(url):
        return ResolvedSource(
            canonical_url="https://github.com/trending",
            source_type="github_trending",
            connector="github",
            confidence=1.0,
            hints=["github_trending"],
        )

    return None


def _infer_arxiv(original_url: str) -> ResolvedSource | None:
    """Check if URL is an arXiv source. Uses original URL for category extraction."""
    canonical = canonicalize_url(original_url)
    parsed_orig = urlparse(original_url)

    if parsed_orig.hostname and parsed_orig.hostname.lower() == "rss.arxiv.org":
        # Extract category from ORIGINAL URL to preserve case
        orig_path = parsed_orig.path.rstrip("/")
        category = orig_path.split("/")[-1] if orig_path else ""
        return ResolvedSource(
            canonical_url=canonical,
            source_type="arxiv",
            connector="arxiv",
            confidence=1.0,
            hints=["arxiv_rss", f"category={category}"],
        )

    if parsed_orig.hostname and parsed_orig.hostname.lower() == "arxiv.org":
        m = _ARXIV_ABS_RE.match(original_url)
        if m:
            # Single paper — treat as web, not a Source
            return ResolvedSource(
                canonical_url=canonical,
                source_type="web",
                connector="web",
                confidence=0.5,
                hints=["arxiv_single_paper"],
            )

    return None


def _infer_rss_atom(original_url: str) -> ResolvedSource | None:
    """Check if URL looks like an RSS/Atom feed."""
    # Check both original and canonicalized paths
    original_path = urlparse(original_url).path.lower()
    canonical = canonicalize_url(original_url)
    canonical_path = urlparse(canonical).path.lower()

    # Extension check (both)
    for p in [original_path, canonical_path]:
        if any(p.endswith(ext) for ext in _RSS_EXTENSIONS):
            return ResolvedSource(
                canonical_url=canonical,
                source_type="atom" if ".atom" in p else "rss",
                connector="rss",
                confidence=0.9,
                hints=["rss_extension"],
            )

    # Path pattern check (both original and canonical — trailing slash may differ)
    for p in [original_path, canonical_path]:
        if any(hint.rstrip("/") in p.rstrip("/") or hint in p for hint in _RSS_PATH_HINTS):
            return ResolvedSource(
                canonical_url=canonical,
                source_type="rss",
                connector="rss",
                confidence=0.8,
                hints=["rss_path_pattern"],
            )

    return None


def _infer_web(url: str) -> ResolvedSource:
    """Fallback: treat as generic web source."""
    return ResolvedSource(
        canonical_url=url,
        source_type="web",
        connector="web",
        confidence=0.5,
        hints=["fallback_web"],
    )


# ──────────────────────────────────────────────
# Main resolver
# ──────────────────────────────────────────────

def resolve_source(url: str) -> ResolvedSource:
    """
    Resolve a raw URL into a standardized Source.

    Order of inference:
      1. GitHub (repo / trending)
      2. arXiv (RSS / abs)
      3. RSS / Atom (extension / path)
      4. Web (fallback)

    Raises:
        ValueError: if the URL is invalid (empty, no scheme, etc.)
    """
    if not url or not url.strip():
        raise ValueError("Empty URL")

    original_url = url.strip()

    # Basic validation
    parsed = urlparse(original_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError(f"Invalid hostname in: {original_url}")

    # Canonicalize first
    canonical = canonicalize_url(original_url)

    # Infer type (most specific first) — pass original for metadata, canonical for output
    resolved = _infer_github(original_url)
    if resolved is None:
        resolved = _infer_arxiv(original_url)
    if resolved is None:
        resolved = _infer_rss_atom(original_url)
    if resolved is None:
        resolved = _infer_web(canonical)

    return resolved


def resolve_source_dict(source_input: dict) -> dict:
    """
    Resolve a source dict (from YAML/discovery/manual) into a standardized form.

    If the dict already has 'connector' and 'endpoint', validate them.
    If it only has 'url', resolve it.

    Returns a dict compatible with SourceRegistry / candidates.
    """
    # Already has connector + endpoint? Validate.
    endpoint = source_input.get("endpoint") or source_input.get("url", "")
    connector = source_input.get("connector") or source_input.get("type", "")

    # Connector present but no endpoint (e.g., Exa search, GitHub repo)
    if connector and not endpoint:
        return {
            "connector": connector,
            "source_type": connector,
            "name": source_input.get("name", ""),
            "category": source_input.get("category", source_input.get("default_category")),
            "weight": source_input.get("weight", 5),
            "trust": source_input.get("trust", 1.0),
            "enabled": source_input.get("enabled", source_input.get("active", True)),
            "github_owner": source_input.get("github_owner"),
            "github_repo": source_input.get("github_repo"),
            "github_subtype": source_input.get("github_subtype"),
            "query": source_input.get("query"),
            "max_results": source_input.get("max_results"),
            "metadata": source_input.get("metadata", {}),
        }

    if endpoint and connector:
        canonical = canonicalize_url(endpoint)
        if connector not in AVAILABLE_CONNECTORS:
            logger.warning(f"Unknown connector '{connector}', falling back to resolve")
            resolved = resolve_source(endpoint)
            return {
                "endpoint": resolved.canonical_url,
                "connector": resolved.connector,
                "source_type": resolved.source_type,
                "name": source_input.get("name", ""),
                "category": source_input.get("category", source_input.get("default_category")),
                "weight": source_input.get("weight", 5),
                "trust": source_input.get("trust", 1.0),
                "enabled": source_input.get("enabled", source_input.get("active", True)),
                "metadata": {
                    "resolved_hints": resolved.hints,
                    "confidence": resolved.confidence,
                    **source_input.get("metadata", {}),
                },
            }

    # Only URL provided — resolve it
    if endpoint:
        resolved = resolve_source(endpoint)
        return {
            "endpoint": resolved.canonical_url,
            "connector": resolved.connector,
            "source_type": resolved.source_type,
            "name": source_input.get("name", ""),
            "category": source_input.get("category", source_input.get("default_category")),
            "weight": source_input.get("weight", 5),
            "trust": source_input.get("trust", 0.5),  # Discovered sources start lower
            "enabled": True,
            "metadata": {
                "resolved_hints": resolved.hints,
                "confidence": resolved.confidence,
                **source_input.get("metadata", {}),
            },
        }

    raise ValueError("Source dict must have 'endpoint' or 'url'")

