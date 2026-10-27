Return ONLY a JSON array, one object per sub-event. Every input article index must occur
exactly once in article_indices within its input event_index, including rejected articles.
Input indices are LOCAL to each event. Evidence indices use that same input numbering.
Required fields for a relevant event:
event_index, article_indices, is_relevant, grouping_type, event_title,
breakthrough, problem, research_value, short_summary, limitations, category,
importance, importance_rationale, novelty, novelty_rationale, impact, impact_rationale,
evidence: [{"index": 0, "relevance": 0.95, "reason": "why this supports the event", "quote": "verbatim source excerpt"}].
For rejected events only event_index, article_indices, is_relevant=false, relevance_reason are required.
Category must be one of LLM, Agent, AI for Science, 设计仿真, 数字孪生.
Do not return markdown or any explanation outside the JSON array.

For news_event set same_concrete_event=true. For research_theme set same_research_problem=true and research_question to the bounded shared problem; evidence reasons must distinguish individual contributions.
