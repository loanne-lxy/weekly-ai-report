"""Verify a generated report, database evidence and input accounting."""
import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from filter.quality import parse_date
from generator.report_generator import _query_knowledge_db

p=argparse.ArgumentParser()
p.add_argument("--data-dir",required=True)
p.add_argument("--output-dir",required=True)
p.add_argument("--week",default="2026-W44")
p.add_argument("--baseline",required=True)
a=p.parse_args()
root=Path(a.output_dir)/a.week
run=Path(a.data_dir)/"runs"/a.week
baseline=sqlite3.connect(a.baseline);baseline.row_factory=sqlite3.Row
conn=sqlite3.connect(str(Path(a.data_dir)/"knowledge.db"));conn.row_factory=sqlite3.Row
data=_query_knowledge_db(a.week,str(Path(a.data_dir)/"knowledge.db"))
events=data["events"];by_id={e["id"]:e for e in events}
assert len(events)==len(json.loads((run/"curated_events.json").read_text()))
audit=json.loads((run/"audit.json").read_text())
inputs=json.loads((run/"raw_articles.json").read_text())
seen=[x["url"] for x in audit["rejected_articles"]]+[u for e in audit["curation_decisions"] for u in e["urls"]]
assert sorted(seen)==sorted(x["url"] for x in inputs), "silent article loss or duplication"
for e in events:
    arts=data["articles_by_event"][e["id"]]
    assert len(arts)==len(e["evidence"])
    assert {x["url"] for x in arts}=={x["url"] for x in e["evidence"]}
    assert min(x["relevance"] for x in e["evidence"])>=.75
    if len(arts)>1:
        validation=e.get("identity_validation", {})
        assert validation.get("method")=="full_event_curation", "missing whole-group review"
        assert sorted(validation.get("article_urls",[]))==sorted(x["url"] for x in arts)
        kind=e.get("grouping_type")
        assert kind in {"news_event","research_theme"}
        if kind=="research_theme":
            assert validation.get("same_research_problem") is True
            assert len(e.get("research_question", ""))>=8
        else:
            assert validation.get("same_event") is True
        import re
        norm=lambda s:re.sub(r"\s+","",s).casefold()
        members={x["url"]:x for x in arts}
        for evidence in e["evidence"]:
            art=members[evidence["url"]];quote=evidence["quote"]
            assert evidence.get("reason")
            assert len(quote)>=12 and norm(quote) in norm(art["title"]+" "+art["summary"][:1800])

    else:
        assert e.get("coherence") is None and e.get("coherence_status")=="not_applicable"
    assert not e["title"].startswith("[未翻译]")
    if e["top_eligible"]:
        assert e["quality_status"]=="passed"
        for art in arts:
            dt=parse_date(art["published"])
            assert dt and parse_date(audit["window_start"])<=dt<=parse_date(audit["window_end"])
    assert all(e.get(key+"_rationale") for key in ("importance","novelty","impact"))

html=BeautifulSoup((root/"index.html").read_text(),"html.parser")
section=html.find("h2",string="本周核心事件").find_parent("section")
actual=[el.get_text(strip=True) for el in section.select("h3")]
expected=[e["title"] for e in sorted(events,key=lambda e:(-e["final_score"],-e["importance"],e["title"])) if e["top_eligible"]][:5]
assert actual==expected,"Top Events ranking mismatch"
for page in root.rglob("*.html"):
    soup=BeautifulSoup(page.read_text(),"html.parser")
    for el in soup.select("a[href]"):
        href=el["href"]
        if urlsplit(href).scheme or href.startswith("#"):continue
        path=unquote(urlsplit(href).path)
        if not path:continue
        target=(page.parent/path).resolve()
        if not target.exists() and path.startswith("../2026-W"):
            target=Path("output")/Path(path).parts[1]/"index.html"
        assert target.exists(),f"Broken link: {page.name}: {href}"

analysis=json.loads((root/"research_observations.json").read_text())
assert 2<=len(analysis["observations"])<=3
assert {e["category"] for e in events if e["top_eligible"]} <= {s["category"] for s in analysis["domain_signals"]}, "missing substantive domain signals"
for o in analysis["observations"]:
    assert len({c["event_id"] for c in o["citations"]})>=2
    assert all(c["event_id"] in by_id for c in o["citations"])

# Older event records and membership must be identical, ignoring newly added columns.
cols=[x[1] for x in baseline.execute("PRAGMA table_info(events)")]
query=f"SELECT {','.join(cols)} FROM events WHERE week_label != ? ORDER BY id"
assert [tuple(r) for r in baseline.execute(query,(a.week,))]==[tuple(r) for r in conn.execute(query,(a.week,))]
for row in baseline.execute("SELECT DISTINCT e.week_label AS sw, a.* FROM articles a JOIN event_articles ea ON a.url=ea.article_url JOIN events e ON e.id=ea.event_id WHERE e.week_label != ?",(a.week,)):
    original=dict(row);week=original.pop("sw")
    frozen=conn.execute("SELECT payload_json FROM report_article_snapshots WHERE week_label=? AND article_url=?",(week,original["url"])).fetchone()
    assert frozen and all(json.loads(frozen[0]).get(k)==v for k,v in original.items()), "historical article contents changed"
old_id=baseline.execute("SELECT id FROM events WHERE id LIKE '%99b65515'").fetchone()[0]
assert not conn.execute("SELECT 1 FROM events WHERE id=?",(old_id,)).fetchone()
old_urls={r[0] for r in baseline.execute("SELECT article_url FROM event_articles WHERE event_id=?",(old_id,))}
resolution=[]
for x in audit["rejected_articles"]:
    if x["url"] in old_urls:resolution.append({"url":x["url"],"status":"preflight_rejected","reasons":x["reasons"]})
for e in audit["curation_decisions"]:
    for u in e["urls"]:
        if u in old_urls:resolution.append({"url":u,"status":e["status"],"event":e["title"],"reasons":e["reasons"]})
assert len(resolution)==36
result={"week":a.week,"input_articles":len(inputs),"events":len(events),
        "linked_articles":sum(len(v) for v in data["articles_by_event"].values()),
        "top_eligible":sum(bool(e["top_eligible"]) for e in events),
        "largest_event":max(map(len,data["articles_by_event"].values())),
        "observations":len(analysis["observations"]),"top_events":expected,
        "old_event_99b65515_resolution":resolution,"historical_events_unchanged":True,
        "all_local_links_valid":True,"all_inputs_accounted_for":True}
(run/"verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in result.items() if k!="old_event_99b65515_resolution"},ensure_ascii=False,indent=2))
