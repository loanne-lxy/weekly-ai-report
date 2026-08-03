"""源池自评估 — 每周评估信息来源质量，自动更新权重"""
import yaml
import logging

logger = logging.getLogger(__name__)


class SourceEvaluator:
    def __init__(self, sources_path: str, config: dict):
        self.sources_path = sources_path
        self.min_weekly = config["evaluator"].get("min_weekly_output", 0)
        self.stale_weeks = config["evaluator"].get("stale_weeks", 4)

    def evaluate(self, articles: list[dict], current_sources: list[dict]) -> list[dict]:
        """根据本周产出评估源池质量，返回更新后的源列表"""
        # 统计每个源的产出
        stats: dict[str, int] = {}
        for a in articles:
            src = a.get("source_name", "")
            stats[src] = stats.get(src, 0) + 1

        for s in current_sources:
            name = s.get("name", "")
            count = stats.get(name, 0)
            s["articles_this_week"] = count

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
                s["active"] = False

        # 过滤出活跃源，按评分排序
        active = [s for s in current_sources if s.get("active", True)]
        active.sort(key=lambda x: x.get("eval_score", 5), reverse=True)

        logger.info(
            f"Source eval: {len(current_sources)} total, "
            f"{len(active)} active, "
            f"{len(current_sources) - len(active)} archived"
        )

        self._save(active)
        return active

    def merge_discovered(self, discovered: list[dict], current: list[dict]) -> list[dict]:
        """合并Agent发现的新源到源池"""
        existing_urls = {s.get("url", "") for s in current}
        added = 0
        for ds in discovered:
            if ds["url"] not in existing_urls and len(current) < 80:
                current.append(ds)
                existing_urls.add(ds["url"])
                added += 1
        logger.info(f"Discovered {len(discovered)} sources, merged {added} new")
        return current

    def _save(self, sources: list[dict]):
        with open(self.sources_path, "w", encoding="utf-8") as f:
            yaml.dump({"sources": sources}, f, allow_unicode=True, default_flow_style=False)
