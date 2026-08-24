"""Source Registry — DB-first source management.

架构:
  source.db   →  唯一运行时真相（增删改评分都在这里）
  sources.yaml →  冷启动种子 / 人工快照备份

生命周期:
  1. DB 有数据 → 直接从 DB 加载，YAML 不读
  2. DB 为空   → 从 YAML 一次性迁移到 DB，后续不再读 YAML
  3. 运行时新增 → add_source() 写 DB，立即可见
  4. 快照备份   → save() 把 DB 当前状态写成 YAML（人工回滚用）
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Old → new field name aliases (for YAML cold-start only)
_FIELD_ALIASES = {
    "type": "connector",
    "url": "endpoint",
    "active": "enabled",
    "default_category": "category",
}

# Dead fields from YAML that we strip on migration (use DB eval_score instead)
_DEAD_FIELDS = {"trust", "weight"}


class SourceRegistry:
    """
    Source registry — DB-first.

    source.db 是唯一运行时真相。YAML 仅在 DB 为空时作为冷启动种子。
    """

    DB_PATH = "data/source.db"

    def __init__(self, path: str = "sources.yaml"):
        self.path = path
        self._sources: list[dict[str, Any]] = []
        self._db: Any = None
        self._load()

    def _load(self):
        """Load sources: DB is the single source of truth. YAML = cold-start seed only."""
        from source_db import SourceDB

        self._db = SourceDB(self.DB_PATH)

        # Load from DB — always the truth
        db_sources = self._db.get_active()
        if db_sources:
            self._sources = db_sources
            logger.info(f"SourceRegistry loaded {len(self._sources)} sources from {self.DB_PATH}")
        else:
            # DB empty — one-time migration from YAML seed
            self._load_from_yaml()

    def _load_from_yaml(self):
        """Cold-start: migrate sources.yaml → source.db (one-time only)."""
        from source_resolver import resolve_source_dict

        if not os.path.exists(self.path):
            logger.warning(f"SourceRegistry: {self.path} not found, starting with empty registry")
            return

        import yaml

        with open(self.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        raw_sources = data.get("sources", [])

        self._sources = []
        for s in raw_sources:
            # Strip dead fields
            cleaned = {k: v for k, v in s.items() if k not in _DEAD_FIELDS}

            has_endpoint = bool(cleaned.get("endpoint") or cleaned.get("url"))
            try:
                if has_endpoint:
                    resolved = resolve_source_dict(cleaned)
                else:
                    resolved = self._normalize(cleaned)
                resolved["id"] = cleaned.get("id", resolved.get("id", ""))
                self._sources.append(resolved)
                self._db.add_or_update(resolved)
            except Exception as e:
                logger.warning(f"SourceRegistry: failed to migrate {cleaned.get('name', cleaned.get('url', '?'))}: {e}")

        logger.info(f"SourceRegistry migrated {len(self._sources)} sources from {self.path} → {self.DB_PATH}")

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize a source entry: apply aliases + backward compat."""
        d: dict[str, Any] = {}
        for k, v in raw.items():
            new_key = _FIELD_ALIASES.get(k, k)
            if k in _FIELD_ALIASES and new_key in d:
                continue
            d[new_key] = v

        # Backward compat: set BOTH old and new field names
        for old_key, new_key in _FIELD_ALIASES.items():
            if new_key in d:
                d[old_key] = d[new_key]

        d.setdefault("enabled", True)
        d.setdefault("active", d["enabled"])

        return d

    @property
    def sources(self) -> list[dict[str, Any]]:
        """All active sources — read from DB (single source of truth)."""
        # Refresh from DB to pick up runtime changes (new sources, status updates)
        return self._db.get_active()

    @property
    def active_sources(self) -> list[dict[str, Any]]:
        """Only enabled sources."""
        return [s for s in self.sources
                if s.get("enabled", s.get("active", True))]

    def get_by_id(self, source_id: str) -> dict[str, Any] | None:
        """Find a source by id (from DB)."""
        return self._db.get_by_id(source_id)

    def add_source(self, source_input: dict[str, Any]) -> dict[str, Any] | None:
        """
        Add a new source through Resolver → DB. Visible immediately on next .sources access.
        """
        from source_resolver import resolve_source_dict

        try:
            resolved = resolve_source_dict(source_input)
            saved = self._db.add_or_update(resolved)
            if saved:
                logger.info(f"SourceRegistry added new source: {saved.get('name', saved.get('id'))}")
            return saved
        except Exception as e:
            logger.warning(f"SourceRegistry.add_source failed for {source_input.get('url', '?')}: {e}")
            return None

    def save(self, path: str | None = None):
        """
        Snapshot: write current DB state to YAML as a human-readable backup.
        This is NOT loaded at runtime — use only for manual rollback (--restore-from-yaml).
        """
        target = path or self.path
        clean = []
        for s in self.sources:
            entry: dict[str, Any] = {}
            skip_keys = set(_FIELD_ALIASES.keys()) | {
                "canonical_url",
                # Runtime-only fields — don't snapshot these to YAML
                "articles_this_week", "streak_failures", "eval_score",
                "last_fetched", "last_success", "active",
                "endpoint", "url",  # aliases of canonical_url
            }
            for k, v in s.items():
                if k not in skip_keys and v is not None:
                    entry[k] = v
            clean.append(entry)

        with open(target, "w", encoding="utf-8") as f:
            import yaml
            yaml.dump({"sources": clean}, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        logger.info(f"SourceRegistry snapshot: {len(clean)} sources → {target}")


# Convenience: backward compat for existing code paths
_load_sources = SourceRegistry


def load_sources(path: str = "sources.yaml") -> list[dict[str, Any]]:
    """Backward compat: returns list of source dicts with old field names."""
    registry = SourceRegistry(path)
    return registry.sources
