"""语义去重模块 — FastEmbed 向量相似度 + 双门槛 LLM 裁决
三级去重流水线：
  1. 硬哈希去重 (deduplicator.py) — URL + source_type + date_bucket
  2. 向量相似度 — FastEmbed (BAAI/bge-small-zh-v1.5) + cosine similarity
  3. 灰色地带 LLM 裁决 — 相似度 [0.80, 0.88) 时触发 YES/NO 判断
"""
import hashlib
import json
import logging
from typing import List
import numpy as np
from fastembed import TextEmbedding
from models.llm_client import LLMClient

logger = logging.getLogger(__name__)


class SemanticDeduplicator:
    """语义去重器 — 过滤同一事件的不同报道/转载。"""

    def __init__(
        self,
        llm: "LLMClient | None" = None,
        low_threshold: float = 0.80,
        high_threshold: float = 0.85,
        model_name: str = "BAAI/bge-small-zh-v1.5",
    ):
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold
        self.llm = llm
        logger.info(f"Loading embedding model: {model_name}...")
        self.embed_model = TextEmbedding(model_name=model_name)
        logger.info("Embedding model loaded.")

    @staticmethod
    def _cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 < 1e-10 or norm2 < 1e-10:
            return 0.0
        return float(np.dot(vec1, vec2) / (norm1 * norm2))

    def _llm_judge_duplicate(
        self, title_a: str, summary_a: str, title_b: str, summary_b: str
    ) -> bool:
        """灰色地带 LLM 裁决 — 仅当 0.80 <= similarity < 0.88 时触发。"""
        pair_key = hashlib.md5(f"{title_a}||{title_b}".encode()).hexdigest()
        cached = self._llm_cache.get(pair_key)
        if cached is not None:
            return cached

        prompt = (
            "你是一个极其严格的新闻去重审核员。请判断以下两篇文章报道的"
            "是否为【同一个核心事件/同一个开源项目发布/同一篇论文】。\n\n"
            f"文章A：{title_a}\n简介：{summary_a or '无'}\n\n"
            f"文章B：{title_b}\n简介：{summary_b or '无'}\n\n"
            "判断规则：\n"
            "1. 如果是不同机构/作者发布的类似主题，算【不同事件】(false)。\n"
            "2. 如果是同一事件的不同媒体转载、翻译或解读，算【同一事件】(true)。\n\n"
            "请仅返回JSON：{\"is_duplicate\": true/false, \"reason\": \"一句话\"}"
        )

        try:
            if not self.llm:
                return False
            response = self.llm.chat(
                "You are a strict news dedup reviewer.", prompt
            )
            cleaned = response.strip()
            for fence in ["```json", "```"]:
                cleaned = cleaned.removeprefix(fence).removesuffix(fence).strip()
            result = json.loads(cleaned)
            is_dup = bool(result.get("is_duplicate", False))
            reason = result.get("reason", "")
            logger.info(
                f"[LLM裁决] {'重复' if is_dup else '不重复'} | "
                f"《{title_a[:30]}》 vs 《{title_b[:30]}》 | {reason}"
            )
            self._llm_cache[pair_key] = is_dup
            return is_dup
        except Exception as e:
            logger.warning(f"LLM裁决失败，默认不重复: {e}")
            self._llm_cache[pair_key] = False
            return False

    def filter(self, articles: List[dict]) -> List[dict]:
        """Embed and compare via cosine similarity; gray zone falls back to LLM."""
        if not articles:
            return []

        # Build text for embedding: title + summary
        texts = [f"{a.get('title', '')} {a.get('summary', '')}" for a in articles]

        logger.info(f"Embedding {len(texts)} articles for semantic dedup...")
        embeddings = list(self.embed_model.embed(texts))

        unique_articles: List[dict] = []
        kept_indices: List[int] = []  # indices into articles/embeddings

        skipped_total = 0
        skipped_hard = 0
        skipped_llm = 0

        for idx, (article, new_vec) in enumerate(zip(articles, embeddings)):
            is_duplicate = False

            for ki in kept_indices:
                exist_vec = embeddings[ki]
                exist_article = articles[ki]
                exist_title = exist_article.get("title", "")
                exist_summary = exist_article.get("summary", "") or ""

                similarity = self._cosine_similarity(new_vec, exist_vec)

                if similarity >= self.high_threshold:
                    is_duplicate = True
                    skipped_hard += 1
                    if skipped_hard <= 5:
                        logger.debug(f"[硬重复 {similarity:.2f}] {article.get('title', '')[:40]}")
                    break

                elif similarity >= self.low_threshold:
                    summary_a = article.get("summary", "") or ""
                    if self.llm and self._llm_judge_duplicate(
                        article.get("title", ""), summary_a, exist_title, exist_summary
                    ):
                        skipped_llm += 1
                        break

            if not is_duplicate:
                unique_articles.append(article)
                kept_indices.append(idx)
            else:
                skipped_total += 1

        logger.info(
            f"语义去重: {len(articles)} → {len(unique_articles)} "
            f"(硬重复{skipped_hard}, LLM裁决去重{skipped_llm}, "
            f"共跳过{skipped_total})"
        )

        return unique_articles

    @property
    def _llm_cache(self) -> dict:
        if not hasattr(self, "_cache"):
            self._cache = {}
        return self._cache
