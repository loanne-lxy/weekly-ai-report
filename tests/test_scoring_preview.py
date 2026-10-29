import unittest
from filter.scoring import compute_value_score, score_event
from filter.quality import parse_date
from scripts.preview_scoring import validate_scores


class ScoringPreviewTests(unittest.TestCase):
    def test_value_is_independent_of_age_publisher_and_evidence(self):
        scores = dict(importance=.8, novelty=.6, impact=.7, evidence_relevance=.95)
        art = dict(url="https://arxiv.org/abs/test", title="Study", summary="Evidence", published="2026-10-23T00:00:00Z")
        first = score_event([art], scores, now=parse_date("2026-10-24T00:00:00Z"))
        second = score_event([{**art, "url":"https://unknown.invalid/study"}], {**scores,"evidence_relevance":.75}, now=parse_date("2026-10-26T00:00:00Z"))
        self.assertEqual(first["value_score"], .71)
        self.assertEqual(first["value_score"], second["value_score"])
        self.assertGreater(first["final_score"], second["final_score"])

    def test_value_preserves_real_zero_and_bounds(self):
        self.assertEqual(compute_value_score(dict(importance=0, novelty=0, impact=0)), 0)
        self.assertEqual(compute_value_score(dict(importance=2, novelty=1, impact=1)), 1)

    def test_score_preview_requires_real_evidence_and_valid_scores(self):
        text="The method reduces measured inference cost in a controlled experiment."
        articles=[dict(title="Study", text=text)]
        result={}
        for dimension in ("importance","novelty","impact"):
            result[dimension]=.6
            result[dimension+"_rationale"]="Study reports a measured improvement within a controlled setting."
        result["score_evidence"]={d:dict(article_index=0,quote=text) for d in ("importance","novelty","impact")}
        self.assertEqual(validate_scores(result, articles)["importance"], .6)
        result["importance"]=float("nan")
        with self.assertRaises(ValueError):validate_scores(result,articles)
        result["importance"]=.6
        result["score_evidence"]["impact"]["quote"]="Invented global adoption in all industries."
        with self.assertRaises(ValueError):validate_scores(result,articles)


if __name__=="__main__":unittest.main()
