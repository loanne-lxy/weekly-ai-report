"""Keyword Lexicon + Regex Filter — multi-pattern matching

More sophisticated than the simple keyword pre-filter:
- Domain-specific keyword groups for each of the 5 categories
- Regex patterns for version numbers, benchmark scores, arXiv IDs
- Boost scoring: more keyword hits = higher article quality score
"""
import re
import logging

logger = logging.getLogger(__name__)

# Domain-specific keyword groups
LLM_KEYWORDS = [
    "transformer", "attention", "pre-train", "fine-tun", "LLM", "large language model",
    "GPT", "Claude", "Gemini", "Llama", "Mixtral", "DeepSeek", "Qwen", "Falcon",
    "RLHF", "DPO", "alignment", "MoE", "mixture of experts", "quantization",
    "distillation", "inference optim", "token", "latency", "throughput",
    "benchmark", "MMLU", "HumanEval", "ARC", "HellaSwag", "reasoning",
    "chain-of-thought", "multimodal", "vision language", "embedding",
    "open-weight", "open source model", "foundation model",
]

AGENT_KEYWORDS = [
    "agent", "autonomous", "tool call", "function call", "multi-agent",
    "RAG", "retrieval augment", "memory", "planning", "task decomposition",
    "LangChain", "AutoGen", "CrewAI", "OpenAI swarm", "Claude code",
    "MCP", "model context protocol", "codex", "coding agent",
    "browser agent", "computer use", "safety guardrail", "sandbox",
    "harness", "orchestrat", "workflow", "copilot", "IDE agent",
]

AI4SCIENCE_KEYWORDS = [
    "protein", "AlphaFold", "molecular", "drug discovery", "AIDD",
    "materials science", "crystal", "quantum chemistry", "DFT",
    "weather forecast", "climate model", "physics-informed", "PINN",
    "theorem prov", "mathematical", "genomics", "bioinformatics",
    "neural network potential", "force field", "GNoME", "ESM",
]

DESIGNSIM_KEYWORDS = [
    "generative design", "CAD", "CAE", "CFD", "finite element",
    "simulation", "rendering", "3D generat", "Omniverse", "OpenUSD",
    "chip design", "EDA", "circuit", "PCB", "digital twin design",
    "topology optim", "parametric design", "synthetic data",
]

DIGITALTWIN_KEYWORDS = [
    "digital twin", "industrial IoT", "Industry 4.0", "smart factory",
    "predictive maintenance", "cyber-physical", "CPS", "real-time simulation",
    "sensor fusion", "edge computing", "manufacturing execution",
    "shop floor", "asset management", "condition monitoring",
]

# Regex patterns for technical signals
TECH_PATTERNS = [
    (r'\b\d+\.?\d*[BMK]?\s*(parameters|params|tokens)\b', 2),  # model size
    (r'\b\d+\.?\d*%\s*(improve|faster|reduction|boost)\b', 2),  # benchmark improvement
    (r'arXiv:\d{4}\.\d{4,}', 3),  # arXiv paper ID
    (r'(state.of.the.art|SOTA|best.perform)', 2),  # SOTA claim
    (r'(open.source|released|published|available on GitHub)', 1),  # release signal
    (r'(version|v)\d+\.\d+', 1),  # version number
]

ALL_KEYWORDS = LLM_KEYWORDS + AGENT_KEYWORDS + AI4SCIENCE_KEYWORDS + DESIGNSIM_KEYWORDS + DIGITALTWIN_KEYWORDS


def score_and_filter(articles: list[dict], min_score: int = 1) -> list[dict]:
    """Score articles by keyword+regex hits, filter below threshold"""
    scored = []
    for a in articles:
        text = (a.get("title", "") + " " + a.get("summary", "")).lower()
        # Keyword hits
        keyword_hits = sum(1 for kw in ALL_KEYWORDS if kw.lower() in text)

        # Regex bonus
        regex_bonus = 0
        for pattern, bonus in TECH_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                regex_bonus += bonus

        score = keyword_hits + regex_bonus
        if score >= min_score:
            a["keyword_score"] = score
            scored.append(a)

    scored.sort(key=lambda a: a.get("keyword_score", 0), reverse=True)
    logger.info(f"Keyword filter: {len(articles)} → {len(scored)} (min_score={min_score})")
    return scored
