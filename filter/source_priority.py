"""Source Priority Filter — weight articles by source authority tier

Tiers defined in references/curation-rules.md:
  Tier 1 (×3): Official blogs (OpenAI, DeepMind, Meta), arXiv, conferences
  Tier 2 (×2): Established tech media, NVIDIA/Siemens/Ansys dev blogs
  Tier 3 (×1): Nitter RSS, Medium, Reddit, HN
  Tier 4 (×0): Aggregators, content farms — skip
"""
import logging

logger = logging.getLogger(__name__)

# Tier-1 domain patterns (matched against source URL or name)
TIER1_DOMAINS = [
    "openai.com", "anthropic.com", "deepmind.google", "ai.meta.com",
    "arxiv.org", "huggingface.co/papers", "nature.com", "science.org",
    "nvidia.com", "developer.nvidia.com", "microsoft.com/en-us/research",
    "blog.research.google", "digitaltwinconsortium.org",
]
TIER2_DOMAINS = [
    "techcrunch.com", "venturebeat.com", "theverge.com", "ansys.com",
    "siemens.com", "autodesk.com", "ptc.com", "3ds.com",
    "azure.microsoft.com", "aws.amazon.com", "ge.com",
]
TIER4_DOMAINS = [
    "medium.com", "substack.com", "reddit.com", "news.ycombinator.com",
]

TIER_WEIGHTS = {1: 3.0, 2: 2.0, 3: 1.0, 4: 0.0}


def classify_source(source_name: str, source_url: str = "") -> int:
    """Return tier (1-4) for a source. Tier 4 sources should be skipped."""
    combined = (source_name + " " + source_url).lower()
    for domain in TIER1_DOMAINS:
        if domain in combined:
            return 1
    for domain in TIER4_DOMAINS:
        if domain in combined:
            return 4
    for domain in TIER2_DOMAINS:
        if domain in combined:
            return 2
    return 3


def filter_and_weight(articles: list[dict]) -> list[dict]:
    """Assign tier weights and remove Tier 4 (noise) sources"""
    kept = []
    skipped = 0
    for a in articles:
        tier = classify_source(
            a.get("source_name", ""),
            a.get("url", ""),
        )
        weight = TIER_WEIGHTS[tier]
        if weight == 0:
            skipped += 1
            continue
        a["source_tier"] = tier
        a["source_weight"] = weight
        kept.append(a)
    logger.info(f"Source priority: {len(articles)} → {len(kept)} (skipped {skipped} noise)")
    return kept
