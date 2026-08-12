"""Deduplicator — md5(url + source_type + date_bucket) keyed SQLite dedup.

Daily buckets prevent same-URL updates from being incorrectly filtered.
"""
import hashlib
import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Deduplicator:
    def __init__(self, db_path: str = "dedup.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS seen_items (
                dedup_key TEXT PRIMARY KEY,
                url TEXT,
                first_seen TEXT,
                source TEXT,
                date_bucket TEXT
            )"""
        )
        self.conn.commit()
        self.date_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def make_key(url: str, source_type: str, date_bucket: str) -> str:
        """Generate dedup key: md5(url:source_type:date_bucket)."""
        raw_key = f"{url}:{source_type}:{date_bucket}"
        return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

    def is_new(self, key: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM seen_items WHERE dedup_key = ?", (key,))
        return cursor.fetchone() is None

    def mark_seen(self, key: str, url: str, source: str, bucket: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items (dedup_key, url, first_seen, source, date_bucket) VALUES (?, ?, datetime('now'), ?, ?)",
            (key, url, source, bucket),
        )
        self.conn.commit()

    def filter_new(self, articles: list[dict]) -> list[dict]:
        new_articles = []
        for a in articles:
            url = a.get("url", "")
            source_type = a.get("source_type", "web")
            
            if not url:
                continue

            key = self.make_key(url, source_type, self.date_bucket)
            if self.is_new(key):
                self.mark_seen(key, url, a.get("source_name", ""), self.date_bucket)
                new_articles.append(a)
        
        logger.info(
            f"Dedup (bucket={self.date_bucket}): {len(articles)} → {len(new_articles)} new"
        )
        return new_articles
