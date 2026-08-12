"""Top-K reranker — pairwise comparison for fine-grained top ranking.

Two-stage:
  1. Pointwise: filter articles with priority_score >= threshold
  2. Pairwise: compare each pair of Top-K articles, vote-based ranking
"""
import asyncio
import json
import logging
import itertools
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)

PAIRWISE_SYSTEM = (
    "You are a strict editor ranking news articles by importance. "
    "Given two articles, decide which is more important for this week's readers "
    "based on technical breakthrough, real-world impact, and originality."
)

PAIRWISE_TEMPLATE = """Article A: {title_a}
{tldr_a}

Article B: {title_b}
{tldr_b}

Return JSON: {{"winner": "A" or "B", "reason": "one sentence"}}"""


class TopKReranker:
    def __init__(self, llm: LLMClient, k: int = 5, threshold: float = 8.0):
        self.llm = llm
        self.k = k
        self.threshold = threshold

    async def rerank(self, articles: list[dict]) -> list[dict]:
        if len(articles) < 3:
            return articles

        # Pointwise: find candidates above threshold, lower threshold if too few
        candidates = [a for a in articles if a.get("priority_score", 0) >= self.threshold]
        t = self.threshold
        while len(candidates) < 3 and t > 5.0:
            t -= 0.5
            candidates = [a for a in articles if a.get("priority_score", 0) >= t]

        if len(candidates) < 3:
            logger.info(f"TopK rerank: only {len(candidates)} candidates at threshold {t}, skipping pairwise")
            return articles

        # Trim to top-k by existing score
        candidates.sort(key=lambda a: a.get("priority_score", 0), reverse=True)
        top_k = candidates[: self.k]

        if len(top_k) < 2:
            return articles

        # Pairwise comparisons
        wins = {id(a): 0 for a in top_k}
        pairs = list(itertools.combinations(top_k, 2))

        sem = asyncio.Semaphore(5)
        loop = asyncio.get_running_loop()

        async def _compare(a: dict, b: dict):
            async with sem:
                prompt = PAIRWISE_TEMPLATE.format(
                    title_a=a.get("chinese_title", a.get("title", ""))[:100],
                    tldr_a=(a.get("tldr", "") or a.get("ai_summary", ""))[:200],
                    title_b=b.get("chinese_title", b.get("title", ""))[:100],
                    tldr_b=(b.get("tldr", "") or b.get("ai_summary", ""))[:200],
                )
                response = await loop.run_in_executor(
                    None, self.llm.chat, PAIRWISE_SYSTEM, prompt
                )
                try:
                    cleaned = response.strip()
                    for fence in ["```json", "```"]:
                        cleaned = cleaned.removeprefix(fence).removesuffix("```").strip()
                    result = json.loads(cleaned)
                    winner = result.get("winner", "").upper()
                    if winner == "A":
                        wins[id(a)] += 1
                    elif winner == "B":
                        wins[id(b)] += 1
                except Exception as e:
                    logger.debug(f"Pairwise compare failed: {e}")

        await asyncio.gather(*[_compare(a, b) for a, b in pairs])

        # Sort by wins desc, then original score desc
        top_k.sort(key=lambda a: (wins[id(a)], a.get("priority_score", 0)), reverse=True)

        # Assign rank and boost scores for top items
        for i, a in enumerate(top_k):
            a["rank"] = i + 1
            # Small boost for ranking position (0.1 per rank step, capped)
            if i < 3:
                a["priority_score"] = min(10, a.get("priority_score", 7) + (3 - i) * 0.3)

        logger.info(
            f"TopK rerank: {len(top_k)} candidates, "
            f"{len(pairs)} pairwise comparisons, "
            f"Top 3: {[a.get('chinese_title', a.get('title', ''))[:40] for a in top_k[:3]]}"
        )

        # Merge reranked top_k back into full article list
        top_k_ids = {id(a) for a in top_k}
        others = [a for a in articles if id(a) not in top_k_ids]
        return list(top_k) + others
