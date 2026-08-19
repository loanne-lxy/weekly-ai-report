"""
Article DB — 文章与事件持久化。

职责：
  - Article 原文存档
  - Event 周级聚合
  - Event ↔ Article 多对多关系
  - Report 周报索引

Schema:
  articles
  ├── id              TEXT PRIMARY KEY (url hash)
  ├── canonical_url   TEXT UNIQUE
  ├── title           TEXT
  ├── summary         TEXT
  ├── published       TEXT  (ISO 8601)
  ├── author          TEXT
  ├── source_id       TEXT
  ├── source_name     TEXT
  ├── category        TEXT
  ├── raw_extra       TEXT  (JSON)
  ├── first_seen      TEXT
  ├── week_bucket     TEXT  (e.g. '2026-W33')

  events
  ├── id              TEXT PRIMARY KEY
  ├── week_label      TEXT
  ├── title           TEXT
  ├── summary         TEXT
  ├── category        TEXT
  ├── importance      REAL  (0-1)
  ├── novelty         REAL  (0-1)
  ├── impact          REAL  (0-1)
  ├── source_count    INTEGER
  ├── created_at      TEXT

  event_articles
  ├── event_id        TEXT
  ├── article_url     TEXT
  ├── PRIMARY KEY(event_id, article_url)

  reports
  ├── id              TEXT PRIMARY KEY
  ├── week_label      TEXT
  ├── report_path     TEXT
  ├── event_count     INTEGER
  ├── article_count   INTEGER
  ├── generated_at    TEXT
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _week_label() -> str:
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _article_id(url: str) -> str:
    """Deterministic article ID from URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────

def _init_db(db_path: str = "data/knowledge.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # ── Articles ──────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id              TEXT PRIMARY KEY,
            url   TEXT UNIQUE,
            title           TEXT,
            summary         TEXT,
            published       TEXT,
            author          TEXT,
            source_id       TEXT,
            source_name     TEXT,
            category        TEXT,
            raw_extra       TEXT DEFAULT '{}',
            first_seen      TEXT,
            week_bucket     TEXT,
            FOREIGN KEY(source_id) REFERENCES sources(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_week ON articles(week_bucket)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category)")

    # ── Migration: rename canonical_url → url ──────────
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(articles)").fetchall()]
        if "canonical_url" in cols and "url" not in cols:
            conn.execute("ALTER TABLE articles RENAME COLUMN canonical_url TO url")
            conn.commit()
    except Exception:
        pass  # Migration already done or not needed

    # ── Sources (mirror of source registry for evidence chain) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            connector       TEXT,
            endpoint        TEXT,
            category        TEXT,
            weight          REAL DEFAULT 5,
            trust           REAL DEFAULT 1.0,
            active          INTEGER DEFAULT 1,
            added_at        TEXT,
            last_seen       TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_connector ON sources(connector)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_category ON sources(category)")

    # ── Events ────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id              TEXT PRIMARY KEY,
            week_label      TEXT,
            title           TEXT,
            summary         TEXT,
            category        TEXT,
            importance      REAL,
            novelty         REAL,
            impact          REAL,
            source_count    INTEGER DEFAULT 0,
            created_at      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_week ON events(week_label)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_category ON events(category)")

    # ── Event-Article mapping ─────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_articles (
            event_id        TEXT,
            article_url     TEXT,
            PRIMARY KEY(event_id, article_url),
            FOREIGN KEY(event_id) REFERENCES events(id),
            FOREIGN KEY(article_url) REFERENCES articles(url)
        )
    """)

    # ── Reports ───────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id              TEXT PRIMARY KEY,
            week_label      TEXT UNIQUE,
            report_path     TEXT,
            event_count     INTEGER DEFAULT 0,
            article_count   INTEGER DEFAULT 0,
            generated_at    TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_week ON reports(week_label)")

    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("raw_extra",):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ──────────────────────────────────────────────
# ArticleDB class
# ──────────────────────────────────────────────

class ArticleDB:
    """Article / Event / Report 持久化层。"""

    def __init__(self, db_path: str = "data/knowledge.db"):
        self.db_path = db_path
        self._conn = _init_db(db_path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Articles ──────────────────────────────────────

    def upsert_article(self, article: dict[str, Any]) -> dict[str, Any] | None:
        """
        Insert or update an article.

        Args:
            article: dict with at least url, title, summary
        """
        url = article.get("url", "") or article.get("canonical_url", "")
        if not url:
            return None

        article_id = _article_id(url)
        now = _now()
        week = _week_label()
        raw_extra = json.dumps(
            article.get("raw_extra", {}),
            ensure_ascii=False,
        )

        # Check if exists
        existing = self._conn.execute(
            "SELECT id FROM articles WHERE url = ?",
            (url,),
        ).fetchone()

        if existing:
            # Update first_seen to keep original, update content
            self._conn.execute("""
                UPDATE articles SET
                    title = ?,
                    summary = ?,
                    published = ?,
                    author = ?,
                    source_id = ?,
                    source_name = ?,
                    category = ?,
                    raw_extra = ?
                WHERE url = ?
            """, (
                article.get("title", ""),
                article.get("summary", ""),
                article.get("published", ""),
                article.get("author", ""),
                article.get("source_id", ""),
                article.get("source_name", ""),
                article.get("category", "") or article.get("primary_category", ""),
                raw_extra,
                url,
            ))
            self._conn.commit()
            return {"id": article_id, "url": url, "action": "updated"}
        else:
            self._conn.execute("""
                INSERT INTO articles
                    (id, url, title, summary, published,
                     author, source_id, source_name, category,
                     raw_extra, first_seen, week_bucket)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article_id, url,
                article.get("title", ""),
                article.get("summary", ""),
                article.get("published", ""),
                article.get("author", ""),
                article.get("source_id", ""),
                article.get("source_name", ""),
                article.get("category", "") or article.get("primary_category", ""),
                raw_extra,
                now,
                week,
            ))
            self._conn.commit()
            return {"id": article_id, "url": url, "action": "inserted"}

    def batch_upsert_articles(self, articles: list[dict[str, Any]]) -> dict[str, int]:
        """
        Batch insert/update articles.

        Returns: {"inserted": N, "updated": M}
        """
        stats = {"inserted": 0, "updated": 0}
        for article in articles:
            result = self.upsert_article(article)
            if result:
                if result["action"] == "inserted":
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1

        logger.info(
            f"ArticleDB: {stats['inserted']} inserted, "
            f"{stats['updated']} updated (total {len(articles)})"
        )
        return stats

    def get_articles(
        self,
        week_label: str | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query articles with optional filters."""
        query = "SELECT * FROM articles WHERE 1=1"
        params: list[Any] = []

        if week_label:
            query += " AND week_bucket = ?"
            params.append(week_label)
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def article_count(self, week_label: str | None = None) -> int:
        """Count articles, optionally filtered by week."""
        if week_label:
            return self._conn.execute(
                "SELECT COUNT(*) FROM articles WHERE week_bucket = ?",
                (week_label,),
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    # ── Events ────────────────────────────────────────

    def clear_events_for_week(self, week_label: str):
        """Clear all events and their links for a given week, so reruns don't duplicate."""
        self._conn.execute("DELETE FROM event_articles WHERE event_id IN (SELECT id FROM events WHERE week_label = ?)", (week_label,))
        self._conn.execute("DELETE FROM events WHERE week_label = ?", (week_label,))
        self._conn.commit()

    def create_event(
        self,
        title: str,
        summary: str = "",
        category: str = "",
        importance: float | None = None,
        novelty: float | None = None,
        impact: float | None = None,
        week_label: str | None = None,
    ) -> str:
        """Create an event and return its ID."""
        import uuid
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now = _now()
        week = week_label or _week_label()

        self._conn.execute("""
            INSERT INTO events
                (id, week_label, title, summary, category,
                 importance, novelty, impact, source_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (
            event_id, week, title, summary, category,
            importance, novelty, impact, now,
        ))
        self._conn.commit()
        return event_id

    def link_article_to_event(
        self,
        event_id: str,
        article_url: str,
    ) -> None:
        """Link an article to an event (many-to-many)."""
        self._conn.execute("""
            INSERT OR IGNORE INTO event_articles
                (event_id, article_url) VALUES (?, ?)
        """, (event_id, article_url))

        # Update source_count
        count = self._conn.execute(
            "SELECT COUNT(*) FROM event_articles WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        self._conn.execute(
            "UPDATE events SET source_count = ? WHERE id = ?",
            (count, event_id),
        )
        self._conn.commit()

    def get_events(
        self,
        week_label: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query events with optional filters."""
        query = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []

        if week_label:
            query += " AND week_label = ?"
            params.append(week_label)
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_event_articles(
        self,
        event_id: str,
    ) -> list[dict[str, Any]]:
        """Get all articles linked to an event."""
        rows = self._conn.execute(
            """
            SELECT a.* FROM articles a
            INNER JOIN event_articles ea ON a.url = ea.article_url
            WHERE ea.event_id = ?
            """,
            (event_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def event_count(self, week_label: str | None = None) -> int:
        """Count events, optionally filtered by week."""
        if week_label:
            return self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE week_label = ?",
                (week_label,),
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ── Reports ───────────────────────────────────────

    def create_report(
        self,
        report_path: str,
        week_label: str | None = None,
        event_count: int = 0,
        article_count: int = 0,
    ) -> str:
        """Create a report record. One report per week (upsert)."""
        now = _now()
        week = week_label or _week_label()
        report_id = f"rpt_{week.replace('-', '')}"

        # Upsert: unique on week_label
        self._conn.execute("""
            INSERT INTO reports (id, week_label, report_path, event_count, article_count, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_label) DO UPDATE SET
                report_path = excluded.report_path,
                event_count = excluded.event_count,
                article_count = excluded.article_count,
                generated_at = excluded.generated_at
        """, (
            report_id, week, report_path,
            event_count, article_count, now,
        ))
        self._conn.commit()
        return report_id

    def get_reports(self) -> list[dict[str, Any]]:
        """Get all reports, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM reports ORDER BY generated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_report(self, week_label: str) -> dict[str, Any] | None:
        """Get a specific report by week label."""
        row = self._conn.execute(
            "SELECT * FROM reports WHERE week_label = ?",
            (week_label,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    # ── Sources ──────────────────────────────────────────────────

    def upsert_source(self, source: dict[str, Any]) -> dict[str, Any] | None:
        """
        Insert or update a source record.

        Args:
            source: dict with at least 'id', 'name', 'connector', 'endpoint'
        """
        source_id = source.get("id", "")
        if not source_id:
            return None

        now = _now()

        existing = self._conn.execute(
            "SELECT id FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()

        if existing:
            self._conn.execute("""
                UPDATE sources SET
                    name = ?, connector = ?, endpoint = ?, category = ?,
                    weight = ?, trust = ?, active = ?, last_seen = ?
                WHERE id = ?
            """, (
                source.get("name", ""),
                source.get("connector", source.get("type", "web")),
                source.get("endpoint", source.get("url", "")),
                source.get("category", ""),
                source.get("weight", 5),
                source.get("trust", 1.0),
                1 if source.get("active", True) else 0,
                now,
                source_id,
            ))
            self._conn.commit()
            return {"id": source_id, "action": "updated"}
        else:
            self._conn.execute("""
                INSERT INTO sources
                    (id, name, connector, endpoint, category, weight, trust, active, added_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source_id,
                source.get("name", ""),
                source.get("connector", source.get("type", "web")),
                source.get("endpoint", source.get("url", "")),
                source.get("category", ""),
                source.get("weight", 5),
                source.get("trust", 1.0),
                1 if source.get("active", True) else 0,
                now,
                now,
            ))
            self._conn.commit()
            return {"id": source_id, "action": "inserted"}

    def sync_sources(self, sources: list[dict[str, Any]]) -> dict[str, int]:
        """
        Sync a list of sources from SourceRegistry into knowledge.db.

        Returns: {"inserted": N, "updated": M}
        """
        stats = {"inserted": 0, "updated": 0}
        for source in sources:
            result = self.upsert_source(source)
            if result:
                if result["action"] == "inserted":
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1

        logger.info(
            f"Source sync: {stats['inserted']} inserted, "
            f"{stats['updated']} updated (total {len(sources)})"
        )
        return stats

    def get_sources(self, active_only: bool = True) -> list[dict[str, Any]]:
        """Query sources."""
        query = "SELECT * FROM sources"
        params: list[Any] = []
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY last_seen DESC"

        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def source_count(self) -> int:
        """Count total sources."""
        return self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    # ── Evidence chain queries ───────────────────────────────────

    def get_evidence_chain(
        self, event_id: str
    ) -> dict[str, Any]:
        """
        Get full evidence chain for an event:
          Event → Articles → Sources
        """
        # Event
        event = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not event:
            return {}

        # Articles linked to this event
        article_rows = self._conn.execute(
            """
            SELECT a.* FROM articles a
            INNER JOIN event_articles ea ON a.url = ea.article_url
            WHERE ea.event_id = ?
            """,
            (event_id,),
        ).fetchall()

        # Sources for these articles
        source_ids = [r["source_id"] for r in article_rows if r.get("source_id")]
        source_rows = []
        if source_ids:
            placeholders = ",".join(["?"] * len(source_ids))
            source_rows = self._conn.execute(
                f"SELECT * FROM sources WHERE id IN ({placeholders})",
                source_ids,
            ).fetchall()

        return {
            "event": _row_to_dict(event),
            "articles": [_row_to_dict(r) for r in article_rows],
            "sources": [_row_to_dict(r) for r in source_rows],
        }
