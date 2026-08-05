"""Lightweight Classifier — MiniLM-based domain classification

Runs on CPU (no GPU needed). Classifies each article into one of the 5 domains
using cosine similarity between article embedding and domain description embeddings.
This replaces the expensive LLM classification step, allowing the curator to focus
on scoring and summarization only.
"""
import numpy as np
import logging

logger = logging.getLogger(__name__)

DOMAIN_LABELS = ["LLM", "Agent", "AI for Science", "Design Simulation", "Digital Twin"]

DOMAIN_DESCRIPTIONS = {
    "LLM": "Large language model technology: pre-training, fine-tuning, model architecture, alignment, inference optimization, quantization, multimodal models, foundation model releases",
    "Agent": "AI autonomous agents: multi-agent systems, tool calling, task planning, agent frameworks, coding agents, RAG, safety guardrails",
    "AI for Science": "AI-driven scientific discovery: protein structure, drug discovery, materials science, quantum chemistry, weather forecasting, biology",
    "Design Simulation": "AI engineering design: CAD, CAE, CFD, generative design, 3D rendering, chip design, topology optimization",
    "Digital Twin": "Industrial digital twins: real-time simulation, predictive maintenance, IoT, smart manufacturing, asset management",
}


def _get_model():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("all-MiniLM-L6-v2")
    except ImportError:
        return None


def classify(articles: list[dict]) -> list[dict]:
    """Classify articles using embedding similarity to domain descriptions"""
    model = _get_model()
    if model is None:
        for a in articles:
            if "category" not in a:
                a["category"] = "LLM"
        return articles

    domains = list(DOMAIN_DESCRIPTIONS.keys())
    domain_texts = [DOMAIN_DESCRIPTIONS[d] for d in domains]
    domain_embeddings = model.encode(domain_texts, normalize_embeddings=True)

    texts = []
    for a in articles:
        text = (a.get("title", "") + " " + a.get("summary", "")[:300]).strip()
        texts.append(text)

    article_embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    similarities = np.dot(article_embeddings, domain_embeddings.T)

    for i, a in enumerate(articles):
        best_idx = int(np.argmax(similarities[i]))
        a["category"] = domains[best_idx]
        a["classifier_confidence"] = float(similarities[i][best_idx])

    logger.info(f"MiniLM classified {len(articles)} articles ({len(model.encode(['test']))}d embeddings)")
    return articles
