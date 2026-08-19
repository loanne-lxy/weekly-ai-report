"""Baseline runner — metrics collection, sampling, regression testing.

Usage:
  # Run baseline (sample 20 sources, collect metrics, save artifacts):
  python main.py --baseline

  # Regression test against a saved baseline:
  python main.py --regression baseline/2026-08-17/
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import yaml

# ─── Source sampling ───────────────────────────────────────────

BASELINE_SOURCE_COUNT = 20  # sampled sources for baseline


def sample_sources(sources: list[dict], n: int = BASELINE_SOURCE_COUNT) -> list[dict]:
    """Evenly sample sources by type, preserving type distribution."""
    by_type: dict[str, list[dict]] = {}
    for s in sources:
        t = s.get("connector", s.get("type", "web"))
        by_type.setdefault(t, []).append(s)

    type_counts = {t: len(v) for t, v in by_type.items()}
    total = sum(type_counts.values())
    sampled: list[dict] = []

    for t, pool in by_type.items():
        share = round(n * type_counts[t] / total)
        share = max(1, min(share, len(pool)))
        random.seed(42)  # reproducible
        sampled.extend(random.sample(pool, share))

    # Trim to exact n if rounding overshoot
    if len(sampled) > n:
        random.seed(42)
        sampled = random.sample(sampled, n)

    return sampled


# ─── Metrics collector ─────────────────────────────────────────

class StageMetric:
    __slots__ = ("name", "t_in", "elapsed", "out", "detail")

    def __init__(self, name: str):
        self.name = name
        self.t_in = time.monotonic()
        self.elapsed = 0.0
        self.out = 0
        self.detail: dict[str, Any] = {}

    def finish(self, out: int, detail: dict[str, Any] | None = None) -> "StageMetric":
        self.elapsed = round(time.monotonic() - self.t_in, 2)
        self.out = out
        if detail:
            self.detail = detail
        return self

    def to_dict(self, in_count: int) -> dict:
        d = {
            "name": self.name,
            "in": in_count,
            "out": self.out,
            "elapsed_s": self.elapsed,
            "drop": in_count - self.out,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


class BaselineCollector:
    """Non-intrusive pipeline metrics collector."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.stages: list[tuple[int, StageMetric]] = []
        self._current_in = 0
        self.metadata: dict[str, Any] = {}

    def start_stage(self, name: str, in_count: int):
        self._current_in = in_count
        self.stages.append((in_count, StageMetric(name)))

    def end_stage(self, out_count: int, detail: dict[str, Any] | None = None):
        if self.stages:
            self.stages[-1][1].finish(out_count, detail)

    def set_metadata(self, **kw):
        self.metadata.update(kw)

    def report(self) -> dict:
        total_elapsed = round(time.monotonic() - self.t0, 2)
        stages_list = []
        for in_c, sm in self.stages:
            stages_list.append(sm.to_dict(in_c))
        return {
            "run_id": self.metadata.get("run_id", ""),
            "run_timestamp": datetime.now(timezone.utc).isoformat(),
            "sources_count": self.metadata.get("sources_count", 0),
            "sample_mode": self.metadata.get("sample_mode", "full"),
            "stages": stages_list,
            "total_elapsed_s": total_elapsed,
            "final_article_count": self.stages[-1][1].out if self.stages else 0,
            "category_distribution": self.metadata.get("category_dist", {}),
        }

    def save(self, dir_path: str, raw_articles: list[dict], curated_articles: list[dict]):
        os.makedirs(dir_path, exist_ok=True)

        report = self.report()
        with open(os.path.join(dir_path, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        with open(os.path.join(dir_path, "raw-articles.json"), "w", encoding="utf-8") as f:
            json.dump(raw_articles, f, ensure_ascii=False, indent=2)

        with open(os.path.join(dir_path, "curated-articles.json"), "w", encoding="utf-8") as f:
            json.dump(curated_articles, f, ensure_ascii=False, indent=2)

        # Annotation template
        template = _build_annotation_template(curated_articles)
        with open(os.path.join(dir_path, "annotations_template.json"), "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)

        return report


def _build_annotation_template(articles: list[dict]) -> dict:
    """Build an annotation template for manual labeling."""
    items = []
    for i, a in enumerate(articles, 1):
        items.append({
            "id": i,
            "url": a.get("url", ""),
            "title": a.get("title", a.get("chinese_title", "")),
            "source": a.get("source_name", ""),
            "category": a.get("category", a.get("primary_category", "")),
            "score": a.get("priority_score"),
            # Fields to annotate (fill with null initially):
            "valid": None,          # Is this a real article (not ad/placeholder)?
            "relevant": None,       # Relevant to target domains?
            "duplicate_of": None,   # URL/ID of the article it duplicates, or null
            "same_event_as": [],    # List of IDs sharing the same event
            "content_complete": None,  # Is the content/summary substantive?
            "source_trustworthy": None,  # Is the source credible?
            "notes": "",
        })
    return {
        "instructions": {
            "valid": "true = real article, false = ad/placeholder/water post",
            "relevant": "true = belongs to target domains (LLM/Agent/AI4Science/DesignSim/DigitalTwin)",
            "duplicate_of": "leave null if not a duplicate, otherwise put the URL it duplicates",
            "same_event_as": "list of article IDs that report the same event/news",
            "content_complete": "true = has substantive content/summary, false = title only / too short",
            "source_trustworthy": "true = credible source, false = spam/unknown",
        },
        "articles": items,
    }


# ─── Regression test ───────────────────────────────────────────

def load_baseline(baseline_dir: str) -> tuple[dict, list[dict]]:
    """Load a saved baseline's metrics and raw articles."""
    metrics_path = os.path.join(baseline_dir, "metrics.json")
    raw_path = os.path.join(baseline_dir, "raw-articles.json")
    with open(metrics_path, encoding="utf-8") as f:
        baseline_metrics = json.load(f)
    with open(raw_path, encoding="utf-8") as f:
        raw_articles = json.load(f)
    return baseline_metrics, raw_articles


def regression_report(baseline: dict, current: dict) -> str:
    """Compare current pipeline run against a baseline."""
    lines = [
        "=== Regression Report ===",
        f"Baseline: {baseline.get('run_id', '?')}",
        f"Current:  {current.get('run_id', '?')}",
        "",
        "Stage-by-stage comparison:",
        f"{'Stage':<20} {'Baseline in/out':<20} {'Current in/out':<20} {'Diff (out)':<10}",
        "-" * 70,
    ]

    baseline_stages = {s["name"]: s for s in baseline.get("stages", [])}
    current_stages = {s["name"]: s for s in current.get("stages", [])}
    all_names = sorted(set(list(baseline_stages.keys()) + list(current_stages.keys())))

    for name in all_names:
        b = baseline_stages.get(name, {})
        c = current_stages.get(name, {})
        b_str = f"{b.get('in', '?')} / {b.get('out', '?')}"
        c_str = f"{c.get('in', '?')} / {c.get('out', '?')}"
        b_out = b.get("out", 0) or 0
        c_out = c.get("out", 0) or 0
        diff = c_out - b_out
        diff_str = f"{diff:+d}" if isinstance(diff, int) else "?"
        lines.append(f"{name:<20} {b_str:<20} {c_str:<20} {diff_str:<10}")

    lines.append("-" * 70)
    b_total = baseline.get("final_article_count", 0)
    c_total = current.get("final_article_count", 0)
    lines.append(f"Total articles: {b_total} → {c_total} ({c_total - b_total:+d})")
    lines.append(f"Total elapsed:  {baseline.get('total_elapsed_s', '?')}s → {current.get('total_elapsed_s', '?')}s")

    # Category comparison
    b_cat = baseline.get("category_distribution", {})
    c_cat = current.get("category_distribution", {})
    if b_cat or c_cat:
        lines.append("")
        lines.append("Category distribution:")
        all_cats = sorted(set(list(b_cat.keys()) + list(c_cat.keys())))
        for cat in all_cats:
            b_v = b_cat.get(cat, 0)
            c_v = c_cat.get(cat, 0)
            lines.append(f"  {cat:<20} {b_v:>4} → {c_v:>4} ({c_v - b_v:+d})")

    return "\n".join(lines)
