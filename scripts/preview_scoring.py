"""Re-score frozen groups into an isolated preview; never mutate the source DB."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import shutil
import statistics
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from filter.quality import parse_date
from filter.scoring import score_event, compute_value_score
from generator.report_generator import _query_knowledge_db, generate_report, make_event_slugs
from models.llm_client import LLMClient

DIMENSIONS = ("importance", "novelty", "impact")


def validate_scores(result, articles):
    norm = lambda text: re.sub(r"\s+", "", text).casefold()
    for name in DIMENSIONS:
        value = result[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid score: " + name)
        if not isinstance(result.get(name + "_rationale"), str) or len(result[name + "_rationale"].strip()) < 12:
            raise ValueError("Missing substantive rationale: " + name)
        evidence = result["score_evidence"][name]
        index = evidence["article_index"]
        if type(index) is not int or not 0 <= index < len(articles):
            raise ValueError("Invalid evidence member")
        quote = evidence["quote"]
        if not isinstance(quote, str) or len(quote) < 12 or norm(quote) not in norm(articles[index]["title"] + " " + articles[index]["text"]):
            raise ValueError("Ungrounded score evidence")
    return {key: result[key] for key in (*DIMENSIONS, *(d + "_rationale" for d in DIMENSIONS), "score_evidence")}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--db", default="data/knowledge.db")
    parser.add_argument("--run-dir", type=Path, default=Path("data/runs/2026-W44"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/scoring-preview-W44"))
    args = parser.parse_args()
    audit = json.loads((args.run_dir / "audit.json").read_text())
    week = audit["week"]
    cutoff = parse_date(audit["window_end"])
    if cutoff is None:
        raise ValueError("Missing frozen cutoff")
    out = args.output_dir.resolve()
    source = Path(args.db).resolve()
    if source == out / "knowledge.db" or out == Path("output").resolve() or Path("output").resolve() in out.parents:
        raise ValueError("Preview must be isolated from production")
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "score-cache"
    cache.mkdir(exist_ok=True)
    data = _query_knowledge_db(week, str(source))
    events = data["events"]
    rubric = Path("references/scoring_rubric.md").read_text()
    prompt = rubric + """\nRe-score this EXISTING group only. Do not regroup, reject, rename or rewrite its summary.
Return one JSON object with importance, novelty, impact; a Chinese rationale for each in
importance_rationale, novelty_rationale, impact_rationale (40-100 Chinese characters each).
Also provide score_evidence:{importance:{article_index,quote},novelty:{article_index,quote},impact:{article_index,quote}}.
article_index is local to supplied articles; quote is a verbatim 12-100 character excerpt.
A quote supports the reason, not necessarily a measured benefit. Clearly explain missing comparative evidence.
Only JSON, no markdown. Do not aim for a prescribed score average.
"""
    config = yaml.safe_load(Path("config.yaml").read_text())
    llm = LLMClient(config)

    def rescore(event):
        arts = data["articles_by_event"][event["id"]]
        relevance = {e["url"]: e["relevance"] for e in event["evidence"]}
        core = sorted(arts, key=lambda a: -relevance.get(a["url"], 0))[:3]
        payload = {"title": event["title"], "grouping_type": event.get("grouping_type"),
                   "research_question": event.get("research_question"),
                   "articles": [{"title": a["title"], "text": a["summary"][:1400]} for a in core]}
        encoded = json.dumps(payload, ensure_ascii=False)
        digest = hashlib.sha256((llm.model + prompt + encoded).encode()).hexdigest()
        path = cache / (digest + ".json")
        result = None
        if path.exists():
            result = validate_scores(json.loads(path.read_text()), payload["articles"])
        else:
            feedback = ""
            for attempt in range(3):
                try:
                    raw = llm.chat(prompt, encoded + feedback)
                    result = validate_scores(json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())), payload["articles"])
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                    break
                except (ValueError, KeyError, TypeError) as exc:
                    feedback = "\nPrevious response invalid: " + str(exc) + ". Return complete JSON with exact quotes."
            if result is None:
                raise ValueError("Scoring failed for " + event["title"])
        result["evidence_relevance"] = min(relevance.values())
        scored = score_event(arts, result, category=event["category"], now=cutoff)
        scored["score_evidence"] = {name: {**result["score_evidence"][name], "url": core[result["score_evidence"][name]["article_index"]]["url"]} for name in DIMENSIONS}
        scored["scoring_rubric"] = "value-v2"
        return event["id"], scored

    updated = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(rescore, e) for e in events]
        for future in as_completed(futures):
            eid, scores = future.result()
            updated[eid] = scores
            print(f"Validated scores {len(updated)}/{len(events)}", flush=True)
    assert len(updated) == len(events)
    with sqlite3.connect(source) as original, sqlite3.connect(out / "knowledge.db") as preview:
        original.backup(preview)
        for event in events:
            new = updated[event["id"]]
            raw = preview.execute("SELECT metadata_json FROM events WHERE id=?", (event["id"],)).fetchone()[0]
            metadata = {**json.loads(raw or "{}"), **new}
            preview.execute("UPDATE events SET importance=?,novelty=?,impact=?,final_score=?,metadata_json=? WHERE id=?",
                (new["importance"], new["novelty"], new["impact"], new["final_score"], json.dumps(metadata, ensure_ascii=False), event["id"]))
        for table in ("articles", "event_articles", "report_article_snapshots", "report_events"):
            assert original.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall() == preview.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall(), table
        assert original.execute("SELECT id,title,summary,category,quality_status,top_eligible FROM events ORDER BY id").fetchall() == preview.execute("SELECT id,title,summary,category,quality_status,top_eligible FROM events ORDER BY id").fetchall()
        preview.commit()
    analysis = json.loads((Path("output") / week / "research_observations.json").read_text())
    config["output"]["dir"] = str(out / "site")
    generate_report([], config, week, db_path=str(out / "knowledge.db"), analysis=analysis)
    # Preserve the preview's existing history navigation without changing production.
    homepage = (out / "site" / week / "index.html").read_text()
    for previous in set(re.findall(r"\.\./(\d{4}-W\d{2})/", homepage)):
        historical = Path("output") / previous
        if historical.is_dir():
            shutil.copytree(historical, out / "site" / previous, dirs_exist_ok=True)
    rows = [{"id": e["id"], "title": e["title"], "category": e["category"], "top_eligible": bool(e["top_eligible"]),
             "old_display": e["final_score"] * 100, "old_value": compute_value_score(e) * 100,
             "new_value": updated[e["id"]]["value_score"] * 100, "new_ranking": updated[e["id"]]["final_score"] * 100,
             "old_dimensions": {d: e[d] for d in DIMENSIONS}, "new_scores": updated[e["id"]]} for e in events]
    stats = {key: {"mean": round(statistics.mean(r[key] for r in rows), 1), "median": round(statistics.median(r[key] for r in rows), 1),
                   "min": round(min(r[key] for r in rows), 1), "max": round(max(r[key] for r in rows), 1),
                   "bands": {label: sum(lo <= r[key] < hi for r in rows) for label, lo, hi in [("0-39",0,40),("40-59",40,60),("60-79",60,80),("80-100",80,101)]}}
             for key in ("old_display", "old_value", "new_value", "new_ranking")}
    eligible = [r for r in rows if r["top_eligible"]]
    top = lambda key: [r["title"] for r in sorted(eligible, key=lambda r: (-r[key], -r["new_scores"]["importance"] if key == "new_ranking" else -r["old_dimensions"]["importance"], r["title"]))[:10]]
    result = {"week": week, "events": len(rows), "articles": sum(len(v) for v in data["articles_by_event"].values()), "stats": stats,
              "old_top10": top("old_display"), "new_top10": top("new_ranking"), "rows": rows}
    (out / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    esc = html.escape
    slugs = make_event_slugs(events)
    header = "<tr><th>事件</th><th>原页面分</th><th>原价值分</th><th>新价值分</th><th>新排序分</th></tr>"
    body = "".join("<tr><td><a href=\"site/" + week + "/events/" + slugs[r["id"]] + ".html\">" + esc(r["title"]) + "</a></td>" + "".join(f"<td>{r[k]:.1f}</td>" for k in ("old_display","old_value","new_value","new_ranking")) + "</tr>" for r in sorted(rows,key=lambda r:-r["new_value"]))
    top_html = "".join("<section><h2>"+label+"</h2><ol>"+"".join("<li>"+esc(t)+"</li>" for t in result[key])+"</ol></section>" for label,key in [("原推荐前十","old_top10"),("新推荐前十","new_top10")])
    stats_html = "".join(f"<tr><td>{label}</td><td>{stats[k]['mean']}</td><td>{stats[k]['median']}</td><td>{stats[k]['min']}–{stats[k]['max']}</td></tr>" for k,label in [("old_display","原页面分"),("old_value","原价值分"),("new_value","新价值分"),("new_ranking","新排序分")])
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>W44 评分对照</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;color:#202124;max-width:1100px;margin:40px auto;padding:0 20px}}a{{color:#1967d2}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#f8f9fa}}.tops{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:32px}}h1{{font-size:30px}}td:not(:first-child){{white-space:nowrap}}</style>
<h1>W44 评分对照 · 本地预览</h1><p>{len(rows)} 个分组、{result["articles"]} 篇文章保持不变。仅重新评分；推荐公式与质量门槛不变。</p>
<p>原页面分含时间与证据折减。原价值分去掉这些折减；新价值分进一步应用校准后的准则。价值分与推荐排序分不能直接视为同一量尺。</p>
<p><a href="site/{week}/index.html">打开新版周报预览 →</a></p><table><tr><th>指标</th><th>平均</th><th>中位数</th><th>范围</th></tr>{stats_html}</table>
<div class="tops">{top_html}</div><h2>全部分组</h2><div style="overflow:auto"><table>{header}{body}</table></div></html>"""
    (out / "comparison.html").write_text(page)
    print(json.dumps({"events": len(rows), "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
