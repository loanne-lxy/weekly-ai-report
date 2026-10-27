"""Export every fetched article's final disposition; fail on loss/duplication."""
import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path


def build_lineage(run):
    raw=json.loads((run/"raw_articles.json").read_text())
    audit=json.loads((run/"audit.json").read_text())
    input_counts=Counter(x["url"] for x in raw)
    decisions=defaultdict(deque)
    def add(url, decision):
        decisions[url].append(decision)
        if len(decisions[url]) > input_counts[url]:
            raise ValueError("Article assigned more often than fetched: "+url)
    for item in audit["rejected_articles"]:
        add(item["url"], {"status":"preflight_rejected", "reasons":item["reasons"]})
    for event in audit["curation_decisions"]:
        for url in event["urls"]:
            add(url, {"status":event["status"], "event":event["title"], "reasons":event["reasons"]})
    if {url:len(items) for url,items in decisions.items()}!=dict(input_counts):
        raise ValueError("Input/output article accounting mismatch")
    return [{"input_index":i, "url":x["url"], "title":x.get("title", ""), **decisions[x["url"]].popleft()} for i,x in enumerate(raw)]


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("run",type=Path)
    args=parser.parse_args()
    rows=build_lineage(args.run)
    (args.run/"article_lineage.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    lines=["# "+args.run.name+" 文章去向", "", "状态统计："+json.dumps(dict(Counter(x["status"] for x in rows)),ensure_ascii=False), ""]
    for row in rows:
        lines.extend(["## "+row["title"], "", "- 链接："+row["url"], "- 状态："+row["status"], "- 归属："+row.get("event", "预处理"), "- 原因："+"；".join(row["reasons"]), ""])
    (args.run/"article_lineage.md").write_text("\n".join(lines))
    print(json.dumps(dict(Counter(x["status"] for x in rows)),ensure_ascii=False))
