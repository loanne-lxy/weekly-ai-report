"""预览渲染：用真实 knowledge.db 数据渲染新模板到 /tmp，不碰 output/。"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from jinja2 import Environment, FileSystemLoader
import yaml
from generator.report_generator import (
    _query_knowledge_db, _group_events_by_category, _load_fallback_events, _clean_text,
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
    "LLM": "#2563eb", "Agent": "#7c3aed",
    "AI for Science": "#059669",
    "设计仿真": "#d97706", "数字孪生": "#dc2626"
}
def _ic(paths):
    return ('<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')
icons = {
    "LLM": _ic('<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>'),
    "Agent": _ic('<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><path d="M8 16h.01M16 16h.01"/>'),
    "AI for Science": _ic('<path d="M10 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8V2"/><path d="M8.5 2h7"/><path d="M7 15h10"/>'),
    "设计仿真": _ic('<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/>'),
    "数字孪生": _ic('<path d="M2 20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8l-7 5V8l-7 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M17 18h1M12 18h1M7 18h1"/>'),
    "其他": _ic('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
}

trends = {k: "持续关注" for k in categories}
domain_summaries = {}
for k, evts in categories.items():
    if evts:
        _s = evts[0].get("summary", "暂无摘要")
        domain_summaries[k] = _s[:200] + ("…" if len(_s) > 200 else "")
    else:
        domain_summaries[k] = ""

category_slugs = {"LLM": "llm", "Agent": "agent", "AI for Science": "ai-for-science",
                  "设计仿真": "design-simulation", "数字孪生": "digital-twin"}
generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

env = Environment(loader=FileSystemLoader("generator/templates"))
env.filters["clean_summary"] = _clean_text

html_index = env.get_template("index.html").render(
    title="AI 前沿资讯周报",
    subtitle=f"LLM · Agent · AI for Science · 设计仿真 · 数字孪生 — 共 {total_events} 个事件 / {total_articles} 篇资讯",
    week=WEEK, stats=stats, categories=categories, icons=icons, colors=colors,
    trends=trends, empty_cats=empty_cats, has_carried=False,
    domain_summaries=domain_summaries, category_slugs=category_slugs,
    generated_at=generated_at, discovered_count=2, archived_count=1,
    articles_by_event=articles_by_event,
)
open(f"{OUT}/index.html", "w", encoding="utf-8").write(html_index)

tpl_cat = env.get_template("category.html")
for cat_name, cat_events in categories.items():
    cat_slug = category_slugs.get(cat_name, cat_name.lower().replace(" ", "-"))
    html_cat = tpl_cat.render(
        cat_name=cat_name, cat_icon=icons.get(cat_name, ""),
        cat_color=colors.get(cat_name, "#2563eb"),
        events=cat_events, event_count=len(cat_events),
        articles_by_event=articles_by_event, sources=data["sources"],
        domain_summary=domain_summaries.get(cat_name, ""), week=WEEK,
        generated_at=generated_at, title="AI 前沿资讯周报",
    )
    open(f"{OUT}/{cat_slug}.html", "w", encoding="utf-8").write(html_cat)

print(f"OK: {OUT} ({total_events} events / {total_articles} articles, week={WEEK})")
print("files:", sorted(os.listdir(OUT)))