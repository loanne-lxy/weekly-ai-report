"""
Event Clustering — Hybrid bucket-based clustering.

流程:
  N 篇文章
        ↓
  按 source_type 分桶 (academic vs social)
        ↓
  academic: cosine greedy (threshold=0.40) → 研究趋势
  social:   BERTopic (UMAP+HDBSCAN) → 具体事件
        ↓
  返回 (Event 列表带 bucket 标签, 全部 embedding 数组)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── 分桶配置 ──────────────────────────────────────────────────
SOCIAL_THRESHOLD = 0.30  # 哪些源算 social (source_name 包含以下任一)
ACADEMIC_KEYWORDS = ("arxiv", "nature")  # 学术源关键词

SOCIAL_CONFIG = {
    "n_neighbors": 3,
    "n_components": 5,
    "min_cluster_size": 2,
    "min_dist": 0.0,
}

ACADEMIC_THRESHOLD = 0.50  # cosine greedy 阈值


@dataclass
class Event:
    """一个聚合后的事件簇。"""
    id: str = ""
    title: str = ""
    summary: str = ""
    category: str = ""
    importance: float = 0.0
    novelty: float = 0.0
    impact: float = 0.0
    article_indices: list[int] = field(default_factory=list)
    representative_score: float = 0.0
    bucket: str = "social"  # "academic" | "social"
    method: str = "bertopic"  # "cosine-greedy" | "bertopic"


# ── 模型缓存 ─────────────────────────────────────────────────────
_model_cache: dict[str, Any] = {}


def _get_model(model_name: str = DEFAULT_MODEL) -> Any:
    if model_name not in _model_cache:
        try:
            from fastembed import TextEmbedding
            logger.info(f"Loading embedding model: {model_name}...")
            _model_cache[model_name] = TextEmbedding(model_name)
            logger.info("Embedding model loaded.")
        except Exception as e:
            logger.warning(f"Failed to load embedding model: {e}")
            _model_cache[model_name] = None
    return _model_cache[model_name]


# ── 源分类 ────────────────────────────────────────────────────
def _classify_source(source_name: str) -> str:
    """按 source_name 判断 academic / social。"""
    s = source_name.lower()
    return "academic" if any(kw in s for kw in ACADEMIC_KEYWORDS) else "social"


# ── 分桶 ────────────────────────────────────────────────────────
def _split_buckets(articles: list[dict]) -> dict[str, list[int]]:
    """按 source_type 分桶，返回 {bucket_name: [article_indices]}。"""
    buckets: dict[str, list[int]] = {"academic": [], "social": []}
    for i, a in enumerate(articles):
        bucket = _classify_source(a.get("source_name", ""))
        buckets[bucket].append(i)
    return {k: v for k, v in buckets.items() if v}


# ── Academic: Cosine greedy ────────────────────────────────────
def _cluster_academic_cosine(
    articles: list[dict], indices: list[int], threshold: float
) -> dict[int, list[int]]:
    """Cosine average-linkage greedy for academic papers."""
    model = _get_model()
    if model is None:
        return {i: [idx] for i, idx in enumerate(indices)}

    texts = [
        f"{articles[idx].get('title', '')} {(articles[idx].get('summary', articles[idx].get('content_preview', '')) or '')[:500]}"
        for idx in indices
    ]
    texts = _truncate_texts(texts, model)
    vecs = np.stack([np.array(e, dtype=np.float32) for e in model.embed(texts)])

    # L2 normalize
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.where(norms < 1e-10, 1.0, norms)
    vecs = vecs / norms

    max_l2_sq = 2.0 * (1.0 - threshold)
    clusters: dict[int, list[int]] = {}  # local indices (0..len-1)
    next_cid = 0

    for i in range(len(vecs)):
        best_cluster = -1
        best_mean_l2 = float("inf")
        for cid, local_indices in clusters.items():
            diffs = vecs[i] - vecs[local_indices]
            mean_l2_sq = np.mean(np.einsum("ij,ij->i", diffs, diffs))
            if mean_l2_sq <= max_l2_sq and mean_l2_sq < best_mean_l2:
                best_mean_l2 = mean_l2_sq
                best_cluster = cid
        if best_cluster >= 0:
            clusters[best_cluster].append(i)
        else:
            clusters[next_cid] = [i]
            next_cid += 1

    # Map local indices → global indices
    return {cid: [indices[li] for li in lis] for cid, lis in clusters.items()}


# ── Social: BERTopic ───────────────────────────────────────────
def _cluster_social_bertopic(
    articles: list[dict], indices: list[int], cfg: dict
) -> dict[int, list[int]]:
    """BERTopic (UMAP+HDBSCAN) for social/media articles."""
    import jieba
    import umap
    import hdbscan
    from sklearn.feature_extraction.text import CountVectorizer
    from bertopic import BERTopic
    from bertopic.backend import FastEmbedBackend

    def _jieba_tok(text: str):
        return [
            w.strip()
            for w in jieba.lcut(text)
            if len(w.strip()) > 1 and w.strip().isprintable()
            and not all(not c.isalnum() for c in w)
        ]

    docs = [
        articles[idx].get("title", "") + "\n" + (articles[idx].get("summary", "") or "")
        for idx in indices
    ]

    vec_model = CountVectorizer(
        tokenizer=_jieba_tok, token_pattern=None,
        ngram_range=(1, 2), max_df=0.95, min_df=1,
    )

    emb_backend = FastEmbedBackend(embedding_model=DEFAULT_MODEL)

    umap_m = umap.UMAP(
        n_neighbors=cfg["n_neighbors"],
        n_components=cfg["n_components"],
        metric="cosine",
        random_state=42,
        min_dist=cfg["min_dist"],
    )

    hdb_m = hdbscan.HDBSCAN(
        min_cluster_size=cfg["min_cluster_size"],
        metric="euclidean",
        cluster_selection_method="eom",
        prediction_data=True,
    )

    bm = BERTopic(
        embedding_model=emb_backend,
        umap_model=umap_m,
        hdbscan_model=hdb_m,
        vectorizer_model=vec_model,
        min_topic_size=cfg["min_cluster_size"],
        nr_topics=None,
        calculate_probabilities=False,
        verbose=False,
    )

    topics, _ = bm.fit_transform(docs)

    clusters: dict[int, list[int]] = {}
    next_cid = 0
    for i, label in enumerate(topics):
        if label == -1:
            clusters[next_cid] = [indices[i]]
            next_cid += 1
        else:
            clusters.setdefault(int(label), []).append(indices[i])

    return clusters


# ── 主聚类入口 ───────────────────────────────────────────────────
def cluster_articles(
    articles: list[dict[str, Any]],
    threshold: float = 0.35,  # deprecated, kept for backward compat
) -> tuple[list[Event], np.ndarray]:
    """
    聚类文章，返回 (Event 列表, 全部 embedding 数组)。

    Hybrid: academic → cosine greedy, social → BERTopic.
    """
    if not articles:
        return [], np.zeros((0, 0), dtype=np.float32)

    n = len(articles)

    # 1. 全量 embedding (for fallback / scoring)
    texts = [
        f"{a.get('title', '')} {(a.get('summary', a.get('content_preview', '')) or '')[:500]}"
        for a in articles
    ]

    model = _get_model()
    if model is None:
        logger.warning("No embedding model — each article becomes its own Event")
        embeddings = np.zeros((n, 0), dtype=np.float32)
        return [
            _single_event(i, a) for i, a in enumerate(articles)
        ], embeddings

    texts = _truncate_texts(texts, model)
    embeddings = np.stack(
        [np.array(e, dtype=np.float32) for e in model.embed(texts)]
    )

    # 2. 分桶
    buckets = _split_buckets(articles)
    logger.info(
        f"Bucket split: academic={len(buckets.get('academic', []))}, "
        f"social={len(buckets.get('social', []))}"
    )

    # 3. 各桶聚类
    all_clusters: list[tuple[str, str, list[int]]] = []  # (bucket, method, indices)

    if "academic" in buckets:
        logger.info(
            f"Academic clustering: {len(buckets['academic'])} articles "
            f"(cosine greedy, threshold={ACADEMIC_THRESHOLD})"
        )
        ac = _cluster_academic_cosine(articles, buckets["academic"], ACADEMIC_THRESHOLD)
        for cid, indices in ac.items():
            all_clusters.append(("academic", "cosine-greedy", indices))
        logger.info(f"Academic → {len(ac)} clusters")

    if "social" in buckets:
        logger.info(
            f"Social clustering: {len(buckets['social'])} articles "
            f"(BERTopic, nn={SOCIAL_CONFIG['n_neighbors']}, "
            f"dim={SOCIAL_CONFIG['n_components']}, mcs={SOCIAL_CONFIG['min_cluster_size']})"
        )
        sc = _cluster_social_bertopic(articles, buckets["social"], SOCIAL_CONFIG)
        for cid, indices in sc.items():
            all_clusters.append(("social", "bertopic", indices))
        logger.info(f"Social → {len(sc)} clusters")

    # 4. 构建 Event
    events = []
    for bucket_name, method, indices in all_clusters:
        evt = _build_event(articles, indices)
        evt.bucket = bucket_name
        evt.method = method
        events.append(evt)

    events.sort(key=lambda e: e.importance, reverse=True)

    logger.info(f"Clustering done: {n} articles → {len(events)} events")
    return events, embeddings


# ── Truncate texts ─────────────────────────────────────────────
def _truncate_texts(texts: list[str], model: Any) -> list[str]:
    """用 fastembed 模型的 tokenizer 截断到窗口大小。"""
    try:
        from tokenizers import Tokenizer

        tokenizer = None
        cache_dir = getattr(model, "cache_dir", None)
        if cache_dir:
            import os

            for root, _dirs, files in os.walk(str(cache_dir)):
                if "tokenizer.json" in files:
                    tokenizer = Tokenizer.from_file(
                        os.path.join(root, "tokenizer.json")
                    )
                    break

        if tokenizer is not None:
            model_obj = getattr(model, "_model", None)
            max_length = 512
            if model_obj is not None:
                cfg = getattr(model_obj, "config", None)
                if cfg is not None:
                    max_length = getattr(cfg, "max_position_embeddings", 512)

            truncated = []
            for text in texts:
                encoded = tokenizer.encode(
                    text, add_special_tokens=True,
                    max_length=max_length, truncation=True,
                )
                truncated.append(
                    tokenizer.decode(encoded.ids, skip_special_tokens=True)
                )
            return truncated
    except Exception as e:
        logger.warning(f"Tokenizer truncation failed: {e}")
    return texts


# ── Event helpers ──────────────────────────────────────────────
def _single_event(index: int, article: dict[str, Any]) -> Event:
    return Event(
        id=f"evt_single_{index}",
        title=article.get("title", ""),
        summary=(
            article.get("summary", article.get("content_preview", "")) or ""
        )[:800],
        category=article.get(
            "category", article.get("primary_category", "")
        ),
        importance=article.get("priority_score", 5) / 10.0,
        article_indices=[index],
        representative_score=article.get("priority_score", 5),
    )


def _build_event(
    articles: list[dict[str, Any]], indices: list[int]
) -> Event:
    """从一簇文章构建 Event 骨架（元数据由 Event Curator 的 LLM 完善）。"""
    cluster = [articles[i] for i in indices]

    # 代表性文章：最高 priority_score
    rep = max(cluster, key=lambda a: a.get("priority_score", 5))

    # Category: 众数
    from collections import Counter

    cats = [
        a.get("category", a.get("primary_category", ""))
        for a in cluster
        if a.get("category") or a.get("primary_category")
    ]
    category = (
        Counter(cats).most_common(1)[0][0] if cats else "Uncategorized"
    )

    # Importance: 数量 + 质量
    scores = [a.get("priority_score", 5) for a in cluster]
    avg_score = np.mean(scores) if scores else 5
    count_factor = min(len(cluster) / 3, 1.0)
    importance = min((avg_score / 10 * 0.6) + (count_factor * 0.4), 1.0)

    # Summary: 代表性文章摘要
    summary = (rep.get("summary", rep.get("content_preview", "")) or "")[:600]
    if len(cluster) > 1:
        sources = set(
            a.get("source_name", a.get("source_id", "")) for a in cluster
        )
        summary += f"\n\n({len(cluster)} sources: {', '.join(sorted(sources))})"

    return Event(
        id=f"evt_{''.join(str(i) for i in indices[:5])}",
        title=rep.get("title", ""),
        summary=summary,
        category=category,
        importance=round(importance, 3),
        article_indices=indices,
        representative_score=rep.get("priority_score", 5),
    )


def pick_nearest_to_centroid(
    article_indices: list[int],
    embeddings: np.ndarray,
    top_k: int = 3,
) -> list[int]:
    """
    向量中心点算法：从簇中选出距离中心最近的 top_k 篇文章索引。

    用于 LLM 未返回有效 top_articles 时的兜底。
    """
    if len(article_indices) <= top_k or embeddings.size == 0:
        return article_indices[:top_k]

    cluster_vecs = embeddings[article_indices]
    centroid = np.mean(cluster_vecs, axis=0)
    # L2 normalize centroid
    norm = np.linalg.norm(centroid)
    if norm < 1e-10:
        return article_indices[:top_k]
    centroid /= norm

    # 距离中心的 L2 距离（越小越接近中心）
    distances = []
    for i, idx in enumerate(article_indices):
        diff = cluster_vecs[i] - centroid
        distances.append((np.dot(diff, diff), idx))

    distances.sort()
    return [idx for _, idx in distances[:top_k]]
