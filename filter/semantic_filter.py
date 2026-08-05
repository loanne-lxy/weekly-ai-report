"""Semantic Embedding Filter — embedding-based relevance scoring

Uses sentence-transformers (MiniLM-L6-v2) on CPU to compute semantic similarity
between article summaries and domain descriptions. Only articles above the
similarity threshold pass through.
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Domain descriptions for semantic matching
DOMAIN_DESCRIPTIONS = {
    "LLM": "Large language model technology: pre-training, fine-tuning, model architecture like MoE and Transformer variants, alignment techniques RLHF and DPO, inference optimization, quantization, distillation, multimodal models, foundation model releases and benchmarks",
    "Agent": "AI autonomous agents: multi-agent systems, tool calling and function calling, MCP protocol, task planning and execution, agent frameworks, coding agents, browser automation agents, memory and retrieval augmented generation RAG, safety guardrails",
    "AI for Science": "AI-driven scientific discovery: protein structure prediction like AlphaFold, drug discovery AIDD, materials science, quantum chemistry, weather forecasting, physics simulation, theorem proving, genomics, molecular dynamics",
    "Design Simulation": "AI-assisted engineering: generative design, CAD and CAE tools, computational fluid dynamics CFD, finite element analysis, 3D generation and rendering, electronic design automation EDA, chip design, topology optimization",
    "Digital Twin": "Industrial digital twins: real-time digital replicas of physical systems, Industry 4.0, smart manufacturing, predictive maintenance, IoT and sensor integration, cyber-physical systems, asset management, edge computing for simulation",
}

THRESHOLD = 0.25


def _get_model():
    """Lazy-load sentence-transformers (CPU, ~80MB first download)"""
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("all-MiniLM-L6-v2")
    except ImportError:
        logger.warning("sentence-transformers not installed. Skipping semantic filter.")
        return None


def compute_scores(articles: list[dict]) -> list[dict]:
    """Score each article against ALL domain descriptions, keep best match"""
    model = _get_model()
    if model is None:
        for a in articles:
            a["semantic_score"] = 0.5
        return articles

    # Pre-compute domain embeddings
    domains = list(DOMAIN_DESCRIPTIONS.keys())
    domain_texts = [DOMAIN_DESCRIPTIONS[d] for d in domains]
    domain_embeddings = model.encode(domain_texts, normalize_embeddings=True)

    # Prepare article texts (title + first 300 chars of summary)
    texts = []
    for a in articles:
        text = (a.get("title", "") + " " + a.get("summary", "")[:300]).strip()
        texts.append(text)

    # Batch encode articles
    article_embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    # Score each article against all domains, pick best
    scored = []
    for i, a in enumerate(articles):
        similarities = np.dot(article_embeddings[i], domain_embeddings.T)
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])
        a["semantic_score"] = best_score
        a["semantic_domain"] = domains[best_idx]

    logger.info(f"Semantic filter: scored {len(articles)} articles")
    return articles


def filter_by_threshold(articles: list[dict], threshold: float = THRESHOLD) -> list[dict]:
    """Remove articles below semantic similarity threshold"""
    kept = [a for a in articles if a.get("semantic_score", 0) >= threshold]
    logger.info(f"Semantic threshold ({threshold}): {len(articles)} → {len(kept)}")
    return kept
