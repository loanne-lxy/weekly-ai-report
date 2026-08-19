"""Source Registry — load sources.yaml, normalize field names, manage state.

架构:
  sources.yaml  →  初始化配置（只读）
  SourceDB      →  运行时数据库（主存储）
  SourceResolver →  URL → Connector 标准化

读取顺序:
  1. 如果 source.db 有数据，优先从 DB 加载
  2. 如果 DB 为空，从 sources.yaml 初始化并迁移到 DB
  3. 运行时新增的源通过 Resolver 标准化后写入 DB

Reads sources.yaml (new schema: id, name, connector, endpoint, category, weight, trust).
Returns source dicts with both old and new field names for backward compatibility.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Old → new field name aliases
_FIELD_ALIASES = {
    "type": "connector",
    "url": "endpoint",
    "active": "enabled",
    "default_category": "category",
}

# State fields that live in memory/DB, not in sources.yaml
_STATE_FIELDS = [
    "articles_this_week",
    "streak_failures",
    "eval_score",
    "last_fetched",
    "last_success",
]

# Forward reference for type hints
SourceDB = Any


class SourceRegistry:
    """
    Load, normalize, and manage sources.

    优先从 source.db 加载（运行时数据库），如果 DB 为空则从 sources.yaml 初始化。
    """

    DB_PATH = "data/source.db"

    def __init__(self, path: str = "sources.yaml"):
        self.path = path
        self._sources: list[dict[str, Any]] = []
        self._state: dict[str, dict[str, Any]] = {}  # keyed by source id
        self._db: SourceDB | None = None
        self._yaml_loaded = False
        self._load()

    def _load(self):
        """Load sources: prefer DB, fallback to YAML migration."""
        from source_db import SourceDB

        self._db = SourceDB(self.DB_PATH)

        # Try loading from DB first
        db_sources = self._db.get_active()
        if db_sources:
            self._sources = db_sources
            logger.info(f"SourceRegistry loaded {len(self._sources)} sources from {self.DB_PATH}")
        else:
            # DB empty — migrate from YAML
            self._load_from_yaml()

    def _load_from_yaml(self):
        """Load from sources.yaml and migrate to source.db."""
        from source_resolver import resolve_source_dict

        if not os.path.exists(self.path):
            logger.warning(f"SourceRegistry: {self.path} not found, starting with empty registry")
            return

        with open(self.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        raw_sources = data.get("sources", [])

        # Restore state from old schema if present
        for s in raw_sources:
            src_id = s.get("id", "")
            if src_id:
                for field in _STATE_FIELDS:
                    if field in s:
                        self._state.setdefault(src_id, {})[field] = s[field]

        # Normalize + resolve each source, then save to DB
        self._sources = []
        for s in raw_sources:
            # Skip URL resolution for sources that don't have an endpoint/url
            # (e.g. Exa sources use 'query', GitHub sources use 'github_owner')
            has_endpoint = bool(s.get("endpoint") or s.get("url"))
            try:
                if has_endpoint:
                    resolved = resolve_source_dict(s)
                else:
                    resolved = self._normalize(s)
                # Preserve original id if present
                resolved["id"] = s.get("id", resolved.get("id", ""))
                # Preserve state fields
                src_id = s.get("id", "")
                if src_id and src_id in self._state:
                    for k, v in self._state[src_id].items():
                        resolved[k] = v
                self._sources.append(resolved)
                self._db.add_or_update(resolved)
            except Exception as e:
                logger.warning(f"SourceRegistry: failed to resolve source {s.get('name', s.get('url', '?'))}: {e}")
                # Fallback: use normalized dict without resolver
                normalized = self._normalize(s)
                self._sources.append(normalized)

        logger.info(f"SourceRegistry migrated {len(self._sources)} sources from {self.path} to {self.DB_PATH}")

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
        """All sources — read directly from YAML, merge runtime state from DB."""
        if not self._yaml_loaded:
            # Lazy load from YAML on first access
            self._load_from_yaml()
            self._yaml_loaded = True

        if not os.path.exists(self.path):
            return self._sources

        # Read YAML as source of truth for static config
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            return self._sources

        raw_sources = data.get("sources", [])
        result = []
        for s in raw_sources:
            src_id = s.get("id", "")
            resolved = self._normalize(s)
            resolved["id"] = src_id
            # Merge runtime state from DB
            if src_id and src_id in self._state:
                for k, v in self._state[src_id].items():
                    resolved[k] = v
            # DB may have extra runtime fields — merge them
            if src_id:
                db_row = self._db.get_by_id(src_id)
                if db_row:
                    for field in _STATE_FIELDS:
                        if field not in resolved and field in db_row:
                            resolved[field] = db_row[field]
            result.append(resolved)
        return result

    @property
    def active_sources(self) -> list[dict[str, Any]]:
        """Only enabled sources."""
        return [s for s in self._sources if s.get("enabled", s.get("active", True))]

    def get_by_id(self, source_id: str) -> dict[str, Any] | None:
        """Find a source by id."""
        for s in self._sources:
            if s.get("id") == source_id:
                return s
        return None

    def state(self, source_id: str) -> dict[str, Any]:
        """Get or create state dict for a source."""
        return self._state.setdefault(source_id, {
            "articles_this_week": 0,
            "streak_failures": 0,
            "eval_score": 5.0,
            "last_fetched": None,
            "last_success": None,
        })

    def add_source(self, source_input: dict[str, Any]) -> dict[str, Any] | None:
        """
        Add a new source through the Resolver → DB pipeline.

        Args:
            source_input: dict with url/endpoint, optional connector, name, category

        Returns:
            The resolved + saved source dict, or None if resolution failed
        """
        from source_resolver import resolve_source_dict

        try:
            resolved = resolve_source_dict(source_input)
            saved = self._db.add_or_update(resolved)
            if saved:
                self._sources.append(saved)
            return saved
        except Exception as e:
            logger.warning(f"SourceRegistry.add_source failed for {source_input.get('url', '?')}: {e}")
            return None

    def save(self, path: str | None = None):
        """Save sources.yaml (static fields only, state goes to DB)."""
        target = path or self.path
        clean = []
        for s in self._sources:
            entry: dict[str, Any] = {}
            skip_keys = set(_FIELD_ALIASES.keys()) | set(_STATE_FIELDS) | {"canonical_url"}
            for k, v in s.items():
                if k not in skip_keys:
                    entry[k] = v
            clean.append(entry)

        with open(target, "w", encoding="utf-8") as f:
            yaml.dump({"sources": clean}, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        logger.info(f"SourceRegistry saved {len(clean)} sources to {target}")


# Convenience: backward compat for existing code paths
_load_sources = SourceRegistry


def load_sources(path: str = "sources.yaml") -> list[dict[str, Any]]:
    """Backward compat: returns list of source dicts with old field names."""
    registry = SourceRegistry(path)
    return registry.sources
