"""
Fetch Manager — 统一 HTTP 基础设施层。

职责：
  - HTTP 请求执行 (aiohttp)
  - 重试 + 指数退避
  - 超时控制 (per-request)
  - ETag / Last-Modified 缓存
  - Per-domain 速率限制
  - 统一 aiohttp session 管理

Connector 只构造 FetchRequest → FetchManager 负责稳定执行。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────

@dataclass
class FetchRequest:
    """Connector 构造的请求描述."""
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 30.0

    # Auto-computed
    cache_key: str = ""
    domain: str = ""

    def __post_init__(self):
        if not self.domain:
            try:
                self.domain = (urlparse(self.url).hostname or "").lower().lstrip("www.")
            except Exception:
                self.domain = "unknown"
        if not self.cache_key:
            self.cache_key = hashlib.md5(
                f"{self.method}:{self.url}".encode()
            ).hexdigest()[:16]


@dataclass
class FetchResult:
    """FetchManager 统一返回."""
    url: str
    status: int
    content: bytes = b""
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    cached: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


# ──────────────────────────────────────────────
# In-memory ETag / Last-Modified cache
# ──────────────────────────────────────────────

class CacheStore:
    """Lightweight cache for ETag/Last-Modified headers."""

    def __init__(self, max_size: int = 500, ttl: int = 3600):
        self._store: dict[str, dict[str, Any]] = {}
        self._max = max_size
        self._ttl = ttl

    def get(self, key: str) -> dict | None:
        entry = self._store.get(key)
        if entry and time.time() - entry["ts"] < self._ttl:
            return entry
        return None

    def set(self, key: str, etag: str | None, last_modified: str | None):
        if len(self._store) >= self._max:
            oldest = min(self._store, key=lambda k: self._store[k]["ts"])
            del self._store[oldest]
        self._store[key] = {
            "etag": etag,
            "last_modified": last_modified,
            "ts": time.time(),
        }

    def clear(self):
        self._store.clear()


# ──────────────────────────────────────────────
# Domain rate limiter
# ──────────────────────────────────────────────

class DomainRateLimiter:
    """Per-domain rate limiter: max N requests per window."""

    def __init__(self, max_per_second: float = 2.0, max_per_minute: int = 30):
        self.max_per_second = max_per_second
        self.max_per_minute = max_per_minute
        self._timestamps: dict[str, list[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, domain: str):
        lock = self._lock(domain)
        async with lock:
            now = time.monotonic()
            timestamps = self._timestamps.get(domain, [])

            # Clean old entries (60s window)
            timestamps = [t for t in timestamps if now - t < 60]

            # Check per-minute limit
            if len(timestamps) >= self.max_per_minute:
                wait = 60 - (now - timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    timestamps = [t for t in timestamps if time.monotonic() - t < 60]

            # Check per-second limit
            recent = [t for t in timestamps if now - t < 1.0]
            if len(recent) >= self.max_per_second:
                await asyncio.sleep(1.0)

            timestamps.append(time.monotonic())
            self._timestamps[domain] = timestamps

    def reset_domain(self, domain: str):
        self._timestamps.pop(domain, None)


# ──────────────────────────────────────────────
# FetchManager — main orchestrator
# ──────────────────────────────────────────────

class FetchManager:
    """
    统一 HTTP 基础设施层。

    Connector 构造 FetchRequest → FetchManager 执行。

    Features:
    - 自动重试 (指数退避, 最多 3 次)
    - 超时控制 (per-request)
    - ETag / Last-Modified 缓存 (304 not modified)
    - Per-domain 速率限制
    - 统一 aiohttp session 管理
    - 并发限制 (Semaphore)
    """

    DEFAULT_RETRIES = 3
    DEFAULT_BACKOFF = 1.0  # Base delay in seconds
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_CONCURRENCY = 10

    def __init__(
        self,
        concurrency: int = DEFAULT_CONCURRENCY,
        default_timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
        rate_limit_per_second: float = 2.0,
        rate_limit_per_minute: int = 30,
        cache_ttl: int = 3600,
        user_agent: str = "Weekly-AI-Report-Agent/1.0",
    ):
        self.concurrency = concurrency
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.user_agent = user_agent

        # Components
        self.cache = CacheStore(ttl=cache_ttl)
        self.rate_limiter = DomainRateLimiter(
            max_per_second=rate_limit_per_second,
            max_per_minute=rate_limit_per_minute,
        )
        self.semaphore = asyncio.Semaphore(concurrency)

        # Per-domain failure tracking
        self._domain_failures: dict[str, int] = {}

        # Session managed externally or internally
        self._session: aiohttp.ClientSession | None = None
        self._owns_session: bool = False

    async def start(self):
        """Create internal aiohttp session."""
        connector = aiohttp.TCPConnector(limit=self.concurrency)
        self._session = aiohttp.ClientSession(connector=connector)
        self._owns_session = True

    async def close(self):
        """Close internal session if we own it."""
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None
            self._owns_session = False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    def set_session(self, session: aiohttp.ClientSession):
        """Use an externally managed session."""
        self._session = session
        self._owns_session = False

    # ── Core: Fetch a single request ─────────────────────

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Execute a single FetchRequest with full infrastructure."""
        session = self._session
        if session is None:
            connector = aiohttp.TCPConnector(limit=10)
            session = aiohttp.ClientSession(connector=connector)
            try:
                return await self._fetch_with_retry(session, request)
            finally:
                await session.close()
        return await self._fetch_with_retry(session, request)

    async def _fetch_with_retry(
        self, session: aiohttp.ClientSession, request: FetchRequest
    ) -> FetchResult:
        """Execute with semaphore, rate limit, retry, and cache."""
        async with self.semaphore:
            # Rate limiting
            await self.rate_limiter.acquire(request.domain)

            # Check cache (ETag / Last-Modified)
            ck = request.cache_key or "unknown"
            cached = self.cache.get(ck)
            if cached:
                result = await self._fetch_with_cache(session, request, cached)
                if result.not_modified:
                    logger.debug(f"304 Not Modified: {request.url[:80]}")
                    return FetchResult(
                        url=request.url,
                        status=304,
                        cached=True,
                        etag=cached.get("etag"),
                        last_modified=cached.get("last_modified"),
                    )

            # Execute with retry
            last_result: FetchResult = FetchResult(
                url=request.url, status=0, error="no_attempt",
            )
            for attempt in range(self.max_retries):
                result = await self._execute_request(session, request)
                last_result = result

                if result.ok:
                    # Cache ETag/Last-Modified for next time
                    if result.etag or result.last_modified:
                        self.cache.set(ck, result.etag, result.last_modified)
                    self._domain_failures[request.domain] = 0
                    return result

                # Retry on 5xx or network errors
                retryable = result.status >= 500 or result.error is not None
                if not retryable:
                    return result

                wait = self.DEFAULT_BACKOFF * (2 ** attempt)
                logger.warning(
                    f"Retry {attempt + 1}/{self.max_retries} for "
                    f"{request.url[:80]}: {result.error or f'HTTP {result.status}'}"
                )
                await asyncio.sleep(wait)

            # All retries exhausted
            self._domain_failures[request.domain] = (
                self._domain_failures.get(request.domain, 0) + 1
            )
            logger.error(
                f"Fetch failed after {self.max_retries} retries: "
                f"{request.url[:80]}, last error: "
                f"{last_result.error or f'HTTP {last_result.status}'}"
            )
            return last_result

    async def _fetch_with_cache(
        self, session: aiohttp.ClientSession, request: FetchRequest,
        cached: dict,
    ) -> FetchResult:
        """Try conditional fetch with ETag/Last-Modified headers."""
        headers = dict(request.headers)
        etag = cached.get("etag")
        lm = cached.get("last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if lm:
            headers["If-Modified-Since"] = lm
        cached_request = FetchRequest(**{**request.__dict__, "headers": headers})
        return await self._execute_request(session, cached_request)

    async def _execute_request(
        self, session: aiohttp.ClientSession, request: FetchRequest
    ) -> FetchResult:
        """Execute the raw HTTP request."""
        try:
            timeout = ClientTimeout(total=request.timeout or self.default_timeout)
            headers = {"User-Agent": self.user_agent, **request.headers}

            async with session.request(
                method=request.method,
                url=request.url,
                timeout=timeout,
                headers=headers,
                data=request.body,
                allow_redirects=True,
            ) as resp:
                content = await resp.read()
                text = content.decode("utf-8", errors="replace")
                etag = resp.headers.get("ETag")
                last_modified = resp.headers.get("Last-Modified")
                resp_headers = dict(resp.headers)

                return FetchResult(
                    url=request.url,
                    status=resp.status,
                    content=content,
                    text=text,
                    headers=resp_headers,
                    etag=etag,
                    last_modified=last_modified,
                )

        except asyncio.TimeoutError:
            return FetchResult(
                url=request.url,
                status=0,
                error=f"timeout after {request.timeout or self.default_timeout}s",
            )
        except aiohttp.ClientResponseError as e:
            return FetchResult(
                url=request.url,
                status=e.status,
                error=str(e),
            )
        except Exception as e:
            return FetchResult(
                url=request.url,
                status=0,
                error=str(e),
            )

    # ── Convenience: text / bytes fetchers ────────────────

    async def fetch_text(self, url: str, **kwargs: Any) -> FetchResult:
        """Quick text fetch for simple use cases."""
        return await self.fetch(FetchRequest(url=url, **kwargs))

    async def fetch_bytes(self, url: str, **kwargs: Any) -> FetchResult:
        """Quick bytes fetch (e.g., RSS XML)."""
        return await self.fetch(FetchRequest(url=url, **kwargs))

    # ── Health / diagnostics ─────────────────────────────

    def domain_health(self) -> dict[str, int]:
        """Return per-domain failure counts."""
        return dict(self._domain_failures)

    def is_domain_dead(self, domain: str, threshold: int = 3) -> bool:
        """Check if a domain has hit the failure threshold."""
        return self._domain_failures.get(domain, 0) >= threshold

    def reset_domain(self, domain: str):
        """Reset failure count and rate limit for a domain."""
        self._domain_failures.pop(domain, None)
        self.rate_limiter.reset_domain(domain)
        self.cache.clear()
