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
import re
from collections import Counter
from datetime import datetime
from typing import Any

import sqlite3
from jinja2 import Environment, FileSystemLoader
from models.llm_client import LLMClient
from event_clustering import _clean_category

logger = logging.getLogger(__name__)


# ── 展示清洗 ──────────────────────────────────────────────
# RSS 摘要常带 "点击查看原文" 链接/HTML 残留，读库时统一清洗（事件+文章）
# - <...> HTML 标签；- 孤立尖括号；- 含 utm 的查询串（整个 ?... 段）；
# - 独立 utm_xxx=yyy 参数；- "点击查看原文"
_DIRTY_RE = re.compile(r"<[^>]+>|[<>]|\?\S*utm_\S*|utm_\w+=[^\s]*|点击?查看原文")


def _clean_text(s: str | None) -> str:
    return _DIRTY_RE.sub("", s or "").strip()


def _fmt_date(s: str | None) -> str:
    """published 可能是 ISO 或 RSC (Thu, 10 Sep 2026 ...) → 统一 YYYY-MM-DD。"""
    from email.utils import parsedate_to_datetime
    s = (s or "").strip()
    if not s:
        return ""
    if s[:4].isdigit() and s[4:5] in ("-", "T"):
        return s[:10]
    try:
        return parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return s[:10]


# ── 查询 ────────────────────────────────────────────────
def _prev_weeks(week_label: str, db_path: str = "data/knowledge.db", limit: int = 3) -> list[dict]:
    """过往周入口：DB 中早于当前周的周标签（新→旧，最多 limit 个），供首页 topbar 历史周报链接。"""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT DISTINCT week_label FROM events WHERE week_label < ? "
            "ORDER BY week_label DESC LIMIT ?",
            (week_label, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    # week_label 固定零填充格式（2026-W39），字典序=时间序
    return [{"label": r[0], "href": f"../{r[0].replace(' ', '_')}/"} for r in rows]


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
        ORDER BY final_score DESC, importance DESC, title ASC
        """,
        (week_label,),
    ).fetchall()

    events = []
    event_ids = []
    for row in events_rows:
        evt = dict(row)
        evt = {**json.loads(evt.get("metadata_json") or "{}"), **evt}
        from filter.scoring import compute_value_score
        evt["value_score"] = compute_value_score(evt)
        evt["summary"] = _clean_text(evt.get("summary"))
        events.append(evt)
        event_ids.append(evt["id"])

    # ── Articles linked to these events ─────────────────────
    articles_by_event = {}
    if event_ids:
        placeholders = ",".join(["?"] * len(event_ids))
        rows = conn.execute(
            f"SELECT ea.event_id AS linked_event_id, a.*, s.payload_json AS snapshot_json FROM articles a "
            f"JOIN event_articles ea ON a.url=ea.article_url JOIN events e ON e.id=ea.event_id "
            f"LEFT JOIN report_article_snapshots s ON s.article_url=a.url AND s.week_label=e.week_label "
            f"WHERE ea.event_id IN ({placeholders})",
            event_ids,
        ).fetchall()
        for row in rows:
            article = dict(row)
            eid = article.pop("linked_event_id")
            snapshot = article.pop("snapshot_json", None)
            if snapshot:
                article.update(json.loads(snapshot))
            article["summary"] = _clean_text(article.get("summary"))
            articles_by_event.setdefault(eid, []).append(article)

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


def _group_events_by_category(events: list[dict], category_names: list[str]) -> dict[str, list[dict]]:
    """按领域分组事件。

    - 已知类别（含别名归一）→ 对应桶
    - 未识别 / 空类别 → "其他" 桶，绝不静默混入 LLM
    - 每个配置类别名都保证存在（空则空列表，供回填判断）
    """
    categories: dict[str, list[dict]] = {name: [] for name in category_names}
    categories.setdefault("其他", [])
    uncategorized = 0
    for evt in events:
        cat = _clean_category(evt.get("category", ""))
        if cat in categories:
            categories[cat].append(evt)
        elif cat:
            matched = False
            for cat_name in categories:
                if cat_name in cat or cat in cat_name:
                    categories[cat_name].append(evt)
                    matched = True
                    break
            if not matched:
                logger.warning(
                    f"Unknown event category {cat!r} → 其他: {evt.get('event_title', '')[:40]}"
                )
                categories["其他"].append(evt)
        else:
            uncategorized += 1
            categories["其他"].append(evt)
    if uncategorized:
        logger.warning(f"{uncategorized} events 无类别 → 其他")
    return categories


def make_event_slugs(all_events: list[dict]) -> dict[str, str]:
    """事件 slug：evt-<id 尾 8 位>-<标题拉丁词>，供事件详情页文件名。"""
    def _slug(evt: dict) -> str:
        m = re.findall(r"[A-Za-z0-9]+", evt.get("title", ""))
        return f"evt-{evt['id'][-8:]}-{'-'.join(m[:6]).lower() or 'event'}"
    return {e["id"]: _slug(e) for e in all_events}


def generate_report(
    articles: list[dict],
    config: dict,
    week_label: str,
    llm: LLMClient | None = None,
    discovered_count: int = 0,
    archived_count: int = 0,
    trends: dict[str, str] | None = None,
    domain_summaries: dict[str, str] | None = None,
    generated_at: str | None = None,
    db_path: str = "data/knowledge.db",
    analysis: dict | None = None,
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
        trends: 现成趋势文案（跳过 LLM，用于重渲旧周保留原文案）
        domain_summaries: 现成领域摘要（同上）
        generated_at: 现成时间戳（重渲时保留原日期）
    """
    # ── 1. 从 knowledge.db 读取数据 ──────────────────────
    data = _query_knowledge_db(week_label, db_path)
    events = data.get("events", [])
    articles_by_event = data.get("articles_by_event", {})
    sources = data.get("sources", {})

    # ── 2. 按领域分组 Events ─────────────────────────────
    categories_cfg = config["filter"]["categories"]
    category_names = [c["name"] for c in categories_cfg]
    categories = _group_events_by_category(events, category_names)

    # ── 3. 空模块回填 ────────────────────────────────────
    empty_cats = [k for k, v in categories.items() if not v and k != "其他"]
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
        "设计仿真": "#f59e0b", "数字孪生": "#ef4444",
        "其他": "#94a3b8"
    }
    icons = {
        "LLM": "🧠", "Agent": "🤖", "AI for Science": "🔬",
        "设计仿真": "🎨", "数字孪生": "🏭"
    }

    from generator.research_observations import synthesize
    analysis = analysis if analysis is not None else synthesize(llm, events)
    research_observations = analysis["observations"]
    domain_signals = {item["category"]: item for item in analysis["domain_signals"]}
    trends = {name: domain_signals.get(name, {}).get("title", "本周证据不足") for name in categories}
    domain_summaries = {name: domain_signals.get(name, {}).get("text", "本周暂无足够证据支持领域判断。") for name in categories}
    prev_weeks = _prev_weeks(week_label, db_path)

    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    category_slugs = {
        "LLM": "llm", "Agent": "agent", "AI for Science": "ai-for-science",
        "设计仿真": "design-simulation", "数字孪生": "digital-twin",
    }

    # 本周全部事件（扁平化 + 分类名，按重要性排序）：供首页信息流；Top5 精选取前 5
    all_events = []
    for cat_name, cat_events in categories.items():
        for evt in cat_events:
            item = dict(evt)
            item["cat"] = cat_name
            all_events.append(item)
    all_events.sort(key=lambda e: (-(e.get("final_score") or 0), -(e.get("importance") or 0), e.get("title", "")))
    top_events = [e for e in all_events if e.get("top_eligible") and e.get("quality_status") == "passed"][:5]

    # 事件 slug 与证据链：Event → Articles → Sources
    event_slugs = make_event_slugs(all_events)
    cited_sources = dict(Counter(
        a.get("source_id", "")
        for arts in articles_by_event.values() for a in arts
    ))

    from filter.quality import publisher
    publisher_count = len({publisher(a) for arts in articles_by_event.values() for a in arts})
    active_domain_count = sum(bool(v) for k, v in categories.items() if k != "其他")

    # ── 6. 渲染 HTML ─────────────────────────────
    env = Environment(loader=FileSystemLoader("generator/templates"), autoescape=True)
    env.filters["clean_summary"] = _clean_text  # 模板层双保险
    env.filters["fmt_date"] = _fmt_date

    week_dir = os.path.join(config.get("output", {}).get("dir", "output"), week_label.replace(" ", "_"))
    os.makedirs(week_dir, exist_ok=True)
    from pathlib import Path
    (Path(week_dir) / "research_observations.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2))

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
        top_events=top_events,
        research_observations=research_observations, domain_signals=domain_signals,
        all_events=all_events,
        prev_weeks=prev_weeks,
        generated_at=generated_at,
        discovered_count=discovered_count,
        archived_count=archived_count,
        articles_by_event=articles_by_event,
        total_events=total_events,
        publisher_count=publisher_count, active_domain_count=active_domain_count,
        total_articles=total_articles,
        event_slugs=event_slugs,
        cited_sources=cited_sources,
    )
    index_path = os.path.join(week_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(line.rstrip() for line in html_index.splitlines()) + "\n")

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
            categories=categories, stats=stats, this_cat=cat_name,
            icons=icons, colors=colors, category_slugs=category_slugs,
            trends=trends, discovered_count=discovered_count,
            archived_count=archived_count,
            event_slugs=event_slugs,
            generated_at=generated_at,
            title="AI 前沿资讯周报",
        )
        cat_path = os.path.join(week_dir, f"{cat_slug}.html")
        with open(cat_path, "w", encoding="utf-8") as f:
            f.write("\n".join(line.rstrip() for line in html_cat.splitlines()) + "\n")

    # --- Render event detail pages (Evidence Chain) ---
    tpl_evt = env.get_template("event.html")
    events_dir = os.path.join(week_dir, "events")
    os.makedirs(events_dir, exist_ok=True)
    for evt in all_events:
        evt_arts = articles_by_event.get(evt["id"], [])
        # Evidence Chain order: LLM Top-3 first, remaining keep DB order
        top_urls = []
        raw_top = evt.get("top_article_urls")
        if raw_top:
            try:
                top_urls = json.loads(raw_top) if isinstance(raw_top, str) else list(raw_top)
            except (ValueError, TypeError):
                top_urls = []
        if top_urls:
            rank = {u: i for i, u in enumerate(top_urls)}
            # stable sort: Top-3 float to front, remainder keeps DB order
            evt_arts = sorted(
                evt_arts, key=lambda a: rank.get(a.get("url", ""), len(rank))
            )
        from filter.scoring import _source_quality_score
        from filter.quality import publisher
        evidence_by_url = {e["url"]: e for e in evt.get("evidence", [])}
        evt_arts = [{**a, "publisher": publisher(a), "publisher_quality": _source_quality_score([publisher(a)]), "evidence": evidence_by_url.get(a["url"], {})} for a in evt_arts]
        evt_srcs = dict(Counter(
            a.get("source_id", "") for a in evt_arts
        ))
        html_evt = tpl_evt.render(
            ev=evt,
            cat_name=evt["cat"],
            cat_color=colors.get(evt["cat"], "#94a3b8"),
            articles=evt_arts,
            sources=sources,
            cited_sources=evt_srcs,
            domain_summary=domain_summaries.get(evt["cat"], ""),
            week=week_label,
            generated_at=generated_at,
            title="AI 前沿资讯周报",
        )
        evt_path = os.path.join(events_dir, f"{event_slugs[evt['id']]}.html")
        with open(evt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(line.rstrip() for line in html_evt.splitlines()) + "\n")

    # 保存 articles.json 供兼容
    if articles:
        json_path = os.path.join(week_dir, "articles.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, default=str)

    logger.info(
        f"Report generated: {total_events} events, "
        f"{total_articles} articles → {index_path}"
    )
    expected = {slug + ".html" for slug in event_slugs.values()}
    for stale in Path(events_dir).glob("*.html"):
        if stale.name not in expected:
            stale.unlink()
    return index_path
