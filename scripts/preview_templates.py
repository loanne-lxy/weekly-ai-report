"""预览渲染：用真实 knowledge.db 数据渲染新模板到 /tmp，不碰 output/。"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from collections import Counter
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
import yaml
from generator.report_generator import (
    _query_knowledge_db, _group_events_by_category, _load_fallback_events, _clean_text,
    _fmt_date, make_event_slugs,
)

WEEK = "2026-W38"
OUT = "/tmp/war-preview"
os.makedirs(OUT, exist_ok=True)

cfg = yaml.safe_load(open("config.yaml"))
category_names = [c["name"] for c in cfg["filter"]["categories"]]

data = _query_knowledge_db(WEEK)
events = data["events"]
articles_by_event = data["articles_by_event"]

categories = _group_events_by_category(events, category_names)
empty_cats = [k for k, v in categories.items() if not v and k != "其他"]
fallback = _load_fallback_events(WEEK, empty_cats) if empty_cats else {}
for k, v in fallback.items():
    categories[k] = v

stats = {k: len(v) for k, v in categories.items()}
total_events = sum(stats.values())
total_articles = sum(len(articles_by_event.get(e["id"], [])) for e in events)

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

trends = {k: "持续关注" for k in categories}

# all events (flat + cat, sorted by importance); top5 = first 5
_top = []
for _k, _evts in categories.items():
    for _e in _evts:
        _i = dict(_e); _i["cat"] = _k; _top.append(_i)
_top.sort(key=lambda e: (e.get("importance") or 0), reverse=True)
all_events = _top
top_events = _top[:5]
domain_summaries = {}
for k, evts in categories.items():
    if evts:
        _s = evts[0].get("summary", "暂无摘要")
        domain_summaries[k] = _s[:200] + ("…" if len(_s) > 200 else "")
    else:
        domain_summaries[k] = ""

category_slugs = {"LLM": "llm", "Agent": "agent", "AI for Science": "ai-for-science",
                  "设计仿真": "design-simulation", "数字孪生": "digital-twin"}
event_slugs = make_event_slugs(all_events)
cited_sources = dict(Counter(
    a.get("source_id", "") for arts in articles_by_event.values() for a in arts
))
generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

env = Environment(loader=FileSystemLoader("generator/templates"))
env.filters["clean_summary"] = _clean_text
env.filters["fmt_date"] = _fmt_date

html_index = env.get_template("index.html").render(
    title="AI 前沿资讯周报",
    subtitle=f"LLM · Agent · AI for Science · 设计仿真 · 数字孪生 — 共 {total_events} 个事件 / {total_articles} 篇资讯",
    week=WEEK, stats=stats, categories=categories, icons=icons, colors=colors,
    trends=trends, top_events=top_events, all_events=all_events, empty_cats=empty_cats, has_carried=False,
    domain_summaries=domain_summaries, category_slugs=category_slugs,
    generated_at=generated_at, discovered_count=2, archived_count=1,
    articles_by_event=articles_by_event, total_events=total_events,
    total_articles=total_articles, event_slugs=event_slugs, cited_sources=cited_sources,
)
open(f"{OUT}/index.html", "w", encoding="utf-8").write(html_index)

tpl_cat = env.get_template("category.html")
for cat_name, cat_events in categories.items():
    cat_slug = category_slugs.get(cat_name, cat_name.lower().replace(" ", "-"))
    html_cat = tpl_cat.render(
        cat_name=cat_name, cat_icon=icons.get(cat_name, ""),
        cat_color=colors.get(cat_name, "#1a5276"),
        events=cat_events, event_count=len(cat_events),
        articles_by_event=articles_by_event, sources=data["sources"],
        domain_summary=domain_summaries.get(cat_name, ""), week=WEEK,
        categories=categories, stats=stats, this_cat=cat_name,
        icons=icons, colors=colors, category_slugs=category_slugs,
        trends=trends, discovered_count=2, archived_count=1,
        event_slugs=event_slugs,
        generated_at=generated_at, title="AI 前沿资讯周报",
    )
    open(f"{OUT}/{cat_slug}.html", "w", encoding="utf-8").write(html_cat)

# event detail pages
tpl_evt = env.get_template("event.html")
os.makedirs(f"{OUT}/events", exist_ok=True)
for evt in all_events:
    evt_arts = articles_by_event.get(evt["id"], [])
    evt_srcs = dict(Counter(a.get("source_id", "") for a in evt_arts))
    html_evt = tpl_evt.render(
        ev=evt, cat_name=evt["cat"], cat_color=colors.get(evt["cat"], "#94a3b8"),
        articles=evt_arts, sources=data["sources"], cited_sources=evt_srcs,
        domain_summary=domain_summaries.get(evt["cat"], ""), week=WEEK,
        generated_at=generated_at, title="AI 前沿资讯周报",
    )
    open(f"{OUT}/events/{event_slugs[evt['id']]}.html", "w", encoding="utf-8").write(html_evt)

print(f"OK: {OUT} ({total_events} events / {total_articles} articles, week={WEEK})")
print("files:", sorted(os.listdir(OUT)))