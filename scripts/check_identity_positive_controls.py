"""Live-model SYNTHETIC controls; never included in published report data."""
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from main import load_config
from models.llm_client import LLMClient
from filter.event_identity import IdentityResolver
from filter.event_curator import EventCurator

async def main():
    fixtures=[
        ("AtlasProbe release", "AtlasProbe version 2 was released on 22 October 2026. It adds tool-execution evidence checking for research agents.",
         "AtlasProbe 第二版发布", "AtlasProbe 于2026年10月22日发布第二版，为研究智能体新增工具执行证据检查功能。",True),
        ("LumenStudy paper", "The LumenStudy paper, arXiv:2610.12345, introduces an uncertainty-calibrated molecular screening model and evaluates it on a fixed laboratory dataset.",
         "A look at LumenStudy", "This commentary discusses the LumenStudy paper at https://arxiv.org/abs/2610.12345v1, explaining its uncertainty calibration for molecular screening.",True),
        ("CedarAgent version 1", "CedarAgent version 1 was released on 20 October 2026 with a new tool execution runtime.",
         "CedarAgent version 2", "CedarAgent version 2 was released on 24 October 2026 with a new memory system; this is a separate release from version 1.",False),
        ("BirchFlow for particles", "The BirchFlow paper proposes a neural particle simulator and evaluates trajectory errors on fluid experiments.",
         "MapleFlow for particles", "The MapleFlow paper proposes a different neural particle simulator by a separate research team and evaluates trajectory errors on fluid experiments.",False)]
    arts=[];vectors=[]
    for i,(t1,s1,t2,s2,_) in enumerate(fixtures):
        for j,(title,summary) in enumerate([(t1,s1),(t2,s2)]):
            arts.append({"url":f"https://synthetic-{i}-{j}.invalid/article", "title":title,"summary":summary,"published":"2026-10-22T00:00:00Z","date_status":"verified"})
            vectors.append(np.eye(len(fixtures))[i])
    events=[{"article_indices":[i],"articles":[a],"is_relevant":True,"category":"Agent"} for i,a in enumerate(arts)]
    llm=LLMClient(load_config());root=Path("data/w44-identity-stage/synthetic-controls")
    resolver=IdentityResolver(llm,root/"identity")
    result=await resolver.consolidate(events,arts,np.array(vectors),EventCurator(llm,cache_dir=root/"curation"))
    owner={i:k for k,e in enumerate(result) for i in e["article_indices"]}
    checks=[{"case":f[0],"expected_merge":f[4],"actual_merge":owner[2*i]==owner[2*i+1]} for i,f in enumerate(fixtures)]
    report={"synthetic":True,"checks":checks,"audit":resolver.audit,"all_passed":all(c["expected_merge"]==c["actual_merge"] for c in checks)}
    (root/"evaluation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({"synthetic":True,"checks":checks,"all_passed":report["all_passed"]},ensure_ascii=False,indent=2))
    assert report["all_passed"],"synthetic live-model identity contract failed"

if __name__=="__main__":asyncio.run(main())
