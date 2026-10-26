import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from filter.quality import prepare_articles, parse_date, report_window, score_number
from filter.event_curator import EventCurator
from event_clustering import coherent_groups, coherence
from filter.scoring import score_event, compute_final_score
from article_db import KnowledgeStore
from generator.report_generator import _query_knowledge_db
from generator.research_observations import synthesize
from main import _dedup_events

TEXT = "The agent evaluates tool execution against independent evidence and reports unsupported claims."

def article(i=0, date="2026-10-22T00:00:00Z"):
    return {"url": f"https://arxiv.org/abs/test{i}", "title": f"Evidence agent {i}",
            "summary": TEXT, "published": date, "source_id": "arxiv:test",
            "date_status": "verified", "source_name": "arxiv"}

def result(indices=None):
    ids = [0] if indices is None else indices
    return {"event_index": 0, "article_indices": ids, "is_relevant": True,
      "same_concrete_event": True, "event_title": "证据检查方法", "breakthrough": "检查工具执行证据。",
      "problem": "识别缺失支持的陈述。", "research_value": "支持研究评测。", "short_summary": "检查执行证据",
      "category": "Agent", "importance": 0, "novelty": 0.4, "impact": 0.5,
      "importance_rationale": "Evidence agent未报告增益", "novelty_rationale": "缺乏历史比较", "impact_rationale": "局限于评测",
      "evidence": [{"index": i, "relevance": 0.95, "reason": "describes evidence checking", "quote": TEXT} for i in ids]}

class QualityTests(unittest.TestCase):
    def test_exa_requests_exact_frozen_seven_days(self):
        from unittest.mock import patch, AsyncMock
        from types import SimpleNamespace
        from fetcher.extractors import ExaExtractor
        search = AsyncMock(return_value=SimpleNamespace(results=[]))
        with patch.dict("os.environ", {"REPORT_CUTOFF": "2026-10-26T00:00:01Z"}), patch("exa_py.AsyncExa", return_value=SimpleNamespace(search=search)):
            asyncio.run(ExaExtractor().extract(None, {"query": "AI research", "exa_api_key": "test"}))
        args = search.call_args.kwargs
        self.assertEqual(args["start_published_date"], "2026-10-19T00:00:01+00:00")
        self.assertEqual(args["end_published_date"], "2026-10-26T00:00:01+00:00")

    def test_recover_short_body_before_quarantine(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from filter.quality import recover_short_bodies
        a=article();a["summary"]="Short RSS intro"
        response=SimpleNamespace(text="<article>body</article>",headers={"Content-Type":"text/html"},raise_for_status=lambda:None)
        with patch("requests.get",return_value=response),patch("trafilatura.bare_extraction",return_value=SimpleNamespace(text=TEXT*3,date=None,title="Article")):
            restored=asyncio.run(recover_short_bodies([a]))
        self.assertEqual(restored[0]["raw_extra"]["body_recovery"],"trafilatura")
        self.assertEqual(len(prepare_articles(restored,"2026-10-26T00:00:01Z")[0]),1)

    def test_window_and_rfc(self):
        start,end=report_window("2026-10-26T00:00:01Z")
        self.assertEqual((end-start).days,7)
        self.assertEqual(parse_date("Thu, 22 Oct 2026 19:00:00 GMT").hour,19)
        arts=[article(0,"2026-06-01"), article(1,"2026-09-07"),article(2,"2026-10-27"),article(3,"")]
        accepted,rejected=prepare_articles(arts,end)
        self.assertEqual(len(rejected),3)
        self.assertEqual(accepted[0]["date_status"],"unknown")

    def test_body_hygiene(self):
        a=article();a["summary"]="---\ntitle: wrong\n---\n"+TEXT
        b=article(1);b["summary"]="Pivot News"
        accepted,rejected=prepare_articles([a,b],"2026-10-26T00:00:01Z")
        self.assertEqual(accepted[0]["summary"],TEXT)
        self.assertEqual(rejected[0]["reasons"],["insufficient_body"])

    def test_no_chain_cluster(self):
        angles=np.deg2rad([0,35,70]);v=np.array([np.cos(angles),np.sin(angles)]).T
        groups=coherent_groups([0,1,2],v,.72)
        self.assertEqual(sorted(sum(groups,[])),[0,1,2])
        self.assertGreater(len(groups),1)
        self.assertTrue(all(coherence(g,v)>=.72 for g in groups))

    def test_large_cluster_partition(self):
        v=np.ones((36,3));groups=coherent_groups(list(range(36)),v)
        self.assertEqual(sorted(sum(groups,[])),list(range(36)))
        self.assertLessEqual(max(map(len,groups)),8)

    def test_split_evidence_local_and_zero(self):
        evt={"articles":[article(i) for i in range(3)],"article_indices":[10,20,30]}
        data=result([2]);out=EventCurator(None)._build(evt,data,np.ones((31,2)))
        self.assertEqual(out["article_indices"],[30]);self.assertEqual(out["top_articles"],[0])
        self.assertEqual(out["importance"],0)
        self.assertEqual(out["evidence"][0]["url"],article(2)["url"])

    def test_fabricated_quote_and_unrelated_evidence(self):
        evt={"articles":[article()],"article_indices":[0]};data=result()
        data["evidence"][0]["quote"]="This statement does not appear in the input source"
        with self.assertRaises(ValueError):EventCurator(None)._build(evt,data,np.ones((1,2)))
        data=result();data["evidence"][0]["relevance"]=.2
        with self.assertRaises(ValueError):EventCurator(None)._build(evt,data,np.ones((1,2)))

    def test_unknown_date_not_top(self):
        a=article();a["date_status"]="unknown"
        out=EventCurator(None)._build({"articles":[a],"article_indices":[0]},result(),np.ones((1,2)))
        self.assertFalse(out["top_eligible"])

    def test_invalid_llm_is_quarantined(self):
        class Bad:
            def chat(self,*args):return "not json"
        evt={"articles":[article()],"article_indices":[0]}
        out=asyncio.run(EventCurator(Bad()).curate_events([evt],np.ones((1,2))))
        self.assertFalse(out[0]["is_relevant"])
        self.assertFalse(out[0]["top_eligible"])

    def test_split_events_not_remerged(self):
        events=[{"title":"same original", "summary":"same original", "articles":[article(i)],"article_indices":[i]} for i in range(2)]
        self.assertEqual(len(_dedup_events(events)),2)

    def test_no_count_or_trending_boost(self):
        scores={"importance":.5,"novelty":.5,"impact":.5,"evidence_relevance":.95}
        a=article();b=copy.deepcopy(a);b["raw_extra"]={"trending_rank":1}
        now=parse_date("2026-10-26T00:00:01Z")
        one=score_event([a],scores,now=now);many=score_event([b]*10,scores,now=now)
        self.assertEqual(one["final_score"],many["final_score"])
        self.assertEqual(many["importance"],.5)
        self.assertEqual(score_number(float("nan")),0)

    def test_weekly_replacement_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/"k.db");db=KnowledgeStore(path)
            a=article();evt=EventCurator(None)._build({"articles":[a],"article_indices":[0]},result(),np.ones((1,2)))
            evt["final_score"]=.3
            for week in ("2026-W43","2026-W44"):
                db.persist_run([], [a], [evt], week, "", [a])
            evt["event_title"]="新事件标题"
            a["summary"] = "Changed content for W44 only"
            for _ in range(2):db.persist_run([], [a], [evt], "2026-W44", "", [a])
            self.assertEqual(db.event_count("2026-W43"),1)
            self.assertEqual(db.event_count("2026-W44"),1)
            data=_query_knowledge_db("2026-W44",path)
            self.assertEqual(sum(map(len,data["articles_by_event"].values())),1)
            e=data["events"][0];self.assertEqual(e["final_score"],.3)
            self.assertEqual(e["evidence"][0]["url"],a["url"])
            historical=_query_knowledge_db("2026-W43",path)
            self.assertEqual(next(iter(historical["articles_by_event"].values()))[0]["summary"],TEXT)
            db.close()

    def test_equal_titles_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp:
            db=KnowledgeStore(str(Path(temp)/"k.db"))
            arts=[article(0),article(1)]
            events=[EventCurator(None)._build({"articles":[a],"article_indices":[i]},result(),np.ones((2,2))) for i,a in enumerate(arts)]
            db.persist_run([],arts,events,"2026-W44","",arts)
            self.assertEqual(db.event_count("2026-W44"),2)
            db.close()

    def test_missing_domain_signals_retries_even_with_valid_observations(self):
        class Retry:
            calls=0
            def chat(self,*args):
                self.calls+=1
                citations=[{"event_id":str(i),"quote":TEXT} for i in range(2)]
                return json.dumps({"observations":[{"title":"Observation", "citations":citations}]*2,
                    "domain_signals":[] if self.calls==1 else [{"category":"Agent","citations":citations}]})
        llm=Retry()
        events=[{"id":str(i),"top_eligible":True,"quality_status":"passed","title":"x","summary":TEXT,"category":"Agent"} for i in range(2)]
        out=synthesize(llm,events)
        self.assertEqual(llm.calls,2)
        self.assertEqual(len(out["domain_signals"]),1)

    def test_unbacked_observation_rejected(self):
        class Bad:
            def chat(self,*args):return json.dumps({"observations":[{"citations":[{"event_id":"fake","quote":TEXT}]}]})
        events=[{"id":str(i),"top_eligible":True,"quality_status":"passed","title":"x","summary":TEXT,"category":"Agent"} for i in range(2)]
        self.assertEqual(synthesize(Bad(),events)["observations"],[])

if __name__=="__main__":unittest.main()
