"""
URL Registry — URL 去重与变化检测。

职责：
  - 记录所有见过的 URL
  - 硬去重（同一 URL 不重复抓取）
  - 变化检测（content_hash 检测标题党/内容更新）
  - 缓存 ETag/Last-Modified 避免重复请求

Schema:
  url_records
  ├── canonical_url   TEXT PRIMARY KEY
  ├── source_id       TEXT
  ├── first_seen      TEXT  (ISO 8601)
  ├── last_seen       TEXT  (ISO 8601)
  ├── last_fetched    TEXT  (ISO 8601)
  ├── etag            TEXT
  ├── last_modified   TEXT
  ├── content_hash    TEXT  (SHA-256[:16] of url+title+summary)
  ├── status          TEXT  (new, fetched, changed, duplicate)
  ├── date_bucket     TEXT  (e.g., '2026-W33')
  └── metadata        TEXT  (JSON)

流程：
  RSS/GitHub/Exa/Web
        ↓
   URL Canonicalization
        ↓
      URL Registry
        ↓
     seen?
    /     \
  yes      no
  ↓         ↓
 skip     fetch
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────


def content_hash(*, url: str, title: str, summary: str = "") -> str:
    """SHA-256 hash of (url + title + summary) for change detection."""
    raw = f"{url}||{title}||{summary}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def current_date_bucket() -> str:
    """Current date bucket, e.g., '2026-W33' or '2026-08-17'."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────
# SQLite persistence
# ──────────────────────────────────────────────

def _init_db(db_path: str = "data/url_registry.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS url_records (
            canonical_url   TEXT PRIMARY KEY,
            source_id       TEXT,
            first_seen      TEXT,
            last_seen       TEXT,
            last_fetched    TEXT,
            etag            TEXT,
            last_modified   TEXT,
            content_hash    TEXT,
            status          TEXT DEFAULT 'new',
            date_bucket     TEXT,
            metadata        TEXT DEFAULT '{}'
        )
    """)
    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON url_records(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bucket ON url_records(date_bucket)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON url_records(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON url_records(content_hash)")
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
    return d


# ──────────────────────────────────────────────
# URLRegistry class
# ──────────────────────────────────────────────

class URLRegistry:
    """
    URL 去重与变化检测层。

    独立于文章语义 Dedup，只处理 URL 级别的去重。
    """

    def __init__(self, db_path: str = "data/url_registry.db"):
        self.db_path = db_path
        self._conn = _init_db(db_path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Core: Check & Register ─────────────────────

    def is_seen(self, canonical_url: str) -> bool:
        """Check if this URL has been seen before."""
        row = self._conn.execute(
            "SELECT 1 FROM url_records WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        return row is not None

    def check_and_register(
        self,
        canonical_url: str,
        source_id: str,
        title: str = "",
        summary: str = "",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> dict[str, Any]:
        """
        Check if URL is new, and register it.

        Returns dict with:
          - new: bool (True if first time seeing this URL)
          - changed: bool (True if content_hash differs from previous)
          - record: the URL record dict
        """
        now = _now()
        bucket = current_date_bucket()
        c_hash = content_hash(url=canonical_url, title=title, summary=summary)
        metadata = json.dumps({}, ensure_ascii=False)

        # Check existing
        existing = self._conn.execute(
            "SELECT * FROM url_records WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()

        if existing:
            existing_dict = _row_to_dict(existing)
            old_hash = existing_dict.get("content_hash", "")

            # Update last_seen, hash, status
            self._conn.execute("""
                UPDATE url_records SET
                    last_seen = ?,
                    content_hash = ?,
                    last_fetched = ?,
                    status = ?,
                    metadata = ?
                WHERE canonical_url = ?
            """, (now, c_hash, now, "seen_again", metadata, canonical_url))
            self._conn.commit()

            changed = c_hash != old_hash if old_hash else True
            return {
                "new": False,
                "changed": changed,
                "record": existing_dict,
            }
        else:
            # New URL
            self._conn.execute("""
                INSERT INTO url_records
                    (canonical_url, source_id, first_seen, last_seen,
                     last_fetched, etag, last_modified, content_hash,
                     status, date_bucket, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                canonical_url, source_id, now, now, now,
                etag, last_modified, c_hash,
                "new", bucket, metadata,
            ))
            self._conn.commit()

            return {
                "new": True,
                "changed": True,
                "record": {
                    "canonical_url": canonical_url,
                    "source_id": source_id,
                    "first_seen": now,
                    "content_hash": c_hash,
                    "status": "new",
                    "date_bucket": bucket,
                },
            }

    # ── Batch operations ─────────────────────

    def batch_check(
        self,
        articles: list[dict[str, Any]],
    ) -> tuple[list[dict], list[dict]]:
        """
        Batch check articles against registry.

        Args:
            articles: list of dicts with at least 'url', 'source_id'

        Returns:
            (new_articles, seen_articles)
        """
        new_articles = []
        seen_articles = []
        now = _now()
        bucket = current_date_bucket()

        for article in articles:
            url = article.get("url", "")
            source_id = article.get("source_id", "")
            title = article.get("title", "")
            summary = article.get("summary", "")

            if not url:
                continue

            result = self.check_and_register(url, source_id, title, summary)
            if result["new"]:
                new_articles.append(article)
            else:
                seen_articles.append(article)

        logger.info(
            f"URL Registry: {len(new_articles)} new, "
            f"{len(seen_articles)} seen duplicates"
        )
        return new_articles, seen_articles

    # ── Time-window filtering ─────────────────────

    def get_seen_urls(self, days: int = 7) -> set[str]:
        """Get all URLs seen in the last N days."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        rows = self._conn.execute(
            "SELECT canonical_url FROM url_records WHERE last_seen >= ?",
            (cutoff_str,),
        ).fetchall()
        return {row["canonical_url"] for row in rows}

    def get_urls_for_bucket(self, bucket: str) -> list[dict[str, Any]]:
        """Get all URLs registered in a specific date bucket."""
        rows = self._conn.execute(
            "SELECT * FROM url_records WHERE date_bucket = ?",
            (bucket,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Maintenance ─────────────────────

    def expire_old(self, keep_days: int = 30) -> int:
        """
        Remove URL records older than keep_days.

        Returns number of records deleted.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        cutoff_str = cutoff.isoformat()

        result = self._conn.execute(
            "DELETE FROM url_records WHERE last_seen < ?",
            (cutoff_str,),
        )
        self._conn.commit()
        deleted = result.rowcount
        if deleted:
            logger.info(f"URL Registry: expired {deleted} old records")
        return deleted

    def count(self) -> int:
        """Count total URL records."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM url_records"
        ).fetchone()[0]

    def count_today(self) -> int:
        """Count URLs registered today."""
        bucket = current_date_bucket()
        return self._conn.execute(
            "SELECT COUNT(*) FROM url_records WHERE date_bucket = ?",
            (bucket,),
        ).fetchone()[0]

    # ── ETag/Last-Modified cache ─────────────

    def get_cache_headers(
        self, canonical_url: str
    ) -> tuple[str | None, str | None]:
        """
        Get cached ETag and Last-Modified for conditional requests.

        Returns (etag, last_modified) or (None, None).
        """
        row = self._conn.execute(
            "SELECT etag, last_modified FROM url_records WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if row:
            return row["etag"], row["last_modified"]
        return None, None

    def update_cache_headers(
        self,
        canonical_url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Update ETag/Last-Modified cache."""
        self._conn.execute("""
            UPDATE url_records SET etag = ?, last_modified = ?
            WHERE canonical_url = ?
        """, (etag, last_modified, canonical_url))
        self._conn.commit()
