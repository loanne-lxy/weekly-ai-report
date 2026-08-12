# 分类与评分优化方案 — 四层漏斗架构

## 设计原则

1. **对象永远是文章，不是源** — 源的 `default_category` 仅作为 Bayesian prior 传给 LLM
2. **失败安全** — 每层 fallback 到现有流程，绝不因优化导致周报空白
3. **不改 curator 核心** — `_curate_async()` 保持原样，只做前置过滤 + 后置精排

---

## L0: 黑名单过滤（Python 正则，零 Token）

**位置：** `main.py` Phase 2 和 Phase 2.5 之间

```python
# config.yaml 新增
filter:
  blacklist_keywords:
    - "融资"
    - "融资成功"
    - "上市"
    - "股票"
    - "涨停"
    - "小白教程"
    - "零基础"
    - "套壳"
    - "SEO"
    - "NFT"
    - "crypto"
```

- 匹配字段：`title + summary[:200]` 转小写
- 匹配即丢弃，不进入任何 LLM 阶段
- 预计过滤 5–15% 的噪音

---

## L1: 轻量预分类

**位置：** 新建 `filter/preclassifier.py`

**LLM 调用参数：**
```python
temperature = 0.0
max_tokens = 128
model = "Qwen3.6-27B"  # 同一模型
```

**输入 prompt（~2KB，远小于 curator 的 ~7KB）：**
```
你是一个 AI 资讯分类器。判断以下文章是否属于以下五个领域之一：
LLM | Agent | AI for Science | Design Simulation | Digital Twin

{optional: Source specializes in: {default_category}}

标题: {title}
摘要: {summary[:300]}

请先简要分析这篇文章的核心主题，然后给出分类。
返回 JSON:
{{
  "reasoning": "1-2 句话说明为什么相关或不相关",
  "is_relevant": true/false,
  "category": "LLM|Agent|AI for Science|Design Simulation|Digital Twin"
}}
```

**输出 Pydantic schema：**
```python
from pydantic import BaseModel

class PreClassificationResult(BaseModel):
    reasoning: str
    is_relevant: bool
    category: str
```

**并发策略：** `concurrency=5`（ curator 是 3，预分类输入更小可以更多）
**预计耗时：** ~185 篇 × 0.3s / 5 = ~11s 排队 + ~55s 处理 ≈ 1–2 分钟

**guided decoding 尝试（可选）：**
```python
response = self.client.chat.completions.create(
    model=self.model,
    messages=[...],
    temperature=0.0,
    max_tokens=128,
    extra_body={"guided_json": PreClassificationResult.model_json_schema()},
)
```
如果 `extra_body` 不被支持 → fallback 普通 JSON 解析

---

## L2: LLM Curator（现有，微调）

**改动：**
1. 在 `_ARTICLE_TEMPLATE` 中加入 L1 预分类结果：
   ```
   Source: {source_name}{prior_hint}
   Pre-classified as: {l1_category} (use as prior, override if content disagrees)
   ```
2. 传入的文章已经经过 L0 + L1 过滤，数量从 231 降到 ~60

**预计耗时：** ~60 篇 / 3 批次 × 30s ≈ 6 分钟（vs 原来 14 分钟）

---

## L3: Top-K 精排

**位置：** 新建 `filter/topk_reranker.py`

**Phase：** 在 curator 之后、merge 之前

**流程：**
```
50+ 篇策展结果
    │
    ├─ Pointwise 筛选：priority_score >= 8.0 → ~5 篇
    │  (如果 < 3 篇则降到 7.5，再 < 3 篇降到 7.0)
    │
    └─ Pairwise 精排：对 Top-K 做 C(K,2) 次成对比较
          每次输入：文章 A 的 title + tldr vs 文章 B 的 title + tldr
          输出：{winner: "A"/"B", reason: "..."}
          基于胜负关系生成最终排名

输出：最终 priority_score 微调 + rank 字段
```

**Pairwise prompt（~1KB）：**
```
以下两篇文章哪篇对本周读者更重要？考虑：
- 技术突破性
- 对行业的实际影响
- 内容的原创性（非 PR/转载）

文章 A: {title_a}
{tldr_a}

文章 B: {title_b}
{tldr_b}

返回 JSON: {{"winner": "A" or "B", "reason": "一句话"}}
```

**并发：** `concurrency=5`，10 次 × 2s / 5 = ~4s

---

## 在 main.py 中的位置

```python
# Phase 2.5: Semantic Dedup (已有)
articles = sdd.filter(articles)

# ── 新增 ──
# L0: Blacklist filter
articles = BlacklistFilter(config).filter(articles)

# L1: Pre-classification (filter irrelevant early)
preclassifier = Preclassifier(llm)
articles = preclassifier.filter(articles)

# ── 已有 ──
# Phase 3: LLM Curator (接收预过滤 + 预分类 prior)
articles = curator.curate_batch(articles)

# ── 新增 ──
# L3: Top-K reranking (精排头部文章)
articles = TopKReranker(llm).rerank(articles)
```

---

## 数据流示意

```
source → RawArticle
             │
             ▼ default_category (optional, from source)
        IngestionManager
             │
             ▼
        Deduplicator (Phase 2)
             │
             ▼
      ┌─────┴──────┐
      │ L0 黑名单   │  → 丢弃噪音 (~15%)
      └─────┬──────┘
            ▼
      ┌─────┴──────┐
      │ L1 预分类   │  → is_relevant=false 丢弃 (~40%)
      │ + prior    │  → is_relevant=true 携带 category prior
      └─────┬──────┘
            ▼
      ┌─────┴──────┐
      │ L2 Curator  │  → 完整策展 (评分 + 摘要 + 标签)
      │ + prior     │
      └─────┬──────┘
            ▼
      ┌─────┴──────┐
      │ L3 精排     │  → Top-K 微调分数 + rank
      └─────┬──────┘
            ▼
         Phase 4: Merge + Phase 5: Generate
```

---

## 风险评估

| 风险 | 概率 | 缓解 |
|------|------|------|
| guided decoding 不支持 | 中 | fallback 普通 JSON |
| 预分类误杀（is_relevant=false 但实际相关） | 低 | curator 可以覆盖，且预分类只影响效率不影响 curator 质量 |
| 精排 Pairwise 结果矛盾（A>B, B>C, C>A） | 低 | 用投票/Condorcet 方法处理循环 |
| L1 调用失败（网络/超时） | 低 | fallback 全部进 curator，和现在一样 |
