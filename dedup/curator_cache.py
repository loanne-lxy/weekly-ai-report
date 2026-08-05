"""LLM Response Cache — SHA256 content hash → cached curator result

Three-tier caching:
  Tier 1: SQLite dedup (URL-level) — already in dedup/deduplicator.py
  Tier 2: Content hash (SHA256) — this module
  Tier 3: LLM call — only on cache miss

Benefits:
  - Zero cost for duplicate content across different URLs
  - Zero cost when re-running pipeline on same data
  - Persistent across runs (SQLite file)
"""
import hashlib
import json
import sqlite3
import logging

logger = logging.getLogger(__name__)


class CuratorCache:
    def __init__(self, db_path: str = "curator_cache.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS curator_cache (
                content_hash TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                source_name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        self.conn.commit()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def hash_article(title: str, summary: str) -> str:
        """SHA256 hash of (title + first 1000 chars of summary)"""
        text = (title + " " + summary[:1000]).strip()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, title: str, summary: str) -> dict | None:
        """Return cached result or None on miss"""
        h = self.hash_article(title, summary)
        cursor = self.conn.execute(
            "SELECT result_json FROM curator_cache WHERE content_hash = ?", (h,)
        )
        row = cursor.fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])
        self._misses += 1
        return None

    def set(self, title: str, summary: str, result: dict, source_name: str = ""):
        """Store curator result for future reuse"""
        h = self.hash_article(title, summary)
        self.conn.execute(
            "INSERT OR REPLACE INTO curator_cache (content_hash, result_json, source_name) VALUES (?, ?, ?)",
            (h, json.dumps(result, ensure_ascii=False, default=str), source_name),
        )
        self.conn.commit()

    @property
    def stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "total": self._hits + self._misses}
