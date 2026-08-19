"""
Source DB — 运行时 Source 数据库 (SQLite)。

职责：
  - 持久化 Source 运行时数据
  - 按 endpoint + connector 去重
  - 提供 Source 的 CRUD 操作
  - sources.yaml 只作为初始化/配置输入，不直接作为运行时状态

Schema:
  sources
  ├── id              TEXT PRIMARY KEY  (deterministic, from make_source_id)
  ├── name            TEXT
  ├── canonical_url   TEXT              (canonicalized endpoint)
  ├── source_type     TEXT              (github_repo, rss, arxiv, web)
  ├── connector       TEXT              (github, rss, arxiv, web)
  ├── category        TEXT              (LLM, Agent, etc.)
  ├── status          TEXT              (active, archived, pending)
  ├── enabled         BOOLEAN
  ├── priority        INTEGER           (1-10)
  ├── trust           REAL              (0-1)
  ├── metadata        TEXT              (JSON)
  ├── created_at      TEXT              (ISO 8601)
  ├── updated_at      TEXT              (ISO 8601)
  ├── articles_this_week INTEGER
  ├── streak_failures  INTEGER
  ├── eval_score       REAL
  ├── last_fetched     TEXT
  └── last_success     TEXT
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _init_db(db_path: str = "data/source.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id              TEXT PRIMARY KEY,
            name            TEXT,
            canonical_url   TEXT,
            source_type     TEXT,
            connector       TEXT,
            category        TEXT,
            status          TEXT DEFAULT 'active',
            enabled         INTEGER DEFAULT 1,
            priority        INTEGER DEFAULT 5,
            trust           REAL DEFAULT 1.0,
            metadata        TEXT DEFAULT '{}',
            created_at      TEXT,
            updated_at      TEXT,
            articles_this_week INTEGER DEFAULT 0,
            streak_failures  INTEGER DEFAULT 0,
            eval_score       REAL DEFAULT 5.0,
            last_fetched     TEXT,
            last_success     TEXT
        )
    """)
    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_connector ON sources(connector)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON sources(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enabled ON sources(enabled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON sources(category)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical ON sources(canonical_url, connector)")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Connector-specific fields that live in the DB `metadata` JSON column
# but are promoted to top-level on read so extractors find them naturally.
_CONNECTOR_FIELDS = {
    "query", "max_results",
    "github_owner", "github_repo", "github_subtype",
}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # Parse metadata JSON
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
    # Promote connector fields from metadata to top level
    if isinstance(d["metadata"], dict):
        for key in _CONNECTOR_FIELDS:
            if key in d["metadata"]:
                d[key] = d["metadata"][key]
    # Convert enabled int -> bool
    d["enabled"] = bool(d.get("enabled", True))
    # Backward compat: derive 'active' from status + enabled
    d["active"] = d["enabled"] and d.get("status") == "active"
    return d


# ══════════════════════════════════════════════
# SourceDB class
# ══════════════════════════════════════════════

class SourceDB:
    """SQLite-backed Source persistence layer."""

    def __init__(self, db_path: str = "data/source.db"):
        self.db_path = db_path
        self._conn = _init_db(db_path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get_all(self) -> list[dict[str, Any]]:
        """Get all sources."""
        rows = self._conn.execute("SELECT * FROM sources ORDER BY priority DESC").fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_active(self) -> list[dict[str, Any]]:
        """Get all enabled + active sources."""
        rows = self._conn.execute(
            "SELECT * FROM sources WHERE enabled = 1 AND status = 'active' ORDER BY priority DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_by_id(self, source_id: str) -> dict[str, Any] | None:
        """Get a source by id."""
        row = self._conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_by_canonical(self, canonical_url: str, connector: str) -> dict[str, Any] | None:
        """Get a source by canonical_url + connector (dedup key)."""
        row = self._conn.execute(
            "SELECT * FROM sources WHERE canonical_url = ? AND connector = ?",
            (canonical_url, connector),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def add_or_update(self, source: dict[str, Any]) -> dict[str, Any] | None:
        """
        Add a new source or update an existing one.

        Uses (canonical_url, connector) as the dedup key.
        Returns the saved source dict.
        """
        now = _now()
        # Merge connector-specific fields into metadata before storage
        meta = dict(source.get("metadata", {}))
        for key in _CONNECTOR_FIELDS:
            if key in source:
                meta[key] = source[key]
        metadata = json.dumps(meta, ensure_ascii=False)

        # For sources without an endpoint (Exa, GitHub repos), canonical_url is empty.
        # Use id as the primary dedup key instead of (canonical_url, connector).
        canonical = source.get("canonical_url", source.get("endpoint", ""))
        src_id = source.get("id", "")

        self._conn.execute("""
            INSERT INTO sources
                (id, name, canonical_url, source_type, connector, category,
                 status, enabled, priority, trust, metadata,
                 created_at, updated_at, articles_this_week, streak_failures, eval_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                name            = EXCLUDED.name,
                canonical_url   = COALESCE(EXCLUDED.canonical_url, sources.canonical_url),
                source_type     = EXCLUDED.source_type,
                category        = EXCLUDED.category,
                status          = EXCLUDED.status,
                enabled         = EXCLUDED.enabled,
                priority        = EXCLUDED.priority,
                trust           = EXCLUDED.trust,
                metadata        = EXCLUDED.metadata,
                updated_at      = EXCLUDED.updated_at,
                eval_score      = COALESCE(EXCLUDED.eval_score, sources.eval_score)
        """, (
            src_id,
            source.get("name", ""),
            canonical,
            source.get("source_type", "web"),
            source.get("connector", "web"),
            source.get("category", source.get("default_category")),
            source.get("status", "active"),
            1 if source.get("enabled", source.get("active", True)) else 0,
            source.get("priority", source.get("weight", 5)),
            source.get("trust", 1.0),
            metadata,
            now,
            now,
            source.get("eval_score", 5.0),
        ))
        self._conn.commit()
        return self.get_by_canonical(
            source.get("canonical_url", source.get("endpoint", "")),
            source.get("connector", "web"),
        )

    def bulk_upsert(self, sources: list[dict[str, Any]]) -> int:
        """Bulk upsert sources. Returns count of rows affected."""
        now = _now()
        count = 0
        for s in sources:
            self.add_or_update(s)
            count += 1
        return count

    def update_state(self, source_id: str, **kwargs: Any) -> None:
        """Update runtime state fields for a source."""
        allowed = {
            "articles_this_week", "streak_failures", "eval_score",
            "last_fetched", "last_success", "status", "enabled",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [source_id]

        self._conn.execute(
            f"UPDATE sources SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    def archive(self, source_id: str) -> None:
        """Archive a source (status = archived)."""
        self.update_state(source_id, status="archived")

    def enable(self, source_id: str) -> None:
        """Enable a source."""
        self.update_state(source_id, enabled=True)

    def disable(self, source_id: str) -> None:
        """Disable a source."""
        self.update_state(source_id, enabled=False)

    def reset_weekly(self) -> None:
        """Reset weekly counters for all sources."""
        self._conn.execute("UPDATE sources SET articles_this_week = 0")
        self._conn.commit()

    def count(self) -> int:
        """Count all sources."""
        return self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    def count_active(self) -> int:
        """Count active + enabled sources."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND status = 'active'"
        ).fetchone()[0]

    def exists(self, canonical_url: str, connector: str) -> bool:
        """Check if a source with this canonical_url + connector exists."""
        row = self._conn.execute(
            "SELECT 1 FROM sources WHERE canonical_url = ? AND connector = ?",
            (canonical_url, connector),
        ).fetchone()
        return row is not None

    def migrate_from_yaml(self, yaml_sources: list[dict[str, Any]], resolver=None) -> int:
        """
        Migrate sources from sources.yaml into the DB.

        Args:
            yaml_sources: Raw source dicts from YAML
            resolver: Optional SourceResolver to normalize sources

        Returns:
            Number of sources migrated
        """
        count = 0
        for s in yaml_sources:
            # If resolver is provided, normalize the source first
            if resolver:
                from source_resolver import resolve_source_dict
                resolved = resolve_source_dict(s)
                resolved["id"] = s.get("id", "")
            else:
                resolved = s
                # Ensure canonical_url field
                resolved.setdefault("canonical_url", resolved.get("endpoint", resolved.get("url", "")))

            self.add_or_update(resolved)
            count += 1

        logger.info(f"Migrated {count} sources from YAML to source.db")
        return count
