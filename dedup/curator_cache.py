"""LLM Curator Cache — SHA256 content hash → cached result.

Prompt versioning: cache is keyed on (content_hash, prompt_version) so that
changing curation-rules.md or digest-prompt.md automatically invalidates stale
entries without manual DB cleanup.
"""
import hashlib
import json
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CuratorCache:
    def __init__(self, db_path: str = "data/curator_cache.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS curator_cache (
                content_hash TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                source_name TEXT,
                prompt_version TEXT,
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

    @staticmethod
    def prompt_version(rules_path: str = "references/curation-rules.md",
                       digest_path: str = "references/digest-prompt.md") -> str:
        """Generate version hash from prompt files — changes when prompts change."""
        h = hashlib.sha256()
        for path in [rules_path, digest_path]:
            p = Path(path)
            if p.exists():
                h.update(p.read_bytes())
            else:
                h.update(f"MISSING:{path}".encode())
        return h.hexdigest()[:16]

    def get(self, title: str, summary: str, prompt_version: str | None = None) -> dict | None:
        """Return cached result or None on miss. Returns None if prompt version mismatch."""
        h = self.hash_article(title, summary)
        pv = prompt_version or self.prompt_version()
        cursor = self.conn.execute(
            "SELECT result_json FROM curator_cache WHERE content_hash = ? AND prompt_version = ?",
            (h, pv),
        )
        row = cursor.fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])
        self._misses += 1
        return None

    def set(self, title: str, summary: str, result: dict, source_name: str = "",
            prompt_version: str | None = None):
        """Store curator result for future reuse."""
        h = self.hash_article(title, summary)
        pv = prompt_version or self.prompt_version()
        self.conn.execute(
            "INSERT OR REPLACE INTO curator_cache (content_hash, result_json, source_name, prompt_version) VALUES (?, ?, ?, ?)",
            (h, json.dumps(result, ensure_ascii=False, default=str), source_name, pv),
        )
        self.conn.commit()

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total": total,
            "hit_rate": f"{rate:.1f}%",
        }
