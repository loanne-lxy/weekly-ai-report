"""Synthetic identity fixtures: regression contracts, not a human-labelled benchmark."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from filter.event_identity import anchors,candidates,identity_groups,IdentityResolver

class IdentityTests(unittest.TestCase):
    def test_publisher_association_is_separate_from_citation(self):
        from filter.article_context import extract_context
        html="""<meta name="citation_article_type" content="News &amp; Views"><meta name="citation_doi" content="10.1234/comment">
        <a href="/paper" data-track-category="associated content" data-track-label="10.1234/paper">Target paper</a>
        <section data-title="References"><a href="https://doi.org/10.1234/background">Background</a></section>
        <nav><a href="/unrelated">Noise</a></nav>"""
        ctx=extract_context(html,"https://journal.example/comment")
        self.assertEqual(ctx["article_type"],"News & Views")
        self.assertEqual(ctx["associated_content"][0]["url"],"https://journal.example/paper")
        self.assertEqual(len(ctx["associated_content"]),1)
        self.assertEqual(ctx["reference_urls"],["https://doi.org/10.1234/background"])

    def test_supplementary_search_is_bounded_and_week_scoped(self):
        from filter.evidence_discovery import evidence_sources
        arts=[{"url":f"https://source.example/{i}","title":f"Neural agent method {i}",
               "published":"2026-10-22T00:00:00Z","category":"Agent"} for i in range(6)]
        arts.append({"url":"https://old.example/1","title":"Neural agent old result","published":"2025-01-01"})
        found=evidence_sources(arts,"2026-10-26T00:00:01Z",3,3)
        self.assertEqual(len(found),3)
        self.assertTrue(all(x["max_results"]==3 and "old.example" not in x["evidence_seed_url"] for x in found))

    def test_paper_version_anchor_recall_without_similarity(self):
        arts=[{"url":"https://arxiv.org/abs/2610.12345v1"},
              {"url":"https://news.example/study","summary":"Study https://arxiv.org/pdf/2610.12345v2"}]
        self.assertIn((0,1),candidates(arts,np.eye(2)))

    def test_cross_language_name_recalls_without_vector_match(self):
        arts=[{"url":"https://a.example/1","title":"AtlasAgent announces version 2"},
              {"url":"https://b.example/2","title":"AtlasAgent 第二版发布"}]
        self.assertIn((0,1),candidates(arts,np.eye(2)))

    def test_shared_repository_only_retrieves(self):
        arts=[{"url":"https://github.com/org/tool/releases/tag/v1"},
              {"url":"https://github.com/org/tool/releases/tag/v2"}]
        self.assertIn((0,1),candidates(arts,np.eye(2)))
        self.assertEqual(len(identity_groups([{"article_indices":[0]},{"article_indices":[1]}],{})),2)

    def test_no_transitive_topic_bridge(self):
        ev=[{"article_indices":[i]} for i in range(3)]
        out=identity_groups(ev,{(0,1):True,(1,2):True})
        self.assertEqual(len(out),2)
        self.assertNotIn([0,1,2],out)

    def test_large_real_event_not_capped(self):
        ev=[{"article_indices":[i]} for i in range(10)]
        yes={(i,j):True for i in range(10) for j in range(i+1,10)}
        self.assertEqual(identity_groups(ev,yes),[list(range(10))])
        self.assertEqual(identity_groups(ev[::-1],yes),[list(range(10))])

    def run_resolver(self,relation="same_event",bad_quote=False,rebuild_fails=False):
        text="Project Atlas version one launches a new tool execution evaluator."
        arts=[{"url":f"https://source{i}.example/news","title":"Atlas release","summary":text} for i in range(2)]
        ev=[{"articles":[a],"article_indices":[i],"is_relevant":True} for i,a in enumerate(arts)]
        class LLM:
            def chat(self,system,payload):
                return json.dumps([{"pair_id":0,"relation":relation,"identity":"Atlas version one launch", "reason":"same release", "quotes":["fabricated source text" if bad_quote else text,text]}])
        class Curator:
            async def curate_events(self,events,embeddings):
                return [] if rebuild_fails else [{**events[0],"is_relevant":True}]
        with tempfile.TemporaryDirectory() as temp:
            resolver=IdentityResolver(LLM(),temp)
            out=asyncio.run(resolver.consolidate(ev,arts,np.ones((2,3)),Curator()))
            return out,resolver.audit

    def test_existing_cluster_rechecked_and_split(self):
        text="This is an independent neural model research article."
        arts=[{"url":f"https://example.com/{i}","title":str(i),"summary":text} for i in range(2)]
        class LLM:
            def chat(self,*args):return json.dumps([{"pair_id":0,"relation":"different_event","reason":"different named papers"}])
        class Curator:
            async def curate_events(self,events,embeddings):return [{**events[0],"is_relevant":True}]
        with tempfile.TemporaryDirectory() as temp:
            resolver=IdentityResolver(LLM(),temp)
            out=asyncio.run(resolver.consolidate([{"articles":arts,"article_indices":[0,1]}],arts,np.ones((2,3)),Curator()))
        self.assertEqual(len(out),2)
        self.assertEqual(sorted(i for e in out for i in e["article_indices"]),[0,1])

    def test_grounded_same_event_rebuilt(self):
        out,audit=self.run_resolver();self.assertEqual(len(out),1)
        self.assertEqual(len(out[0]["articles"]),2);self.assertTrue(out[0]["identity_checks"])

    def test_related_topic_stays_separate(self):
        out,_=self.run_resolver("related_topic");self.assertEqual(len(out),2)

    def test_fabricated_identity_evidence_fails_closed(self):
        out,audit=self.run_resolver(bad_quote=True);self.assertEqual(len(out),2)
        self.assertEqual(audit["uncertain_pairs"],1)

    def test_failed_rewrite_preserves_every_article(self):
        out,audit=self.run_resolver(rebuild_fails=True);self.assertEqual(len(out),2)
        self.assertEqual(audit["merges"][0]["status"],"retained_originals")

if __name__=="__main__":unittest.main()
