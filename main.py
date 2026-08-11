"""主流程 — 端到端周报生成"""
import os
import yaml
import logging
import argparse
from datetime import datetime, timezone

from fetcher.ingestion_manager import IngestionManager
from extractors.contract import RawArticle
from dedup.deduplicator import Deduplicator
from filter.source_priority import filter_and_weight as source_priority_filter
from filter.keyword_filter import score_and_filter as keyword_filter
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
        return yaml.safe_load(f)


def load_sources() -> list[dict]:
    with open("sources.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


async def main():
    parser = argparse.ArgumentParser(description="Weekly AI Report Agent")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching")
    parser.add_argument("--reset", action="store_true", help="Reset dedup DB (for testing)")
    args = parser.parse_args()

    if args.reset:
        import os as _os
        db = "dedup.db"
        if _os.path.exists(db):
            _os.remove(db)
            logger.info("Reset dedup database")

    config = load_config()
    sources = load_sources()
    logger.info(f"Loaded {len(sources)} sources")

    # 1. 抓取
    if not args.no_fetch:
        logger.info("=== Phase 1: Fetching ===")
        manager = IngestionManager(
            concurrency=config["fetch"].get("concurrency", 10),
            timeout=config["fetch"].get("timeout", 30),
        )
        raw_articles = await manager.fetch(sources)
        # RawArticle → dict 兼容下游 pipeline
        articles = [a.model_dump() for a in raw_articles]
        logger.info(f"Fetched {len(articles)} raw articles")

        # Time-based weight boost: newer articles rank higher
        from datetime import timedelta
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
            # Boost: today +3, 1-2d +2, 3-4d +1, 5-7d 0, 8-14d -1, older -2
            if days_old <= 0: boost = 3
            elif days_old <= 2: boost = 2
            elif days_old <= 4: boost = 1
            elif days_old <= 7: boost = 0
            elif days_old <= 14: boost = -1
            else: boost = -2
            a["time_boost"] = boost
        logger.info(f"Time boost applied to {len(articles)} articles")
    else:
        articles = []

    # 2. 去重
    logger.info("=== Phase 2: Dedup ===")
    dedup = Deduplicator()
    articles = dedup.filter_new(articles)

    # 3. Source priority filter — remove noise sources, assign tier weights
    logger.info("=== Phase 3: Source Priority ===")
    articles = source_priority_filter(articles)

    # 4. Keyword lexicon + regex — score articles by domain keyword density
    logger.info("=== Phase 4: Keyword + Regex ===")
    articles = keyword_filter(articles, min_score=1)

    # 5. LLM curator — importance scoring + summarization (MiniLM removed)
    logger.info("=== Phase 5: Curator (score + summarize) ===")
    llm = LLMClient(config)
    fs = FilterSummarizer(llm, config)
    articles = await fs._curate_async(articles)
    articles.sort(key=lambda a: a.get("priority_score", 3) + a.get("time_boost", 0), reverse=True)

    # 7. 合并本周已有文章（累积而非替换）
    week_num = datetime.now(timezone.utc).isocalendar()
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"
    week_dir = f"output/{week_label.replace(' ', '_')}"
    import json as _json
    acc_path = os.path.join(week_dir, "articles.json")
    existing = []
    if os.path.exists(acc_path):
        try:
            with open(acc_path) as f:
                existing = _json.load(f)
        except Exception:
            pass
    # Merge: new articles first, then deduplicate by URL
    # English → Chinese category mapping
    _CAT_MAP = {
        "Design Simulation": "设计仿真",
        "Digital Twin": "数字孪生",
        "AI for Science": "AI for Science",
        "LLM": "LLM",
        "Agent": "Agent",
    }
    seen_urls = {a.get("url", "") for a in articles}
    for old_a in existing:
        if old_a.get("url", "") not in seen_urls:
            # Normalize: old articles may use 'category' instead of 'primary_category'
            if not old_a.get("primary_category") and old_a.get("category"):
                old_a["primary_category"] = old_a["category"]
            if not old_a.get("category") and old_a.get("primary_category"):
                old_a["category"] = old_a["primary_category"]
            # Normalize English → Chinese
            for key in ("category", "primary_category"):
                raw = old_a.get(key)
                if raw and raw in _CAT_MAP:
                    old_a[key] = _CAT_MAP[raw]
            articles.append(old_a)
            seen_urls.add(old_a.get("url", ""))
    articles.sort(key=lambda a: a.get("priority_score", 3) + a.get("time_boost", 0), reverse=True)
    logger.info(f"Merged with existing: {len(existing)} old + new → {len(articles)} total")

    # 8. 生成报告
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"
    logger.info(f"=== Phase 6: Generate Report ({week_label}) ===")
    report_path = generate_report(articles, config, week_label, llm)
    logger.info(f"Report saved to: {report_path}")

    # 自动在浏览器打开 + 公网 URL
    import webbrowser
    abs_path = os.path.abspath(report_path)
    webbrowser.open(f"file://{abs_path}")
    public_url = "https://loanne-lxy.github.io/weekly-ai-report/"
    logger.info(f"公网地址: {public_url}  (推送后自动部署)")
    logger.info(f"本地打开: {abs_path}")

    # 7. 源池自评估 + 自动发现
    logger.info("=== Phase 7: Evaluate & Discover Sources ===")
    evaluator = SourceEvaluator("sources.yaml", config)
    sources = evaluator.evaluate(articles, sources)

    discoverer = SourceDiscoverer(llm)
    new_sources = discoverer.discover(articles, {s.get("url", "") for s in sources})
    sources = evaluator.merge_discovered(new_sources, sources)
    evaluator._save(sources)

    logger.info("Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
