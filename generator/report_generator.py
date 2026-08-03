"""报告生成 — Jinja2 渲染 HTML 周报 + LLM 趋势分析 + 空模块回填"""
import os
import json
import glob
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


def _load_last_week_articles() -> dict[str, list[dict]]:
    """读取上周 articles.json，返回按领域分组的文章字典"""
    pattern = "output/*/articles.json"
    files = sorted(glob.glob(pattern), reverse=True)
    if len(files) < 2:
        return {}
    last_json = files[1]  # 倒数第二周
    try:
        with open(last_json) as f:
            all_arts = json.load(f)
    except Exception:
        return {}

    result: dict[str, list[dict]] = {}
    for a in all_arts:
        cat = a.get("category", "")
        if cat not in result:
            result[cat] = []
        result[cat].append(a)
    return result


def generate_report(articles: list[dict], config: dict, week_label: str, llm: LLMClient = None) -> str:
    categories: dict[str, list] = {}
    for cat_cfg in config["filter"]["categories"]:
        categories[cat_cfg["name"]] = []

    for a in articles:
        cat = a.get("category", "LLM")
        if cat in categories:
            categories[cat].append(a)

    # 空模块回填——从上周报告拉取内容
    empty_cats = [k for k, v in categories.items() if not v]
    if empty_cats:
        last_week = _load_last_week_articles()
        for cat_name in empty_cats:
            if cat_name in last_week and last_week[cat_name]:
                for a in last_week[cat_name]:
                    a["carried_over"] = True
                categories[cat_name] = last_week[cat_name]

    # Sort each category by importance desc
    for cat_name in categories:
        categories[cat_name].sort(key=lambda a: a.get("importance", 5), reverse=True)

    stats = {k: len(v) for k, v in categories.items()}
    total = sum(stats.values())
    has_carried = any(
        any(a.get("carried_over") for a in cats)
        for cats in categories.values()
    )

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
        empty_cats=empty_cats,
        has_carried=has_carried,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    week_dir = f"output/{week_label.replace(' ', '_')}"
    os.makedirs(week_dir, exist_ok=True)
    filepath = os.path.join(week_dir, "index.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    # 保存 articles.json 供下周回填
    json_path = os.path.join(week_dir, "articles.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, default=str)

    return filepath
