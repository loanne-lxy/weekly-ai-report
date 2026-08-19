"""
Knowledge Store — 长期累积型持久化层。

职责：
  - Article / Event / Event-Article / Report 幂等持久化
  - 统一事务：一次 Pipeline Run = 一个 BEGIN ... COMMIT
  - Append-mostly + Idempotent Update，不删除历史数据

Schema 增量：
  - report_events(report_id, event_id)  — 新表，Report ↔ Event 多对多

写入策略：
  Article   : UPSERT on url UNIQUE
  Event     : INSERT OR IGNORE（确定性 ID）
  event_articles: INSERT OR IGNORE（UNIQUE PK）
  Report    : UPSERT on week_label UNIQUE
  report_events: INSERT OR IGNORE（UNIQUE PK）

幂等保证：
  同一批 data 连续 persist_run() 两次 → 行数不变（idempotent）
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


def _event_id(title: str, category: str, week_label: str) -> str:
    """Deterministic event ID — stable for same week + same content."""
    raw = f"{week_label}|{category}|{title.strip()}"
    return f"evt_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


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
            url             TEXT UNIQUE,
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

    # Migration: rename canonical_url → url
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(articles)").fetchall()]
        if "canonical_url" in cols and "url" not in cols:
            conn.execute("ALTER TABLE articles RENAME COLUMN canonical_url TO url")
            conn.commit()
    except Exception:
        pass

    # ── Sources ───────────────────────────────────────
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

    # ── Report-Event mapping (NEW) ────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_events (
            report_id       TEXT,
            event_id        TEXT,
            PRIMARY KEY(report_id, event_id),
            FOREIGN KEY(report_id) REFERENCES reports(id),
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
    """)

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
# KnowledgeStore
# ──────────────────────────────────────────────

class KnowledgeStore:
    """长期累积型 Knowledge Store。Append-mostly + Idempotent Update + Transactional."""

    def __init__(self, db_path: str = "data/knowledge.db"):
        self.db_path = db_path
        self._conn = _init_db(db_path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Unified persist ───────────────────────────────

    def persist_run(
        self,
        sources: list[dict[str, Any]],
        articles: list[dict[str, Any]],
        curated_events: list[dict[str, Any]],
        week_label: str,
        report_path: str,
        raw_articles_by_index: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """
        Atomically persist one Pipeline Run into the Knowledge Store.

        All writes are wrapped in a single transaction. On failure, the
        entire run is rolled back — no partial state.

        Args:
            sources:          Source dicts from SourceRegistry
            articles:         Raw article dicts (from fetcher)
            curated_events:   Curated event dicts (each with article_indices, scores)
            week_label:       e.g. '2026-W34'
            report_path:      Path to generated HTML report
            raw_articles_by_index: Mapping for event.article_indices → article dict
                                   (defaults to `articles`)

        Returns:
            {"articles": N, "events": M, "event_articles": K, "sources": S, "report": 1}
        """
        if raw_articles_by_index is None:
            raw_articles_by_index = articles

        conn = self._conn
        stats = {"articles": 0, "events": 0, "event_articles": 0, "sources": 0, "report": 0}

        try:
            conn.execute("BEGIN")

            # ── 1. UPSERT Sources ─────────────────────
            stats["sources"] = self._batch_upsert_sources(conn, sources)

            # ── 2. UPSERT Articles ────────────────────
            stats["articles"] = self._batch_upsert_articles(conn, articles, week_label)

            # ── 3. UPSERT Events ──────────────────────
            stats["events"] = self._batch_upsert_events(
                conn, curated_events, week_label
            )

            # ── 4. UPSERT Event-Article relations ─────
            stats["event_articles"] = self._batch_upsert_event_articles(
                conn, curated_events, raw_articles_by_index, week_label
            )

            # ── 5. UPSERT Report ──────────────────────
            report_id = self._upsert_report(
                conn, week_label, report_path,
                stats["events"], stats["articles"],
            )

            # ── 6. INSERT Report-Event links ──────────
            if report_id:
                self._batch_upsert_report_events(
                    conn, report_id, curated_events, week_label
                )

            conn.execute("COMMIT")
            stats["report"] = 1

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

        logger.info(
            f"KnowledgeStore persist_run: "
            f"sources={stats['sources']}, articles={stats['articles']}, "
            f"events={stats['events']}, event_articles={stats['event_articles']}, "
            f"report={stats['report']}"
        )
        return stats

    # ── Batch operations ──────────────────────────────

    def _batch_upsert_sources(
        self, conn: sqlite3.Connection, sources: list[dict[str, Any]]
    ) -> int:
        if not sources:
            return 0
        now = _now()
        total = 0
        for s in sources:
            sid = s.get("id", "")
            if not sid:
                continue
            conn.execute("""
                INSERT INTO sources
                    (id, name, connector, endpoint, category, weight, trust, active, added_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, connector=excluded.connector,
                    endpoint=excluded.endpoint, category=excluded.category,
                    weight=excluded.weight, trust=excluded.trust,
                    active=excluded.active, last_seen=excluded.last_seen
            """, (
                sid,
                s.get("name", ""),
                s.get("connector", s.get("type", "web")),
                s.get("endpoint", s.get("url", "")),
                s.get("category", ""),
                s.get("weight", 5),
                s.get("trust", 1.0),
                1 if s.get("active", True) else 0,
                now,
                now,
            ))
            total += 1
        return total

    def _batch_upsert_articles(
        self, conn: sqlite3.Connection, articles: list[dict[str, Any]], week_label: str
    ) -> int:
        if not articles:
            return 0
        now = _now()
        total = 0
        for a in articles:
            url = a.get("url", "") or a.get("canonical_url", "")
            if not url:
                continue
            article_id = _article_id(url)
            raw_extra = json.dumps(
                a.get("raw_extra", {}),
                ensure_ascii=False,
            )
            # INSERT OR REPLACE — article_id is derived from url, so on conflict
            # we fully replace with the latest values. first_seen is preserved
            # via a CASE expression.
            conn.execute("""
                INSERT INTO articles
                    (id, url, title, summary, published, author,
                     source_id, source_name, category, raw_extra, first_seen, week_bucket)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    id=excluded.id, title=excluded.title, summary=excluded.summary,
                    published=excluded.published, author=excluded.author,
                    source_id=excluded.source_id, source_name=excluded.source_name,
                    category=excluded.category, raw_extra=excluded.raw_extra,
                    first_seen=CASE WHEN articles.first_seen IS NOT NULL
                                   THEN articles.first_seen ELSE excluded.first_seen END,
                    week_bucket=excluded.week_bucket
            """, (
                article_id, url,
                a.get("title", ""),
                a.get("summary", ""),
                a.get("published", ""),
                a.get("author", ""),
                a.get("source_id", ""),
                a.get("source_name", ""),
                a.get("category", "") or a.get("primary_category", ""),
                raw_extra,
                now,
                week_label,
            ))
            total += 1
        return total

    def _batch_upsert_events(
        self, conn: sqlite3.Connection, events: list[dict[str, Any]], week_label: str
    ) -> int:
        if not events:
            return 0
        now = _now()
        inserted = 0
        for evt in events:
            title = evt.get("event_title", evt.get("title", ""))
            summary = evt.get("event_summary", evt.get("summary", ""))[:2000]
            category = evt.get("category", "")
            eid = _event_id(title, category, week_label)

            # Determine source_count from article_indices
            source_count = len(evt.get("article_indices", []))

            existing = conn.execute(
                "SELECT id FROM events WHERE id = ?", (eid,)
            ).fetchone()

            if existing:
                # Update existing event in-place
                conn.execute("""
                    UPDATE events SET
                        title=?, summary=?, category=?,
                        importance=?, novelty=?, impact=?,
                        source_count=?
                    WHERE id=?
                """, (
                    title, summary, category,
                    evt.get("importance", 0.5),
                    evt.get("novelty", 0.5),
                    evt.get("impact", 0.5),
                    source_count,
                    eid,
                ))
            else:
                conn.execute("""
                    INSERT INTO events
                        (id, week_label, title, summary, category,
                         importance, novelty, impact, source_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    eid, week_label, title, summary, category,
                    evt.get("importance", 0.5),
                    evt.get("novelty", 0.5),
                    evt.get("impact", 0.5),
                    source_count,
                    now,
                ))
                inserted += 1

        return inserted

    def _batch_upsert_event_articles(
        self, conn: sqlite3.Connection,
        events: list[dict[str, Any]],
        raw_articles: list[dict[str, Any]],
        week_label: str,
    ) -> int:
        if not events:
            return 0
        rows = []
        for evt in events:
            eid = _event_id(
                evt.get("event_title", evt.get("title", "")),
                evt.get("category", ""),
                week_label,
            )
            for idx in evt.get("article_indices", []):
                if idx < len(raw_articles):
                    url = raw_articles[idx].get("url", "")
                    if url:
                        rows.append((eid, url))
        if not rows:
            return 0
        conn.executemany("""
            INSERT OR IGNORE INTO event_articles (event_id, article_url) VALUES (?, ?)
        """, rows)
        return len(rows)

    def _upsert_report(
        self, conn: sqlite3.Connection,
        week_label: str, report_path: str,
        event_count: int, article_count: int,
    ) -> str:
        now = _now()
        report_id = f"rpt_{week_label.replace('-', '')}"
        conn.execute("""
            INSERT INTO reports
                (id, week_label, report_path, event_count, article_count, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_label) DO UPDATE SET
                report_path=excluded.report_path,
                event_count=excluded.event_count,
                article_count=excluded.article_count,
                generated_at=excluded.generated_at
        """, (
            report_id, week_label, report_path,
            event_count, article_count, now,
        ))
        return report_id

    def _batch_upsert_report_events(
        self, conn: sqlite3.Connection,
        report_id: str, events: list[dict[str, Any]], week_label: str,
    ) -> None:
        rows = []
        for evt in events:
            eid = _event_id(
                evt.get("event_title", evt.get("title", "")),
                evt.get("category", ""),
                week_label,
            )
            rows.append((report_id, eid))
        if rows:
            conn.executemany("""
                INSERT OR IGNORE INTO report_events (report_id, event_id) VALUES (?, ?)
            """, rows)

    # ── Read methods (backward compatible) ────────────

    def get_articles(
        self,
        week_label: str | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
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
        if week_label:
            return self._conn.execute(
                "SELECT COUNT(*) FROM articles WHERE week_bucket = ?",
                (week_label,),
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    def get_events(
        self,
        week_label: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
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

    def get_event_articles(self, event_id: str) -> list[dict[str, Any]]:
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
        if week_label:
            return self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE week_label = ?",
                (week_label,),
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def get_reports(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM reports ORDER BY generated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_report(self, week_label: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reports WHERE week_label = ?",
            (week_label,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_sources(self, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM sources"
        params: list[Any] = []
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY last_seen DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def source_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    def get_evidence_chain(self, event_id: str) -> dict[str, Any]:
        event = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not event:
            return {}
        article_rows = self._conn.execute(
            """
            SELECT a.* FROM articles a
            INNER JOIN event_articles ea ON a.url = ea.article_url
            WHERE ea.event_id = ?
            """,
            (event_id,),
        ).fetchall()
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

    def sync_sources(
        self, sources: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Backward-compatible: sync sources into knowledge.db (non-transactional)."""
        stats = {"inserted": 0, "updated": 0}
        now = _now()
        for s in sources:
            sid = s.get("id", "")
            if not sid:
                continue
            existing = self._conn.execute(
                "SELECT id FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            if existing:
                self._conn.execute("""
                    UPDATE sources SET
                        name=?, connector=?, endpoint=?, category=?,
                        weight=?, trust=?, active=?, last_seen=?
                    WHERE id=?
                """, (
                    s.get("name", ""),
                    s.get("connector", s.get("type", "web")),
                    s.get("endpoint", s.get("url", "")),
                    s.get("category", ""),
                    s.get("weight", 5),
                    s.get("trust", 1.0),
                    1 if s.get("active", True) else 0,
                    now,
                    sid,
                ))
                stats["updated"] += 1
            else:
                self._conn.execute("""
                    INSERT INTO sources
                        (id, name, connector, endpoint, category, weight, trust, active, added_at, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sid, s.get("name", ""),
                    s.get("connector", s.get("type", "web")),
                    s.get("endpoint", s.get("url", "")),
                    s.get("category", ""),
                    s.get("weight", 5), s.get("trust", 1.0),
                    1 if s.get("active", True) else 0, now, now,
                ))
                stats["inserted"] += 1
        self._conn.commit()
        logger.info(
            f"Source sync: {stats['inserted']} inserted, "
            f"{stats['updated']} updated (total {len(sources)})"
        )
        return stats

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
        """Backward-compatible: create single event (non-transactional)."""
        week = week_label or _week_label()
        event_id = _event_id(title, category, week)
        now = _now()
        existing = self._conn.execute(
            "SELECT id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if existing:
            self._conn.execute("""
                UPDATE events SET title=?, summary=?, category=?,
                    importance=?, novelty=?, impact=?
                WHERE id=?
            """, (title, summary[:2000], category, importance or 0.5,
                  novelty or 0.5, impact or 0.5, event_id))
        else:
            self._conn.execute("""
                INSERT INTO events
                    (id, week_label, title, summary, category,
                     importance, novelty, impact, source_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """, (event_id, week, title, summary[:2000], category,
                  importance or 0.5, novelty or 0.5, impact or 0.5, now))
        self._conn.commit()
        return event_id

    def link_article_to_event(self, event_id: str, article_url: str) -> None:
        """Backward-compatible: link one article to one event."""
        self._conn.execute("""
            INSERT OR IGNORE INTO event_articles (event_id, article_url) VALUES (?, ?)
        """, (event_id, article_url))
        self._conn.commit()

    def upsert_article(self, article: dict[str, Any]) -> dict[str, Any] | None:
        """Backward-compatible: upsert single article (non-transactional)."""
        url = article.get("url", "") or article.get("canonical_url", "")
        if not url:
            return None
        article_id = _article_id(url)
        now = _now()
        week = _week_label()
        raw_extra = json.dumps(article.get("raw_extra", {}), ensure_ascii=False)
        action = "inserted"
        existing = self._conn.execute(
            "SELECT id FROM articles WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            self._conn.execute("""
                UPDATE articles SET
                    title=?, summary=?, published=?, author=?,
                    source_id=?, source_name=?, category=?, raw_extra=?
                WHERE url=?
            """, (
                article.get("title", ""), article.get("summary", ""),
                article.get("published", ""), article.get("author", ""),
                article.get("source_id", ""), article.get("source_name", ""),
                article.get("category", "") or article.get("primary_category", ""),
                raw_extra, url,
            ))
            action = "updated"
        else:
            self._conn.execute("""
                INSERT INTO articles
                    (id, url, title, summary, published, author,
                     source_id, source_name, category, raw_extra, first_seen, week_bucket)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article_id, url,
                article.get("title", ""), article.get("summary", ""),
                article.get("published", ""), article.get("author", ""),
                article.get("source_id", ""), article.get("source_name", ""),
                article.get("category", "") or article.get("primary_category", ""),
                raw_extra, now, week,
            ))
        self._conn.commit()
        return {"id": article_id, "url": url, "action": action}

    def batch_upsert_articles(
        self, articles: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Backward-compatible: batch upsert articles."""
        stats = {"inserted": 0, "updated": 0}
        for a in articles:
            r = self.upsert_article(a)
            if r:
                stats[r["action"]] += 1
        logger.info(
            f"ArticleDB: {stats['inserted']} inserted, "
            f"{stats['updated']} updated (total {len(articles)})"
        )
        return stats

    def create_report(
        self,
        report_path: str,
        week_label: str | None = None,
        event_count: int = 0,
        article_count: int = 0,
    ) -> str:
        """Backward-compatible: create/update report."""
        now = _now()
        week = week_label or _week_label()
        report_id = f"rpt_{week.replace('-', '')}"
        self._conn.execute("""
            INSERT INTO reports
                (id, week_label, report_path, event_count, article_count, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_label) DO UPDATE SET
                report_path=excluded.report_path,
                event_count=excluded.event_count,
                article_count=excluded.article_count,
                generated_at=excluded.generated_at
        """, (report_id, week, report_path, event_count, article_count, now))
        self._conn.commit()
        return report_id


# ── Backward-compatible alias ─────────────────
ArticleDB = KnowledgeStore
