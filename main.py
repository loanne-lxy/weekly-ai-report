"""主流程 — 端到端周报生成"""
import yaml
import logging
import argparse
from datetime import datetime, timezone

from fetcher.fetcher import fetch_all
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
        return yaml.safe_load(f)


def load_sources() -> list[dict]:
    with open("sources.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


async def main():
    parser = argparse.ArgumentParser(description="Weekly AI Report Agent")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching")
    args = parser.parse_args()

    config = load_config()
    sources = load_sources()
    logger.info(f"Loaded {len(sources)} sources")

    # 1. 抓取
    if not args.no_fetch:
        logger.info("=== Phase 1: Fetching ===")
        articles = await fetch_all(sources, config["fetch"].get("concurrency", 10))
        logger.info(f"Fetched {len(articles)} raw articles")
    else:
        articles = []

    # 2. 去重
    logger.info("=== Phase 2: Dedup ===")
    dedup = Deduplicator()
    articles = dedup.filter_new(articles)

    # 3. 关键词预过滤
    logger.info("=== Phase 3: Filter ===")
    llm = LLMClient(config)
    fs = FilterSummarizer(llm, config)
    articles = fs.keyword_pre_filter(articles)

    # 4. LLM 分类（并发）
    logger.info("=== Phase 4: Classify ===")
    articles = await fs._classify_async(articles)

    # 5. LLM 评分 + 中文标题 + 摘要（并发）
    logger.info("=== Phase 5: Enrich (score + CN title + summary) ===")
    articles = await fs._enrich_async(articles)
    articles.sort(key=lambda a: a.get("importance", 5), reverse=True)

    # 6. 生成报告
    week_num = datetime.now(timezone.utc).isocalendar()
    week_label = f"{week_num[0]}-W{week_num[1]:02d}"
    logger.info(f"=== Phase 6: Generate Report ({week_label}) ===")
    report_path = generate_report(articles, config, week_label, llm)
    logger.info(f"Report saved to: {report_path}")

    # 自动在浏览器打开
    import webbrowser
    webbrowser.open(f"file://{report_path}")
    logger.info(f"Opened in browser")

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
