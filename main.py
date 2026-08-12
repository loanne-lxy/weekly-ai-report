"""主流程 — 端到端周报生成

Pipeline:
  Phase 1  : Fetching (RSS / GitHub / web)
  Phase 2  : Hard Dedup (URL + source_type + date_bucket)
  Phase 2.3: Blacklist Filter (zero-token keyword matching)
  Phase 2.5: Semantic Dedup (FastEmbed vector similarity)
  Phase 3  : LLM Curator (classify + score + summarize, BATCH_SIZE=5)
  Phase 3.5: Top-K Reranking (pairwise comparison for top articles)
  Phase 4  : Merge with existing (cumulative per week)
  Phase 5  : Generate Report (HTML frontend)
  Phase 6  : Source Evaluation & Auto-Discovery
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

from fetcher.ingestion_manager import IngestionManager
from extractors.contract import RawArticle, CuratedArticle
from dedup.deduplicator import Deduplicator
from filter.filter_summarizer import FilterSummarizer
from evaluator.source_evaluator import SourceEvaluator
from evaluator.source_discoverer import SourceDiscoverer
from generator.report_generator import generate_report
from models.llm_client import LLMClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weekly_report")


def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        import yaml
        return yaml.safe_load(f)


def load_sources() -> list[dict]:
    with open("sources.yaml", encoding="utf-8") as f:
        import yaml
        data = yaml.safe_load(f)
    return data.get("sources", [])


def _normalize_category(cat: str) -> str:
    """Map English category names to Chinese for frontend display."""
    return {
        "Design Simulation": "设计仿真",
        "Digital Twin": "数字孪生",
        "AI for Science": "AI for Science",
        "LLM": "LLM",
        "Agent": "Agent",
    }.get(cat, cat)


def _time_boost(days_old: int) -> int:
    """Newer articles get higher score boost."""
    if days_old <= 0: return 3
    elif days_old <= 2: return 2
    elif days_old <= 4: return 1
    elif days_old <= 7: return 0
    elif days_old <= 14: return -1
    else: return -2


async def main():
    parser = argparse.ArgumentParser(description="Weekly AI Report Agent")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching")
    parser.add_argument("--reset", action="store_true", help="Reset dedup DB (for testing)")
    args = parser.parse_args()

    if args.reset:
        for db in ("dedup.db", "curator_cache.db"):
            if os.path.exists(db):
                os.remove(db)
                logger.info(f"Reset {db}")

    config = load_config()
    sources = load_sources()
    logger.info(f"Loaded {len(sources)} sources")

    # ── Phase 1: Fetching ────────────────────────────────────────
    if not args.no_fetch:
        logger.info("=== Phase 1: Fetching ===")
        manager = IngestionManager(
            concurrency=config["fetch"].get("concurrency", 10),
            timeout=config["fetch"].get("timeout", 30),
        )
        raw_articles = await manager.fetch(sources)
        articles = [a.model_dump() for a in raw_articles]
        logger.info(f"Fetched {len(articles)} raw articles")
    else:
        articles = []

    # Apply time-based boost
    now = datetime.now(timezone.utc)
    for a in articles:
        pub = a.get("published", "")
        days_old = 999
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                days_old = (now - pub_dt).days
            except (ValueError, TypeError):
                pass
        a["time_boost"] = _time_boost(days_old)
    logger.info(f"Time boost applied to {len(articles)} articles")

    # Keep a reference for auto-retry if dedup blocks everything
    all_articles = list(articles)

    # Phase 2: Hard dedup (URL + source_type + date_bucket)
    logger.info("=== Phase 2: Hard Dedup ===")
    deduplicator = Deduplicator()
    articles = deduplicator.filter_new(articles)

    # Phase 2.3: Blacklist filter (zero-token keyword matching) — before semantic dedup to save tokens
    logger.info("=== Phase 2.3: Blacklist Filter ===")
    try:
        from filter.blacklist_filter import BlacklistFilter
        articles = BlacklistFilter(config).filter(articles)
    except Exception as e:
        logger.warning(f"Blacklist filter failed (continuing): {e}")

    # Phase 2.5: Semantic dedup (vector similarity)
    logger.info("=== Phase 2.5: Semantic Dedup ===")
    try:
        from dedup.semantic_deduplicator import SemanticDeduplicator
        sdd = SemanticDeduplicator(llm=None)
        articles = sdd.filter(articles)
    except Exception as e:
        logger.warning(f"Semantic dedup failed (continuing without it): {e}")

    # Phase 3: LLM Curator
    logger.info("=== Phase 3: LLM Curator ===")
    llm = LLMClient(config)
    articles = await FilterSummarizer(llm, config)._curate_async(articles)
    articles.sort(
        key=lambda a: a.get("priority_score", 3) + a.get("time_boost", 0),
        reverse=True,
    )
    logger.info(f"Curator result: {len(articles)} relevant articles")

    # ── Auto-retry: if curator got 0 and no accumulator exists,
    # clear today's dedup and re-run dedup chain (avoids lost work) ──
    week_num = datetime.now(timezone.utc).isocalendar()
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"
    week_dir = f"output/{week_label.replace(' ', '_')}"
    acc_path = os.path.join(week_dir, "articles.json")
    has_accumulator = os.path.exists(acc_path)

    if not articles and not has_accumulator:
        cleared = deduplicator.reset_today()
        if cleared:
            logger.info("Dedup reset — re-running through pipeline with %d articles", len(all_articles))
            articles = deduplicator.filter_new(all_articles)
            try:
                from filter.blacklist_filter import BlacklistFilter
                articles = BlacklistFilter(config).filter(articles)
            except Exception:
                pass
            try:
                from dedup.semantic_deduplicator import SemanticDeduplicator
                sdd = SemanticDeduplicator(llm=None)
                articles = sdd.filter(articles)
            except Exception as e:
                logger.warning(f"Semantic dedup retry failed: {e}")
            articles = await FilterSummarizer(llm, config)._curate_async(articles)
            articles.sort(
                key=lambda a: a.get("priority_score", 3) + a.get("time_boost", 0),
                reverse=True,
            )
            logger.info(f"Curator retry result: {len(articles)} relevant articles")

    # ── Phase 3.5: Top-K Reranking (pairwise) ────────────────────
    logger.info("=== Phase 3.5: Top-K Reranking ===")
    try:
        from filter.topk_reranker import TopKReranker
        reranker = TopKReranker(llm, k=config.get("filter", {}).get("top_k", 5))
        articles = await reranker.rerank(articles)
    except Exception as e:
        logger.warning(f"Top-K rerank failed (continuing without it): {e}")

    # ── Phase 4: Merge with existing (cumulative) ────────────────
    week_num = datetime.now(timezone.utc).isocalendar()
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"
    week_dir = f"output/{week_label.replace(' ', '_')}"
    acc_path = os.path.join(week_dir, "articles.json")

    existing = []
    if os.path.exists(acc_path):
        try:
            with open(acc_path) as f:
                existing = json.load(f)
        except Exception:
            pass

    seen_urls = {a.get("url", "") for a in articles}
    for old_a in existing:
        if old_a.get("url", "") not in seen_urls:
            # Normalize category fields
            for key in ("category", "primary_category"):
                if old_a.get(key):
                    old_a[key] = _normalize_category(old_a[key])
            if not old_a.get("primary_category") and old_a.get("category"):
                old_a["primary_category"] = old_a["category"]
            if not old_a.get("category") and old_a.get("primary_category"):
                old_a["category"] = old_a["primary_category"]
            articles.append(old_a)
            seen_urls.add(old_a.get("url", ""))

    articles.sort(
        key=lambda a: a.get("priority_score", 3) + a.get("time_boost", 0),
        reverse=True,
    )
    logger.info(f"Merged: {len(existing)} old + {len(articles) - len(existing)} new → {len(articles)} total")

    # ── Phase 5: Generate Report ─────────────────────────────────
    logger.info(f"=== Phase 5: Generate Report ({week_label}) ===")
    report_path = generate_report(articles, config, week_label, llm)
    logger.info(f"Report saved to: {report_path}")

    abs_path = os.path.abspath(report_path)
    public_url = "https://loanne-lxy.github.io/weekly-ai-report/"
    logger.info(f"公网地址: {public_url}  (推送后自动部署)")
    logger.info(f"本地打开: {abs_path}")

    # ── Phase 6: Source Evaluation & Discovery ───────────────────
    logger.info("=== Phase 6: Evaluate & Discover Sources ===")
    evaluator = SourceEvaluator("sources.yaml", config)
    sources = evaluator.evaluate(articles, sources)

    discoverer = SourceDiscoverer(llm)
    try:
        new_sources = discoverer.discover(articles, {s.get("url", "") for s in sources})
        sources = evaluator.merge_discovered(new_sources, sources)
        evaluator._save(sources)
    except Exception as e:
        logger.warning(f"Source discovery failed (non-fatal): {e}")

    logger.info("Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
