"""W37 端到端验证驱动 — 用桌面缓存的 W37 文章重跑管线（跳过 source-eval）。

不碰 source.db，只写 knowledge.db + output/。跑完可人工检查 output/2026-W37/。
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
import yaml
import main
from models.llm_client import LLMClient
from main import _run_pipeline

CACHE = sys.argv[1] if len(sys.argv) > 1 else \
    "/mnt/c/Users/loanne/Desktop/2026-W37/articles.json"


async def go():
    cfg = yaml.safe_load(open("config.yaml"))
    with open(CACHE, encoding="utf-8") as f:
        articles = json.load(f)
    # time boost
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
        a["time_boost"] = main._time_boost(days_old)
    print(f"[driver] {len(articles)} articles from cache", flush=True)

    llm = LLMClient(cfg)
    out = await _run_pipeline(
        articles=articles, config=cfg, llm=llm,
        collector=None, skip_source_eval=True, skip_report=False, sources=[],
    )
    print(f"[driver] done -> {out}", flush=True)


if __name__ == "__main__":
    asyncio.run(go())