"""源池自评估 — 每周评估信息来源质量，自动更新权重。

Uses SourceRegistry for persistence to avoid data loss and keep
sources.yaml clean (static fields only, state in memory).
"""
import logging

from source_registry import SourceRegistry

logger = logging.getLogger(__name__)


class SourceEvaluator:
    def __init__(self, sources_path: str, config: dict):
        self.sources_path = sources_path
        self.min_weekly = config.get("evaluator", {}).get("min_weekly_output", 0)
        self.stale_weeks = config.get("evaluator", {}).get("stale_weeks", 4)
        self.protected_types = set(config.get("evaluator", {}).get("protect_types", []))

    def evaluate(self, articles: list[dict], current_sources: list[dict]) -> list[dict]:
        """根据本周产出评估源池质量，返回更新后的全部源列表（含 inactive）。

        Caller is responsible for filtering if needed.
        """
        protect_types = self.protected_types

        # 统计每个源的产出
        stats: dict[str, int] = {}
        for a in articles:
            src = a.get("source_name", "")
            stats[src] = stats.get(src, 0) + 1

        for s in current_sources:
            name = s.get("name", "")
            # Support both old ('type') and new ('connector') field names
            source_type = s.get("connector", s.get("type", ""))
            count = stats.get(name, 0)

            # Skip evaluation for protected types (rate-limited ≠ stale)
            if source_type in protect_types:
                continue

            # 更新评估分
            prev_score = s.get("eval_score", s.get("weight", 5))
            if count >= self.min_weekly:
                s["eval_score"] = min(10, prev_score + 1)
                s["streak_failures"] = 0
            elif count == 0:
                s["streak_failures"] = s.get("streak_failures", 0) + 1
                s["eval_score"] = max(0, prev_score - 2)
            else:
                s["streak_failures"] = 0
                s["eval_score"] = max(0, prev_score - 1)

            # 连续 stale_weeks 无产出 → 归档
            if s.get("streak_failures", 0) >= self.stale_weeks:
                logger.info(f"Archiving stale source: {name}")
                s["enabled"] = False
                s["active"] = False

        all_sources = current_sources
        active = [s for s in current_sources if s.get("enabled", s.get("active", True))]
        active.sort(key=lambda x: x.get("eval_score", 5), reverse=True)

        logger.info(
            f"Source eval: {len(all_sources)} total, "
            f"{len(active)} active, "
            f"{len(all_sources) - len(active)} archived"
        )

        # NOTE: Do NOT save here. The caller owns persistence after
        # merge_discovered() to avoid data loss from partial overwrites.
        return all_sources

    def merge_discovered(self, discovered: list[dict], current: list[dict]) -> list[dict]:
        """合并Agent发现的新源到源池。Supports both 'url' and 'endpoint' keys."""
        existing_endpoints = {
            s.get("endpoint", s.get("url", "")) for s in current
        }
        added = 0
        for ds in discovered:
            ds_url = ds.get("endpoint", ds.get("url", ""))
            if ds_url not in existing_endpoints and len(current) < 80:
                current.append(ds)
                existing_endpoints.add(ds_url)
                added += 1
        logger.info(f"Discovered {len(discovered)} sources, merged {added} new")
        return current

    def _save(self, sources: list[dict]):
        """Save via SourceRegistry to keep sources.yaml clean (static fields only)."""
        registry = SourceRegistry(self.sources_path)
        registry._sources = sources
        registry.save()
