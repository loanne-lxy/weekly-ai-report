"""Evaluate explicit news identity and bounded research-problem controls."""
import argparse
import json
from collections import Counter
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument("--run-dir",required=True)
p.add_argument("--controls",default="tests/fixtures/w44_grouping_controls.json")
a=p.parse_args();run=Path(a.run_dir)
audit=json.loads((run/"audit.json").read_text())
events=json.loads((run/"curated_events.json").read_text())
controls=json.loads(Path(a.controls).read_text())
owner={art["url"]:i for i,e in enumerate(events) for art in e["articles"]}
pairs={frozenset(x["urls"]):x for x in audit["identity_resolution"]["pairs"]}
checks=[]
for c in controls:
    left,right=c["urls"]
    present=left in owner and right in owner
    merged=present and owner[left]==owner[right]
    decision=pairs.get(frozenset(c["urls"]))
    checks.append({**c,"both_published":present,"retrieved":decision is not None,
                   "relation":decision["relation"] if decision else None,"expected_merge":c["label"] in {"same_event","same_research_problem"},"actual_merge":merged,
                   "incorrect":(c["label"] in {"same_event","same_research_problem"} and not present) or (present and merged!=(c["label"] in {"same_event","same_research_problem"}))})
result={"events":len(events),"articles":len(owner),"size_distribution":dict(Counter(len(e["articles"]) for e in events)),
        "candidate_pairs":len(pairs),"relations":dict(Counter(x["relation"] for x in pairs.values())),
        "real_controls":checks,"independent_positive_recall":None,
        "exploratory_positive_matches":sum(x["expected_merge"] and x["actual_merge"] for x in checks),
        "limitation":"No independently labelled real positive set: precision/recall targets are NOT certified. Synthetic tests check behaviour only."}
(run/"identity_evaluation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in result.items() if k!="real_controls"},ensure_ascii=False,indent=2))

assert not any(x["incorrect"] for x in checks),"real grouping control failed (see identity_evaluation.json)"
