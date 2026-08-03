"""报告生成 — Jinja2 渲染 HTML 周报 + LLM 趋势分析"""
import os
import json
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from models.llm_client import LLMClient


def _get_trends(llm: LLMClient, articles: list[dict]) -> dict[str, str]:
    cats = {"LLM": [], "Agent": [], "AI for Science": [], "设计仿真": [], "数字孪生": []}
    for a in articles:
        cat = a.get("category", "LLM")
        if cat in cats:
            cats[cat].append(a.get("title", "")[:80])

    trends = {}
    for cat_name, titles in cats.items():
        if not titles:
            trends[cat_name] = "持续关注"
            continue
        sample = "\n".join(titles[:6])
        keyword = llm.chat(
            system_prompt="你是AI趋势分析师。用2-4字中文关键词概括趋势。",
            user_prompt=f"基于以下{cat_name}领域文章标题，用一个中文关键词（2-4字）概括本周趋势:\n{sample}\n关键词:",
        )
        trends[cat_name] = keyword.strip().replace("。", "").replace("，", "")[:6]

    return trends


def generate_report(articles: list[dict], config: dict, week_label: str, llm: LLMClient = None) -> str:
    categories: dict[str, list] = {}
    for cat_cfg in config["filter"]["categories"]:
        categories[cat_cfg["name"]] = []

    for a in articles:
        cat = a.get("category", "LLM")
        if cat in categories:
            categories[cat].append(a)

    # Sort each category by importance desc
    for cat_name in categories:
        categories[cat_name].sort(key=lambda a: a.get("importance", 5), reverse=True)

    stats = {k: len(v) for k, v in categories.items()}
    total = sum(stats.values())
    colors = {"LLM": "#3b82f6", "Agent": "#8b5cf6", "AI for Science": "#10b981", "设计仿真": "#f59e0b", "数字孪生": "#ef4444"}
    icons = {"LLM": "🧠", "Agent": "🤖", "AI for Science": "🔬", "设计仿真": "🎨", "数字孪生": "🏭"}

    trends = _get_trends(llm, articles) if llm else {k: "持续关注" for k in categories}

    env = Environment(loader=FileSystemLoader("generator/templates"))
    template = env.get_template("weekly.html")
    html = template.render(
        title="AI 前沿资讯周报",
        subtitle=f"LLM · Agent · AI for Science · 设计仿真 · 数字孪生 — 共 {total} 篇",
        week=week_label,
        stats=stats,
        categories=categories,
        icons=icons,
        colors=colors,
        trends=trends,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    week_dir = f"output/{week_label.replace(' ', '_')}"
    os.makedirs(week_dir, exist_ok=True)
    filepath = os.path.join(week_dir, "index.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath
