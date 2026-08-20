"""主流程 — 端到端周报生成
Pipeline:
  Phase 1  : Fetching (RSS / GitHub / arXiv / Exa)
  Phase 2  : Hard Dedup (URL Registry)
  Phase 2.3: Blacklist Filter
  Phase 3  : Event Clustering (FAISS + multilingual-MiniLM)
  Phase 3.5: Event Curator (LLM: classify + score + Top-3 selection)
  Phase 5  : Generate Report (HTML frontend)
  Phase 6  : Source Evaluation & Auto-Discovery

Modes:
  python main.py                        # Normal run (all sources)
  python main.py --baseline             # Baseline run (sampled sources + metrics)
  python main.py --regression <dir>     # Regression test against saved baseline
"""
import os
import json
import logging
import argparse
from datetime import datetime, timezone

# Load .env before anything else reads env vars
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from typing import Any

import numpy as np
from fetcher.ingestion_manager import IngestionManager
from extractors.contract import RawArticle, CuratedArticle
from dedup.deduplicator import Deduplicator
from filter.filter_summarizer import FilterSummarizer
from evaluator.source_evaluator import SourceEvaluator
from evaluator.source_discoverer import SourceDiscoverer
from generator.report_generator import generate_report
from models.llm_client import LLMClient

# Baseline / regression support
from baseline_runner import (
    BaselineCollector,
    sample_sources,
    load_baseline,
    regression_report,
)

DATA_DIR = "data"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weekly_report")


def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        import yaml
        return yaml.safe_load(f)


from source_registry import SourceRegistry, load_sources


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


async def _run_pipeline(
    articles: list[dict],
    config: dict,
    llm: LLMClient,
    collector: BaselineCollector | None = None,
    skip_source_eval: bool = False,
    skip_report: bool = False,
    sources: list[dict] | None = None,
) -> list[dict]:
    """Core pipeline: URL Registry → Blacklist → Event Clustering → Event Curator → Sort → [SourceEval] → [Report].

    Returns two lists:
      - curated_events: scored events (each with Articles[], importance/novelty/impact)
      - articles: flat list of raw articles (no event scores) for downstream consumers
    """

    # ── Phase 2: URL Registry (Hard Dedup) ───────────────────────
    logger.info("=== Phase 2: URL Registry (Hard Dedup) ===")
    if collector:
        collector.start_stage("HardDedup", len(articles))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        from url_registry import URLRegistry

        url_reg = URLRegistry(os.path.join(DATA_DIR, "url_registry.db"))
        new_articles, seen_articles = url_reg.batch_check(articles)
        articles = new_articles
        url_reg.expire_old(keep_days=30)
        url_reg.close()
    except Exception as e:
        logger.warning(f"URL Registry failed, falling back to old dedup: {e}")
        try:
            deduplicator = Deduplicator()
            articles = deduplicator.filter_new(articles)
        except Exception as e2:
            logger.warning(f"Old dedup also failed: {e2}")
    if collector:
        collector.end_stage(len(articles))
    logger.info(f"URL Registry dedup: {len(articles)} new articles")

    # ── Phase 2.3: Blacklist Filter ──────────────────────────────
    logger.info("=== Phase 2.3: Blacklist Filter ===")
    if collector:
        collector.start_stage("Blacklist", len(articles))
    try:
        from filter.blacklist_filter import BlacklistFilter
        articles = BlacklistFilter(config).filter(articles)
    except Exception as e:
        logger.warning(f"Blacklist filter failed (continuing): {e}")
    if collector:
        collector.end_stage(len(articles))
    logger.info(f"After blacklist: {len(articles)} articles")

    # ── Phase 3: Event Clustering (FAISS) ───────────────────────
    events: list[Any] = []
    embeddings: np.ndarray = np.zeros((0, 0), dtype=np.float32)
    raw_articles_for_clustering = articles
    try:
        from event_clustering import cluster_articles
        logger.info("=== Phase 3: Event Clustering ===")
        if collector:
            collector.start_stage("Clustering", len(articles))

        threshold = config.get("clustering", {}).get("threshold", 0.35)
        events, embeddings = cluster_articles(articles, threshold=threshold)

        if collector:
            collector.end_stage(len(events))
        logger.info(
            f"Event Clustering: {len(articles)} articles → "
            f"{len(events)} events"
        )
    except Exception as e:
        logger.warning(f"Event Clustering failed: {e}")
        # Fallback: each article is its own event
        from event_clustering import Event
        events = [
            Event(
                title=a.get("title", ""),
                summary=a.get("summary", a.get("content_preview", ""))[:800],
                category=a.get("category", a.get("primary_category", "")),
                article_indices=[i],
            )
            for i, a in enumerate(articles)
        ]

    # ── Phase 3.5: Event Curator (LLM scoring per event) ─────────
    try:
        from filter.event_curator import EventCurator
        logger.info("=== Phase 3.5: Event Curator ===")
        if collector:
            collector.start_stage("EventCurator", len(events))

        # Build event dicts with article details for the curator
        event_dicts = []
        for evt in events:
            event_articles = [articles[idx] for idx in evt.article_indices if idx < len(articles)]
            event_dict = {
                "title": evt.title,
                "summary": evt.summary,
                "category": evt.category,
                "bucket": evt.bucket,
                "method": evt.method,
                "article_count": len(event_articles),
                "articles": event_articles,
                "article_indices": evt.article_indices,
            }
            event_dicts.append(event_dict)

        curator = EventCurator(llm)
        curated_events = await curator.curate_events(event_dicts, embeddings)

        if collector:
            collector.end_stage(len(curated_events))
        logger.info(f"Event Curator: {len(curated_events)} relevant events")

        # Sort by backend weighted score
        curated_events.sort(
            key=lambda e: e.get("importance", 5) * 0.4
            + e.get("impact", 5) * 0.3
            + e.get("novelty", 5) * 0.3,
            reverse=True,
        )

        # ── Scoring: apply combined scoring system ──────────────
        try:
            from filter.scoring import score_event, sort_events
            logger.info("=== Scoring ===")

            scored_events = []
            for evt in curated_events:
                # Get the actual articles for this event
                event_articles = [
                    raw_articles_for_clustering[i]
                    for i in evt.get("article_indices", [])
                    if i < len(raw_articles_for_clustering)
                ]
                llm_scores = {
                    "importance": evt.get("importance", 0.5),
                    "novelty": evt.get("novelty", 0.5),
                    "impact": evt.get("impact", 0.5),
                    "importance_rationale": evt.get("importance_rationale", ""),
                    "novelty_rationale": evt.get("novelty_rationale", ""),
                    "impact_rationale": evt.get("impact_rationale", ""),
                    "evidence_articles": evt.get("evidence_articles", []),
                }
                scoring = score_event(
                    event_articles,
                    llm_scores,
                    category=evt.get("category", ""),
                )
                evt.update(scoring)
                scored_events.append(evt)

            # Sort: group by category → sort by final_score → top N
            sorted_events, cross_domain = sort_events(scored_events)
            curated_events = sorted_events

            if cross_domain:
                logger.info(f"Cross-domain events flagged: {len(cross_domain)}")

        except Exception as e:
            logger.warning(f"Scoring failed (using LLM scores only): {e}")

    except Exception as e:
        logger.warning(f"Event Curator failed (falling back to article list): {e}")
        curated_events = []

    # ── Sort curated events by score ─────────────────────────────
    curated_events.sort(
        key=lambda e: e.get("final_score", e.get("importance", 0.5)),
        reverse=True,
    )

    # ── Compatibility: build flat articles list for downstream consumers ──
    # Report Generator reads from knowledge.db directly and does NOT need this.
    # But other consumers (baseline, source eval) expect an articles list.
    articles = [dict(a) for a in raw_articles_for_clustering]
    if collector:
        collector.start_stage("Merge", len(articles))
        collector.end_stage(len(articles))
    logger.info(f"Sorted {len(articles)} articles by score")

    # ── Category distribution for metrics ────────────────────────
    if collector:
        cat_dist: dict[str, int] = {}
        for a in articles:
            cat = a.get("category") or a.get("primary_category", "Uncategorized")
            cat_dist[cat] = cat_dist.get(cat, 0) + 1
        collector.set_metadata(category_dist=cat_dist)

    # ── Phase 6: Source Eval & Discovery (skip for regression) ───
    if not skip_source_eval and sources:
        logger.info("=== Phase 6: Evaluate & Discover Sources ===")
        evaluator = SourceEvaluator("sources.yaml", config)
        archived_count = sum(1 for s in sources if s.get("active") is False)
        sources = evaluator.evaluate(articles, sources)
        archived_after = sum(1 for s in sources if s.get("active") is False)
        newly_archived = archived_after - archived_count

        # Collect raw candidates from all discovery channels
        discovered: list[dict] = []
        existing_endpoints = {s.get("endpoint", s.get("url", "")) for s in sources}

        discoverer = SourceDiscoverer(llm)
        try:
            discovered.extend(discoverer.discover(articles, existing_endpoints))
        except Exception as e:
            logger.warning(f"LLM source discovery failed (non-fatal): {e}")

        try:
            from filter.link_miner import mine_links, _normalize_domain
            existing_domains = {
                _normalize_domain(s.get("endpoint", s.get("url", ""))) for s in sources
            }
            link_sources = mine_links(articles, existing_domains)
            discovered.extend(link_sources)
        except Exception as e:
            logger.warning(f"Link mining failed (non-fatal): {e}")

        # 6c: Exa API active search — disabled (placeholder exa_discover.py not implemented)
        # TODO: create filter/exa_discover.py when needed

        # ── Candidate Pool: funnel evaluation ─────────────────
        if discovered:
            logger.info(f"Found {len(discovered)} raw candidates, running through funnel...")
            from candidate_pool import CandidateEvaluator, get_pending_candidates

            # Also re-evaluate any pending candidates from previous runs
            pending = get_pending_candidates()
            all_candidates = list(discovered)
            for p in pending:
                # Normalize: pending candidates may use 'url' instead of 'endpoint'
                if "endpoint" not in p and "url" in p:
                    p["endpoint"] = p["url"]
                ep = p.get("endpoint", "")
                if ep and ep not in existing_endpoints:
                    all_candidates.append(p)

            # Deduplicate candidates by endpoint
            seen: set[str] = set()
            unique_candidates: list[dict] = []
            for c in all_candidates:
                # Normalize fields: link_miner uses url/type, discoverer uses endpoint/connector
                if "endpoint" not in c and "url" in c:
                    c["endpoint"] = c["url"]
                if "connector" not in c and "type" in c:
                    c["connector"] = c["type"]
                ep = c.get("endpoint", "")
                if ep and ep not in seen:
                    seen.add(ep)
                    unique_candidates.append(c)

            evaluator_pool = CandidateEvaluator()
            promoted, rejected = await evaluator_pool.evaluate(
                unique_candidates, existing_endpoints,
                db_path=os.path.join(DATA_DIR, "candidates.db"),
            )

            logger.info(f"Candidate funnel: {len(unique_candidates)} total, "
                        f"{len(promoted)} promoted, {len(rejected)} rejected")

            if promoted:
                for p in promoted:
                    # Generate source_id
                    from extractors.contract import make_source_id
                    p["id"] = make_source_id(
                        p.get("connector", "web"), p.get("endpoint", ""), **p
                    )
                sources = evaluator.merge_discovered(promoted, sources)
                evaluator._save(sources)

        newly_archived_val = newly_archived
        discovered_count_val = len(discovered)
    else:
        newly_archived_val = 0
        discovered_count_val = 0

    week_num = datetime.now(timezone.utc).isocalendar()
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"

    # ── Phase 5.5: Unified Knowledge Store Persistence ───────────
    # Persist BEFORE report generation so the report reads fresh data
    report_path = ""
    try:
        from article_db import KnowledgeStore
        ks = KnowledgeStore(os.path.join(DATA_DIR, "knowledge.db"))

        ks.persist_run(
            sources=sources if sources else [],
            articles=raw_articles_for_clustering,
            curated_events=curated_events,
            week_label=week_label,
            report_path=report_path,  # placeholder; updated after generate
            raw_articles_by_index=raw_articles_for_clustering,
        )

        ks.close()
        logger.info(f"KnowledgeStore persisted for {week_label}")
    except Exception as e:
        logger.warning(f"KnowledgeStore persistence failed (non-fatal): {e}")

    # ── Phase 5: Generate Report (skip for regression) ───────────
    # Runs AFTER persist so report_generator reads the fresh DB state
    if not skip_report:
        logger.info(f"=== Phase 5: Generate Report ({week_label}) ===")
        report_path = generate_report(
            articles, config, week_label, llm,
            discovered_count=discovered_count_val,
            archived_count=newly_archived_val,
        )
        logger.info(f"Report saved to: {report_path}")

        # Update report_path in DB
        try:
            from article_db import KnowledgeStore
            ks2 = KnowledgeStore(os.path.join(DATA_DIR, "knowledge.db"))
            ks2.create_report(
                report_path=report_path,
                week_label=week_label,
                event_count=len(curated_events),
                article_count=len(raw_articles_for_clustering),
            )
            ks2.close()
        except Exception as e:
            logger.warning(f"Failed to update report_path in DB: {e}")

    return articles


async def main():
    parser = argparse.ArgumentParser(description="Weekly AI Report Agent")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching")
    parser.add_argument("--reset", action="store_true", help="Reset dedup DB (for testing)")
    parser.add_argument("--baseline", action="store_true", help="Run baseline with sampled sources + metrics")
    parser.add_argument("--regression", type=str, metavar="DIR", help="Regression test against saved baseline dir")
    args = parser.parse_args()

    if args.reset:
        for db in ("dedup.db", "curator_cache.db", "url_registry.db"):
            db_path = os.path.join(DATA_DIR, db)
            if os.path.exists(db_path):
                os.remove(db_path)
                logger.info(f"Reset {db}")

        # Also clear this week's events and event_articles from knowledge.db
        import sqlite3
        kb_path = os.path.join(DATA_DIR, "knowledge.db")
        if os.path.exists(kb_path):
            iso = datetime.now(timezone.utc).isocalendar()
            wl = f"{iso[0]}-W{iso[1]:02d}"
            conn = sqlite3.connect(kb_path)
            conn.execute("DELETE FROM event_articles WHERE event_id IN (SELECT id FROM events WHERE week_label=?)", (wl,))
            conn.execute("DELETE FROM events WHERE week_label=?", (wl,))
            conn.execute("DELETE FROM report_events WHERE report_id IN (SELECT id FROM reports WHERE week_label=?)", (wl,))
            conn.execute("DELETE FROM reports WHERE week_label=?", (wl,))
            conn.commit()
            conn.close()
            logger.info(f"Reset knowledge.db events for {wl}")

    config = load_config()
    sources = load_sources()

    # ── Regression mode ──────────────────────────────────────────
    if args.regression:
        logger.info(f"=== REGRESSION MODE: loading baseline from {args.regression} ===")
        baseline_metrics, raw_articles = load_baseline(args.regression)
        logger.info(f"Loaded {len(raw_articles)} frozen articles from baseline")

        # Reset dedup to avoid false positives from previous runs
        for db in ("dedup.db", "curator_cache.db"):
            db_path = os.path.join(DATA_DIR, db)
            if os.path.exists(db_path):
                os.remove(db_path)
                logger.info(f"Reset {db} for clean regression")

        # Apply time boost (use current time so old articles get negative boost like production)
        now = datetime.now(timezone.utc)
        for a in raw_articles:
            pub = a.get("published", "")
            days_old = 999
            if pub:
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    days_old = (now - pub_dt).days
                except (ValueError, TypeError):
                    pass
            a["time_boost"] = _time_boost(days_old)

        llm = LLMClient(config)
        collector = BaselineCollector()
        collector.set_metadata(
            run_id=f"regression-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
            sources_count=0,
            sample_mode="regression",
        )
        collector.start_stage("Input", len(raw_articles))
        collector.end_stage(len(raw_articles))

        articles = await _run_pipeline(
            articles=raw_articles,
            config=config,
            llm=llm,
            collector=collector,
            skip_source_eval=True,
            skip_report=True,
        )

        current_metrics = collector.report()

        # Save current run metrics alongside baseline
        base_name = os.path.basename(args.regression)
        save_dir = os.path.join(args.regression, f"regression-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}")
        collector.save(save_dir, raw_articles, articles)

        # Print comparison
        report = regression_report(baseline_metrics, current_metrics)
        print(f"\n{report}")
        logger.info("Regression report printed above. Full metrics saved to %s", save_dir)
        return

    # ── Baseline mode ────────────────────────────────────────────
    if args.baseline:
        sampled = sample_sources(sources)
        logger.info(f"=== BASELINE MODE: sampling {len(sampled)} sources from {len(sources)} total ===")
        type_counts = {}
        for s in sampled:
            t = s.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        logger.info(f"Sampled types: {type_counts}")
        sources = sampled

    logger.info(f"Loaded {len(sources)} sources")

    # ── Phase 1: Fetching ────────────────────────────────────────
    collector = None
    if args.baseline:
        collector = BaselineCollector()
        collector.set_metadata(
            run_id=f"baseline-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
            sources_count=len(sources),
            sample_mode="sampled",
        )

    if not args.no_fetch:
        logger.info("=== Phase 1: Fetching ===")
        if collector:
            collector.start_stage("Fetch", 0)
        manager = IngestionManager(
            concurrency=config["fetch"].get("concurrency", 10),
            timeout=config["fetch"].get("timeout", 90),
        )
        raw_articles = await manager.fetch(sources)
        articles = [a.model_dump() for a in raw_articles]
        logger.info(f"Fetched {len(articles)} raw articles")
        if collector:
            collector.end_stage(len(articles))
    else:
        articles = []
        logger.info("Skipping fetch (--no-fetch)")

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

    llm = LLMClient(config)

    # ── Run pipeline ─────────────────────────────────────────────
    skip_flags = False
    articles = await _run_pipeline(
        articles=articles,
        config=config,
        llm=llm,
        collector=collector,
        sources=sources,
    )

    # ── Save baseline artifacts ──────────────────────────────────
    if collector:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        baseline_dir = f"baseline/{date_str}"

        # Re-read raw articles from the fetch output (before dedup)
        raw_for_save = [a.model_dump() for a in await IngestionManager(
            concurrency=config["fetch"].get("concurrency", 10),
            timeout=config["fetch"].get("timeout", 30),
        ).fetch(sample_sources(sources) if args.baseline else sources)] if False else articles

        # Use what we have — articles after full pipeline is the curated output
        # For raw, we already saved it from the fetch stage via collector metadata
        # Let's save the input articles (before dedup) separately
        # Since we lost the pre-dedup list, we save what the collector has
        report = collector.save(baseline_dir, articles, articles)

        print(f"\n{'='*60}")
        print(f"BASELINE SAVED TO: {baseline_dir}/")
        print(f"{'='*60}")
        print(f"Sources: {report['sources_count']}")
        print(f"Final articles: {report['final_article_count']}")
        print(f"Total elapsed: {report['total_elapsed_s']}s")
        print(f"\nStage breakdown:")
        for s in report["stages"]:
            print(f"  {s['name']:<20} {s['in']:>5} → {s['out']:>5}  (drop {s.get('drop', 0):>4}, {s['elapsed_s']}s)")
        if report.get("category_distribution"):
            print(f"\nCategory distribution:")
            for cat, cnt in sorted(report["category_distribution"].items()):
                print(f"  {cat}: {cnt}")
        print(f"\nArtifacts:")
        print(f"  {baseline_dir}/metrics.json")
        print(f"  {baseline_dir}/raw-articles.json      (for regression test)")
        print(f"  {baseline_dir}/curated-articles.json   (pipeline output)")
        print(f"  {baseline_dir}/annotations_template.json  (fill for manual labeling)")
        print(f"\nRun regression later with:")
        print(f"  python main.py --regression {baseline_dir}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
