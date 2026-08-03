"""去重模块 — 基于 URL 的 SQLite 去重"""
import sqlite3
import logging

logger = logging.getLogger(__name__)


class Deduplicator:
    def __init__(self, db_path: str = "dedup.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS seen_urls (
                url TEXT PRIMARY KEY,
                first_seen TEXT,
                source TEXT
            )"""
        )
        self.conn.commit()

    def is_new(self, url: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,))
        return cursor.fetchone() is None

    def mark_seen(self, url: str, source: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_urls (url, first_seen, source) VALUES (?, datetime('now'), ?)",
            (url, source),
        )
        self.conn.commit()

    def filter_new(self, articles: list[dict]) -> list[dict]:
        new_articles = []
        for a in articles:
            url = a.get("url", "")
            if url and self.is_new(url):
                self.mark_seen(url, a.get("source_name", ""))
                new_articles.append(a)
        logger.info(
            f"Dedup: {len(articles)} → {len(new_articles)} new"
        )
        return new_articles
