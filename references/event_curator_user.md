# Event Curator — User Prompt Template

## Input format

Batch of {batch_size} Events:

```
{events}
```

## Output format — JSON array, one object per event:

```json
[
  {{
    "event_index": 0,
    "is_relevant": true,
    "event_title": "简洁的中文标题",
    "event_summary": "1-2句话中文总结核心内容",
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
  }}
]
```

## Rules

- `top_articles`: Indices of the 3 best articles (0-based as shown in input).
  If fewer than 3 exist, list all. If is_relevant=false, set [].
- `evidence_articles`: The article indices (from input sample) that SUPPORT
  your scores. Must be a subset of the indices shown above. Max 3.
  Backend WILL reject IDs not in the sample list.
- Scores are FLOATS 0-1. Each dimension must have a ≤20-word rationale.
- `event_summary` and `key_insights` must reference ONLY the Top-3 articles.
  Do not mention details that appear only in non-selected articles.
- `category`: Output exactly ONE category from the five options.
