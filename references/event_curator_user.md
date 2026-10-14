# Event Curator — User Prompt Template

## Input format

Batch of {batch_size} Events:

```
{events}
```

## Output format — JSON array:

- Default: one object per input event (no `article_indices` field).
- Split (per Step 0): output ONE OBJECT PER SUB-EVENT. All sub-event objects
  for the same input event share the SAME `event_index`, and each carries an
  `article_indices` field: the 0-based article indices (as shown in input)
  belonging to that sub-event. Together they must cover ALL articles of the
  input event, without overlap.

```json
[
  {{
    "event_index": 0,
    "is_relevant": true,
    "event_title": "≤30字中文标题，陈述具体技术内容（方法/结果），禁止新闻体与原文没有的细节",
    "event_summary": "3-5句话中文总结，约150-300字，覆盖核心内容、方法/方案与意义",
    "category": "LLM|Agent|AI for Science|Design Simulation|Digital Twin",
    "importance": 0.75,
    "importance_rationale": "模型架构级创新, 效率提升20%",
    "novelty": 0.6,
    "novelty_rationale": "类似方法已有, 但迁移到新领域",
    "impact": 0.8,
    "impact_rationale": "影响多个下游任务和应用场景",
    "key_insights": ["要点1", "要点2"],
    "tags": ["标签1", "标签2"],
    "top_articles": [2, 0, 1],
    "evidence_articles": [2, 0]
  }},
  {{
    "event_index": 0,
    "article_indices": [5],
    "is_relevant": true,
    "event_title": "（拆出的子事件，字段同上）",
    "top_articles": [5],
    "evidence_articles": [5]
  }}
]
```

## Rules

- `top_articles`: Indices of the 3 best articles (0-based as shown in input).
  If fewer than 3 exist, list all. If is_relevant=false, set [].
  For SPLIT sub-events: indices must come from that sub-event's
  `article_indices` only, and cover its most informative articles (≤3).
- `evidence_articles`: The article indices (from input sample) that SUPPORT
  your scores. Must be a subset of the indices shown above. Max 3.
  Backend WILL reject IDs not in the sample list.
- Scores are FLOATS 0-1. Each dimension must have a ≤20-word rationale.
- `event_summary` and `key_insights` must reference ONLY the Top-3 articles.
  Do not mention details that appear only in non-selected articles.
- `category`: Output exactly ONE category from the five options.
