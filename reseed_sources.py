"""Re-seed github/exa_search sources from sources.yaml into source.db.

Fixes the 8 sources lost during the 8/24 first migration (unique
(canonical_url, connector) collision on empty canonical_url).
Only adds missing ids — never touches existing rows or runtime state.
"""
import sys, os, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from source_db import SourceDB
from source_registry import SourceRegistry

MISSING_EXPECTED = {
    "exa_search:4cb4ce25", "exa_search:04c15fce", "exa_search:991b0be5",
    "exa_search:efb7e780", "github:crewAIInc/crewAI",
    "github:huggingface/transformers", "github:langchain-ai/langchain",
    "github:trending",
}

data = yaml.safe_load(open("sources.yaml", encoding="utf-8"))
srcs = data.get("sources", [])

db = SourceDB("data/source.db")
existing = {r["id"] for r in db.get_all()}
added = []
for s in srcs:
    conn = s.get("connector", s.get("type"))
    if conn not in ("github", "exa_search"):
        continue
    sid = s.get("id", "")
    if sid in existing:
        print(f"  skip (exists): [{conn}] {s.get('name')}")
        continue
    cleaned = {k: v for k, v in s.items() if k not in ("trust", "weight")}
    has_endpoint = bool(cleaned.get("endpoint") or cleaned.get("url"))
    if has_endpoint:
        from source_resolver import resolve_source_dict
        resolved = resolve_source_dict(cleaned)
    else:
        resolved = SourceRegistry._normalize(cleaned)
    resolved["id"] = sid
    saved = db.add_or_update(resolved)
    if saved:
        added.append(sid)
        print(f"  ADDED: [{conn}] {s.get('name')} -> canonical={resolved.get('canonical_url')}")

n_active = db.count_active()
print(f"\n重新播种 {len(added)} 个源; source.db 现在激活 {n_active} 个")
missing_now = MISSING_EXPECTED - {r["id"] for r in db.get_all()}
print("仍缺失:", missing_now if missing_now else "无 — 8 个全部补回")
db.close()