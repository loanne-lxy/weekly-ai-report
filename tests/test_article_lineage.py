import json
import tempfile
import unittest
from pathlib import Path
from scripts.article_lineage import build_lineage

class LineageTests(unittest.TestCase):
    def run_case(self, urls):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp)
            (run/"raw_articles.json").write_text(json.dumps([{"url":"a"},{"url":"b"}]))
            (run/"audit.json").write_text(json.dumps({"rejected_articles":[{"url":"a","reasons":["outside_report_window"]}],
                "curation_decisions":[{"urls":urls,"status":"passed","title":"theme","reasons":[]}]}))
            return build_lineage(run)

    def test_all_articles_accounted(self):
        self.assertEqual([x["status"] for x in self.run_case(["b"])],["preflight_rejected","passed"])

    def test_duplicate_or_missing_article_fails(self):
        for urls in (["b","b"], [], ["a","b"]):
            with self.assertRaises(ValueError):self.run_case(urls)

    def test_duplicate_fetch_records_are_accounted_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Path(temp)
            (run/"raw_articles.json").write_text(json.dumps([{"url":"a"},{"url":"a"}]))
            (run/"audit.json").write_text(json.dumps({"rejected_articles":[{"url":"a","reasons":["duplicate_url"]}],
                "curation_decisions":[{"urls":["a"],"status":"passed","title":"event","reasons":[]}]}))
            rows=build_lineage(run)
            self.assertEqual(len(rows),2)
            self.assertEqual([r["input_index"] for r in rows],[0,1])
