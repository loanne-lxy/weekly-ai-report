# Curation Rules — Filtering & Quality Standards

## Noise Filtering
- **Reject immediately**: Pure PR/promotional material, marketing hype without technical details, crypto/NFT spam, generic listicles, SEO bait
- **Flag as low quality**: Articles under 200 words, no named sources, no original analysis
- **Deduplicate**: If multiple sources cover the same announcement, keep the most authoritative source and discard the rest

## Source Authority Tiers
| Tier | Weight | Examples | Action |
|------|--------|----------|--------|
| 1 — Primary | ×3 | Official blog (OpenAI, DeepMind, Meta AI), arXiv papers, top conference proceedings | Always include, boost priority |
| 2 — Authoritative | ×2 | Established tech media (TechCrunch, VentureBeat, The Verge), NVIDIA/Siemens/Ansys dev blogs | Include if contains technical detail |
| 3 — Community | ×1 | Nitter RSS (individual researchers), Medium posts, Reddit r/MachineLearning, Hacker News | Include only if unique insight |
| 4 — Low signal | ×0 | Aggregator sites, content farms, automatic translations | Skip |

## Content Requirements
- Must contain at least one of: new release, benchmark result, architectural detail, code/dataset publication, strategic announcement, research finding
- Summary must reference a verifiable URL from the source's own domain
- If the article is behind a paywall with no public abstract → skip

## Domain-Specific Filters
- **LLM**: Technical detail required — skip pure product announcements and generic "AI will change X" essays
- **Agent**: Framework/tool release OR research paper → skip "what is an AI agent" explainers
- **AI for Science**: Published result required — skip speculative "could help" articles
- **Design Simulation**: Tool/demo/benchmark OR case study — skip generic trend pieces
- **Digital Twin**: Architecture/case study/standard — skip "digital twin 101" content
