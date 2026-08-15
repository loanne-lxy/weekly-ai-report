"""Deduplicator — 基于 URL + 日期桶的硬去重。

设计：
1. Key = md5(url + date_bucket)。同一 URL 在同一天只保留一次。
2. 跨天允许重新进入管线（周更场景天然按天隔离）。
3. source_type 已移除：RSS/arXiv/GitHub 每篇文章 URL 唯一，无需加前缀。
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
            """CREATE TABLE IF NOT EXISTS seen_urls (
                dedup_key TEXT PRIMARY KEY,
                url TEXT,
                first_seen TEXT,
                date_bucket TEXT
            )"""
        )
        self.conn.commit()
        # 按天分桶
        self.date_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def make_key(url: str, date_bucket: str) -> str:
        """去重 Key: md5(url:date_bucket)"""
        raw_key = f"{url}:{date_bucket}"
        return hashlib.md5(raw_key.encode("utf-8")).hexdigest()

    def is_new(self, key: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM seen_urls WHERE dedup_key = ?", (key,))
        return cursor.fetchone() is None

    def mark_seen(self, key: str, url: str, bucket: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_urls (dedup_key, url, first_seen, date_bucket) VALUES (?, ?, datetime('now'), ?)",
            (key, url, bucket),
        )
        self.conn.commit()

    def filter_new(self, articles: list[dict]) -> list[dict]:
        new_articles = []
        for a in articles:
            url = a.get("url", "")
            if not url:
                continue

            key = self.make_key(url, self.date_bucket)
            if self.is_new(key):
                self.mark_seen(key, url, self.date_bucket)
                new_articles.append(a)

        logger.info(
            f"Hard Dedup (bucket={self.date_bucket}): {len(articles)} → {len(new_articles)} new"
        )
        return new_articles

    def reset_today(self) -> int:
        """清空当日记录，用于 auto-retry。返回删除条数。"""
        cursor = self.conn.execute(
            "DELETE FROM seen_urls WHERE date_bucket = ?",
            (self.date_bucket,),
        )
        self.conn.commit()
        removed = cursor.rowcount
        if removed:
            logger.info(f"Hard Dedup reset: cleared {removed} entries for {self.date_bucket}")
        return removed
