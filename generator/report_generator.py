"""报告生成 — Jinja2 渲染 HTML 周报"""
import os
from datetime import datetime
from jinja2 import Environment, FileSystemLoader


def generate_report(articles: list[dict], config: dict, week_label: str) -> str:
    """生成周报 HTML，返回文件路径"""
    # 按领域分组
    categories: dict[str, list] = {}
    for cat_cfg in config["filter"]["categories"]:
        categories[cat_cfg["name"]] = []

    for a in articles:
        cat = a.get("category", "LLM")
        if cat in categories:
            categories[cat].append(a)

    # 统计
    stats = {k: len(v) for k, v in categories.items()}
    total = sum(stats.values())

    icons = {
        "LLM": "🧠", "Agent": "🤖", "AI4Science": "🔬",
        "设计仿真": "🎨", "数字孪生": "🏭",
    }

    # 渲染
    env = Environment(loader=FileSystemLoader("generator/templates"))
    template = env.get_template("weekly.html")
    html = template.render(
        title=f"AI 前沿资讯周报",
        subtitle=f"LLM · Agent · AI4Science · 设计仿真 · 数字孪生 — 共 {total} 篇",
        week=week_label,
        stats=stats,
        categories=categories,
        icons=icons,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    # 写入文件
    week_dir = f"output/{week_label.replace(' ', '_')}"
    os.makedirs(week_dir, exist_ok=True)
    filepath = os.path.join(week_dir, "index.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath
