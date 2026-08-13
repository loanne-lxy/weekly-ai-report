"""报告生成 — Jinja2 渲染 HTML 周报 + LLM 趋势分析 + 空模块回填"""
import os
import json
import glob
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from models.llm_client import LLMClient


def _get_trends(llm: LLMClient, articles: list[dict], categories: list[dict]) -> dict[str, str]:
    """LLM 生成各领域本周趋势关键词（失败安全回退）"""
    cats: dict[str, list] = {}
    for cat_cfg in categories:
        cats[cat_cfg["name"]] = []

    for a in articles:
        cat = a.get("category", "")
        if cat in cats:
            # Use AI summary for richer context
            text = a.get("ai_summary", "") or a.get("title", "")
            cats[cat].append(text[:200])

    trends = {}
    for cat_name, texts in cats.items():
        if not texts:
            trends[cat_name] = "无相关资讯"
            continue
        sample = "\n".join(f"- {t}" for t in texts[:5])
        try:
            keyword = llm.chat(
                system_prompt=(
                    "You are an AI trend analyst. Based on article summaries, identify the single most "
                    "prominent technology trend or theme in simple Chinese (2-6 characters). "
                    "Good examples: 多模态融合, MoE架构, Agent自主性, 端侧推理, 开源生态, "
                    "蛋白质设计, 物理AI, 实时孪生. "
                    "Bad examples: 技术发展, 行业动态, AI应用. "
                    "Return ONLY the keyword, no explanation."
                ),
                user_prompt=(
                    f"Domain: {cat_name}\n"
                    f"Following are this week's top article summaries. Identify the most prominent trend:\n\n"
                    f"{sample}\n\n"
                    f"Trend keyword (2-6 Chinese characters only):"
                ),
            )
            cleaned = keyword.strip().replace("。", "").replace("，", "").replace("：", "").replace(":", "")
            trends[cat_name] = cleaned[:8]
        except Exception:
            trends[cat_name] = "持续关注"
    return trends


def _domain_summary(llm: LLMClient, cat_name: str, titles: list[str]) -> str:
    """Generate a one-paragraph summary for a domain section"""
    sample = "\n".join(f"- {t}" for t in titles[:5])
    try:
        return llm.chat(
            system_prompt="You are a concise analyst. Write one sentence in Chinese summarizing the key developments.",
            user_prompt=f"Domain: {cat_name}\nTop articles:\n{sample}\n\nOne-sentence Chinese summary of this week's key developments:",
        )
    except Exception:
        return ""


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


def _find_fallback_articles(empty_cats: list[str]) -> dict[str, list[dict]]:
    """从历史周报查找空领域的回填文章，最多回溯4周"""
    pattern = "output/*/articles.json"
    files = sorted(glob.glob(pattern), reverse=True)
    if len(files) < 2:
        return {}

    remaining = set(empty_cats)
    fallback: dict[str, list[dict]] = {}

    for json_file in files[1:]:
        if not remaining:
            break
        try:
            with open(json_file) as f:
                all_arts = json.load(f)
        except Exception:
            continue
        for a in all_arts:
            cat = a.get("category", "")
            if cat in remaining and cat not in fallback:
                fallback[cat] = []
        for a in all_arts:
            cat = a.get("category", "")
            if cat in fallback:
                fallback[cat].append(a)
                remaining.discard(cat)

    return fallback


def generate_report(
    articles: list[dict], config: dict, week_label: str, llm: LLMClient = None,
    discovered_count: int = 0,
    archived_count: int = 0,
) -> str:
    categories: dict[str, list] = {}
    for cat_cfg in config["filter"]["categories"]:
        categories[cat_cfg["name"]] = []

    for a in articles:
        cat = a.get("category", "LLM")
        if cat in categories:
            categories[cat].append(a)

    # 空模块回填——回溯历史周报最多4周
    empty_cats = [k for k, v in categories.items() if not v]
    if empty_cats:
        fallback = _find_fallback_articles(empty_cats)
        for cat_name, arts in fallback.items():
            for a in arts:
                a["carried_over"] = True
            categories[cat_name] = arts

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

    trends = _get_trends(llm, articles, config["filter"]["categories"]) if llm else {k: "持续关注" for k in categories}

    # Generate per-domain summaries
    domain_summaries = {}
    if llm:
        for cat_name, arts in categories.items():
            if arts:
                titles = [a.get("chinese_title", a.get("title", "")) for a in arts[:5]]
                domain_summaries[cat_name] = _domain_summary(llm, cat_name, titles)
            else:
                domain_summaries[cat_name] = ""
    else:
        domain_summaries = {k: "" for k in categories}

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    category_slugs = {
        "LLM": "llm", "Agent": "agent", "AI for Science": "ai-for-science",
        "设计仿真": "design-simulation", "数字孪生": "digital-twin",
    }

    env = Environment(loader=FileSystemLoader("generator/templates"))

    week_dir = f"output/{week_label.replace(' ', '_')}"
    os.makedirs(week_dir, exist_ok=True)

    # --- Render index.html (homepage) ---
    tpl_index = env.get_template("index.html")
    html_index = tpl_index.render(
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
        domain_summaries=domain_summaries,
        category_slugs=category_slugs,
        generated_at=generated_at,
        discovered_count=discovered_count,
        archived_count=archived_count,
    )
    index_path = os.path.join(week_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html_index)

    # --- Render each category sub-page ---
    tpl_cat = env.get_template("category.html")
    for cat_name, cat_articles in categories.items():
        cat_count = len(cat_articles)
        cat_slug = category_slugs.get(cat_name, cat_name.lower().replace(" ", "-"))
        html_cat = tpl_cat.render(
            cat_name=cat_name,
            cat_icon=icons.get(cat_name, ""),
            cat_color=colors.get(cat_name, "#1677ff"),
            articles=cat_articles,
            article_count=cat_count,
            domain_summary=domain_summaries.get(cat_name, ""),
            week=week_label,
            generated_at=generated_at,
            title="AI 前沿资讯周报",
        )
        cat_path = os.path.join(week_dir, f"{cat_slug}.html")
        with open(cat_path, "w", encoding="utf-8") as f:
            f.write(html_cat)

    # 保存 articles.json 供下周回填
    json_path = os.path.join(week_dir, "articles.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, default=str)

    return index_path
