"""语义去重 — FAISS 向量检索 + 多语言模型 + tokenizer 截断

流程:
  1. 构建 embedding 文本 (title + summary)
  2. 按模型 tokenizer 截断到窗口大小 (通常 512 tokens)
  3. FAISS IndexFlatL2 检索 top-1 最近邻
  4. 相似度 >= 阈值 → 判重复
  5. 无 LLM 裁决 (纯向量, 秒级完成)

模型: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
  - 50+ 语言, 384 dim, 80MB
  - 中英文混合场景下比 bge-small-zh 更公平
"""
import logging
from typing import Any, List

import numpy as np
from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class SemanticDeduplicator:
    """FAISS 语义去重, O(n log n)."""

    def __init__(
        self,
        threshold: float = 0.85,
        model_name: str = DEFAULT_MODEL,
    ):
        self.threshold = threshold
        logger.info(f"Loading embedding model: {model_name}...")
        self.embed_model = TextEmbedding(model_name=model_name)
        logger.info("Embedding model loaded.")
        self._tokenizer = self._load_tokenizer()
        self._faiss = self._try_faiss()

    def _load_tokenizer(self) -> Any:
        """Load tokenizer from the fastembed model cache for truncation."""
        try:
            from tokenizers import Tokenizer

            # fastembed stores the model in a cache dir
            cache_dir = getattr(self.embed_model, "cache_dir", None)
            if cache_dir:
                return Tokenizer.from_file(str(cache_dir / "tokenizer.json"))

            # Fallback: try to find tokenizer.json in model path
            import os
            for root, dirs, files in os.walk(getattr(self.embed_model, "model_dir", cache_dir or "")):
                if "tokenizer.json" in files:
                    return Tokenizer.from_file(os.path.join(root, "tokenizer.json"))
        except Exception as e:
            logger.warning(f"Could not load tokenizer for truncation: {e}")
        return None

    def _truncate_texts(self, texts: List[str]) -> List[str]:
        """Truncate texts to model's token window using tokenizer."""
        if self._tokenizer is None:
            # No tokenizer available — fastembed handles truncation internally
            return texts

        # Get max length from model config
        model = getattr(self.embed_model, "_model", None)
        if model is not None:
            max_length = getattr(getattr(model, "config", None), "max_position_embeddings", 512)
        else:
            max_length = 512

        truncated = []
        for text in texts:
            encoded = self._tokenizer.encode(
                text,
                add_special_tokens=True,
                max_length=max_length,
                truncation=True,
            )
            # Decode back to text
            decoded = self._tokenizer.decode(encoded.ids, skip_special_tokens=True)
            truncated.append(decoded)
        return truncated

    def _try_faiss(self):
        """Try to import FAISS, fall back to None if unavailable."""
        try:
            import faiss  # noqa: F401
            logger.info("FAISS available — using vector index for O(n log n) dedup")
            return faiss
        except ImportError:
            logger.warning(
                "FAISS not installed — falling back to O(n²) numpy cosine search. "
                "Install: pip install faiss-cpu"
            )
            return None

    def filter(self, articles: List[dict]) -> List[dict]:
        """Semantic dedup via vector similarity."""
        if not articles:
            return []

        # Build raw text: title + summary
        raw_texts = [
            f"{a.get('title', '')} {a.get('summary', '')}" for a in articles
        ]

        # Truncate to model token window
        texts = self._truncate_texts(raw_texts)
        n = len(texts)

        logger.info(f"Embedding {n} articles for semantic dedup...")
        embeddings = list(self.embed_model.embed(texts))
        dim = len(embeddings[0])

        unique_articles: List[dict] = []
        skipped_hard = 0

        if self._faiss is not None:
            # ── FAISS: O(n log m) per search ──
            index = self._faiss.IndexFlatL2(dim)

            for article, vec in zip(articles, embeddings):
                vec = np.asarray(vec, dtype=np.float32)
                norm = np.linalg.norm(vec)
                if norm < 1e-10:
                    unique_articles.append(article)
                    continue

                vec /= norm  # L2 normalize for cosine similarity
                vec_arr = vec.reshape(1, -1)

                if index.ntotal == 0:
                    index.add(vec_arr)
                    unique_articles.append(article)
                else:
                    distances, _ = index.search(vec_arr, 1)
                    nearest_dist = distances[0][0]
                    cosine_sim = 1.0 - (nearest_dist * nearest_dist) / 2.0

                    if cosine_sim >= self.threshold:
                        skipped_hard += 1
                    else:
                        index.add(vec_arr)
                        unique_articles.append(article)
        else:
            # ── Fallback: O(n²) numpy ──
            kept_vecs: List[np.ndarray] = []
            for article, vec in zip(articles, embeddings):
                vec = np.asarray(vec, dtype=np.float32)
                is_dup = False
                for kept in kept_vecs:
                    sim = self._cosine(vec, kept)
                    if sim >= self.threshold:
                        is_dup = True
                        skipped_hard += 1
                        break
                if not is_dup:
                    unique_articles.append(article)
                    kept_vecs.append(vec)

        logger.info(
            f"语义去重: {n} → {len(unique_articles)} "
            f"(向量重复{skipped_hard})"
        )
        return unique_articles

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-10 or nb < 1e-10:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
