"""报告生成 — 从 knowledge.db 读取 Events，渲染 HTML 周报。

数据流:
  Pipeline → knowledge.db (sources + articles + events + reports)
  ↓
  report_generator.py → 查询 knowledge.db → 按 Event 渲染 HTML
  ↓
  output/{week}/index.html + {category}.html

展示单位: Event (而非 Article)
  首页: 5 个分类卡片 (图标 + 名称 + 事件数 + 领域摘要)
  子页: Event 卡片列表 (评分徽章 + 事件标题 + 摘要 + 来源统计)
"""
import os
import json
import logging
from datetime import datetime
from typing import Any

import sqlite3
from jinja2 import Environment, FileSystemLoader
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)


def _query_knowledge_db(week_label: str, db_path: str = "data/knowledge.db") -> dict[str, Any]:
    """
    从 knowledge.db 查询本周所有 Events + Articles + Sources。

    Returns:
        {
            "events": list[dict],
            "articles_by_event": dict[event_id -> list[dict]],
            "sources": dict[source_id -> dict],
            "stats": {category: count},
        }
    """
    if not os.path.exists(db_path):
        logger.warning(f"Knowledge DB not found: {db_path}")
        return {"events": [], "articles_by_event": {}, "sources": {}, "stats": {}}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── Events for this week ────────────────────────────────
    events_rows = conn.execute(
        """
        SELECT * FROM events WHERE week_label = ?
        ORDER BY importance DESC
        """,
        (week_label,),
    ).fetchall()

    events = []
    event_ids = []
    for row in events_rows:
        evt = dict(row)
        events.append(evt)
        event_ids.append(evt["id"])

    # ── Articles linked to these events ─────────────────────
    articles_by_event = {}
    if event_ids:
        placeholders = ",".join(["?"] * len(event_ids))
        rows = conn.execute(
            f"""
            SELECT a.* FROM articles a
            INNER JOIN event_articles ea ON a.url = ea.article_url
            WHERE ea.event_id IN ({placeholders})
            """,
            event_ids,
        ).fetchall()

        for row in rows:
            article = dict(row)
            # Find which event this article belongs to
            mapping_rows = conn.execute(
                "SELECT event_id FROM event_articles WHERE article_url = ?",
                (article["url"],),
            ).fetchall()
            for mapping in mapping_rows:
                eid = mapping["event_id"]
                if eid not in articles_by_event:
                    articles_by_event[eid] = []
                articles_by_event[eid].append(article)

    # ── Sources ──────────────────────────────────────────────
    source_rows = conn.execute(
        "SELECT * FROM sources WHERE active = 1"
    ).fetchall()
    sources = {dict(r)["id"]: dict(r) for r in source_rows}

    # ── Stats by category ────────────────────────────────────
    stats = {}
    for evt in events:
        cat = evt.get("category", "Uncategorized")
        if cat not in stats:
            stats[cat] = 0
        stats[cat] += 1

    conn.close()
    return {
        "events": events,
        "articles_by_event": articles_by_event,
        "sources": sources,
        "stats": stats,
    }


def _load_fallback_events(week_label: str, empty_cats: list[str],
                          db_path: str = "data/knowledge.db") -> dict[str, list[dict]]:
    """
    从历史周报回填空领域的事件，最多回溯 4 周。
    """
    if not os.path.exists(db_path):
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 获取所有已记录的周标签
    week_rows = conn.execute(
        "SELECT DISTINCT week_label FROM events ORDER BY week_label DESC"
    ).fetchall()
    weeks = [r["week_label"] for r in week_rows]

    # 找到当前周的索引，然后回溯
    current_idx = None
    for i, w in enumerate(weeks):
        if w == week_label:
            current_idx = i
            break

    if current_idx is None:
        conn.close()
        return {}

    # 回溯最多 4 周
    fallback: dict[str, list[dict]] = {}
    remaining = set(empty_cats)
    for week_offset in range(1, 5):
        if not remaining:
            break
        target_idx = current_idx + week_offset
        if target_idx >= len(weeks):
            break
        target_week = weeks[target_idx]

        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE week_label = ? AND category IN ({placeholders})
            ORDER BY importance DESC
            """.format(placeholders=",".join(["?"] * len(remaining))),
            [target_week] + list(remaining),
        ).fetchall()

        for row in rows:
            evt = dict(row)
            cat = evt.get("category", "")
            if cat in remaining and cat not in fallback:
                fallback[cat] = []
            if cat in fallback:
                evt["carried_over"] = True
                fallback[cat].append(evt)
                remaining.discard(cat)

    conn.close()
    return fallback


def _get_trends(llm: LLMClient, events: list[dict], categories: list[dict]) -> dict[str, str]:
    """LLM 生成各领域本周趋势关键词"""
    cats: dict[str, list] = {}
    for cat_cfg in categories:
        cats[cat_cfg["name"]] = []

    for evt in events:
        cat = evt.get("category", "")
        if cat in cats:
            text = evt.get("event_summary", evt.get("title", ""))
            cats[cat].append(text[:300])

    trends = {}
    for cat_name, texts in cats.items():
        if not texts:
            trends[cat_name] = "无相关资讯"
            continue
        sample = "\n".join(f"- {t}" for t in texts[:3])
        try:
            keyword = llm.chat(
                system_prompt=(
                    "You are an AI trend analyst. Based on event summaries, identify the single most "
                    "prominent technology trend or theme in simple Chinese (2-6 characters). "
                    "Good examples: 多模态融合, MoE架构, Agent自主性, 端侧推理, 开源生态, "
                    "蛋白质设计, 物理AI, 实时孪生. "
                    "Bad examples: 技术发展, 行业动态, AI应用. "
                    "Return ONLY the keyword, no explanation."
                ),
                user_prompt=(
                    f"Domain: {cat_name}\n"
                    f"Following are this week's top events. Identify the most prominent trend:\n\n"
                    f"{sample}\n\n"
                    f"Trend keyword (2-6 Chinese characters only)："
                ),
            )
            cleaned = keyword.strip().replace("。", "").replace("，", "").replace("：", "").replace(":", "")
            trends[cat_name] = cleaned[:8]
        except Exception:
            trends[cat_name] = "持续关注"
    return trends


def _domain_summary(llm: LLMClient, cat_name: str, event_summaries: list[str]) -> str:
    """Generate a one-paragraph summary for a domain section"""
    sample = "\n".join(f"- {s}" for s in event_summaries[:3])
    try:
        return llm.chat(
            system_prompt="You are a concise analyst. Write one sentence in Chinese summarizing the key developments.",
            user_prompt=f"Domain: {cat_name}\nTop events:\n{sample}\n\nOne-sentence Chinese summary of this week's key developments:",
        )
    except Exception:
        return ""


def generate_report(
    articles: list[dict],
    config: dict,
    week_label: str,
    llm: LLMClient | None = None,
    discovered_count: int = 0,
    archived_count: int = 0,
) -> str:
    """
    从 knowledge.db 读取 Events 并生成 HTML 周报。

    Args:
        articles: 当前 pipeline 产出的文章列表（用于兼容旧逻辑）
        config: 配置字典
        week_label: 周标签如 "2026-W33"
        llm: LLM 客户端（可选，用于趋势分析）
        discovered_count: 新发现源数
        archived_count: 废弃源数
    """
    # ── 1. 从 knowledge.db 读取数据 ──────────────────────
    data = _query_knowledge_db(week_label)
    events = data.get("events", [])
    articles_by_event = data.get("articles_by_event", {})
    sources = data.get("sources", {})

    # ── 2. 按领域分组 Events ─────────────────────────────
    categories_cfg = config["filter"]["categories"]
    category_names = [c["name"] for c in categories_cfg]
    categories: dict[str, list[dict]] = {name: [] for name in category_names}

    for evt in events:
        cat = evt.get("category", "LLM")
        if cat in categories:
            categories[cat].append(evt)
        else:
            # 尝试匹配（比如 "设计仿真" vs "Design Simulation"）
            matched = False
            for cat_name in categories:
                if cat_name in cat or cat in cat_name:
                    categories[cat_name].append(evt)
                    matched = True
                    break
            if not matched:
                categories.setdefault("LLM", []).append(evt)

    # ── 3. 空模块回填 ────────────────────────────────────
    empty_cats = [k for k, v in categories.items() if not v]
    if empty_cats:
        fallback = _load_fallback_events(week_label, empty_cats)
        for cat_name, fallback_events in fallback.items():
            categories[cat_name] = fallback_events

    # ── 4. 统计 ──────────────────────────────────────────
    stats = {k: len(v) for k, v in categories.items()}
    total_events = sum(stats.values())
    total_articles = sum(len(articles_by_event.get(e["id"], [])) for e in events)

    # 判断是否有回填
    has_carried = any(
        any(evt.get("carried_over") for evt in evts)
        for evts in categories.values()
    )

    colors = {
        "LLM": "#3b82f6", "Agent": "#8b5cf6",
        "AI for Science": "#10b981",
        "设计仿真": "#f59e0b", "数字孪生": "#ef4444"
    }
    icons = {
        "LLM": "🧠", "Agent": "🤖", "AI for Science": "🔬",
        "设计仿真": "🎨", "数字孪生": "🏭"
    }

    # ── 5. LLM 趋势 & 摘要 ───────────────────────────────
    trends = _get_trends(llm, events, categories_cfg) if llm else {k: "持续关注" for k in categories}

    domain_summaries = {}
    if llm:
        for cat_name, cat_events in categories.items():
            if cat_events:
                summaries = [
                    evt.get("event_summary", evt.get("summary", ""))[:200]
                    for evt in cat_events[:5]
                ]
                domain_summaries[cat_name] = _domain_summary(llm, cat_name, summaries)
            else:
                domain_summaries[cat_name] = ""
    else:
        domain_summaries = {k: "" for k in categories}

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    category_slugs = {
        "LLM": "llm", "Agent": "agent", "AI for Science": "ai-for-science",
        "设计仿真": "design-simulation", "数字孪生": "digital-twin",
    }

    # ── 6. 渲染 HTML ─────────────────────────────────────
    env = Environment(loader=FileSystemLoader("generator/templates"))

    week_dir = f"output/{week_label.replace(' ', '_')}"
    os.makedirs(week_dir, exist_ok=True)

    # --- Render index.html (homepage) ---
    tpl_index = env.get_template("index.html")
    html_index = tpl_index.render(
        title="AI 前沿资讯周报",
        subtitle=f"LLM · Agent · AI for Science · 设计仿真 · 数字孪生 — 共 {total_events} 个事件 / {total_articles} 篇资讯",
        week=week_label,
        stats=stats,
        categories=categories,
        icons=icons,
        colors=colors,
        trends=trends,
        empty_cats=empty_cats,
        has_carried=has_carried,
        domain_summaries=domain_summaries,
        category_slugs=category_slugs,
        generated_at=generated_at,
        discovered_count=discovered_count,
        archived_count=archived_count,
        articles_by_event=articles_by_event,
    )
    index_path = os.path.join(week_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html_index)

    # --- Render each category sub-page ---
    tpl_cat = env.get_template("category.html")
    for cat_name, cat_events in categories.items():
        cat_slug = category_slugs.get(cat_name, cat_name.lower().replace(" ", "-"))
        html_cat = tpl_cat.render(
            cat_name=cat_name,
            cat_icon=icons.get(cat_name, ""),
            cat_color=colors.get(cat_name, "#1677ff"),
            events=cat_events,
            event_count=len(cat_events),
            articles_by_event=articles_by_event,
            sources=sources,
            domain_summary=domain_summaries.get(cat_name, ""),
            week=week_label,
            generated_at=generated_at,
            title="AI 前沿资讯周报",
        )
        cat_path = os.path.join(week_dir, f"{cat_slug}.html")
        with open(cat_path, "w", encoding="utf-8") as f:
            f.write(html_cat)

    # 保存 articles.json 供兼容
    if articles:
        json_path = os.path.join(week_dir, "articles.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, default=str)

    logger.info(
        f"Report generated: {total_events} events, "
        f"{total_articles} articles → {index_path}"
    )
    return index_path
