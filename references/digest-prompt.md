# Digest Prompt — Output Format Specification

## Output Structure
Every article must be output as a single JSON object with all fields populated. Missing fields cause the article to be dropped from the digest.

## Required Fields

| Field | Type | Description | Constraints |
|-------|------|-------------|-------------|
| `is_relevant` | boolean | Whether article belongs to any of the 5 domains | Must be `true` for processing |
| `priority_score` | integer 1-10 | Importance relative to other articles this week | LLM/Agent content boosted by +2 minimum |
| `primary_category` | string | Main domain classification | One of: LLM, Agent, AI for Science, Design Simulation, Digital Twin |
| `secondary_category` | string or null | Cross-domain label if applicable | Optional |
| `chinese_title` | string | Accurate Chinese headline | Under 25 characters, fact-based, no clickbait |
| `tldr` | string | One-sentence summary | Under 50 English words or 80 Chinese characters |
| `key_insights` | string array | 1-3 critical bullet points | Each under 100 characters, start with actionable info |
| `why_it_matters` | string | Significance for industry/developers | 1-2 sentences, avoid generic fluff |
| `tags` | string array | 2-5 lowercase tags | Use standard terminology: MoE, RLHF, MCP, RAG, etc. |
| `original_title` | string | Original English title | Preserved for reference |
| `url` | string | Source URL | Must be preserved exactly as received |
| `source_name` | string | Publication/source name | Preserved from input |

## Category Classification Rules
- **Primary category** is the MAIN focus of the article — not a tangential mention
- **Secondary category** only if the article genuinely spans two domains (e.g., using Agents for drug discovery → primary: AI for Science, secondary: Agent)
- When uncertain between LLM and Agent, prefer: model releases → LLM, framework/tool releases → Agent

## Priority Scoring Rubric

| Score | Criteria | Example |
|-------|----------|---------|
| 10 | Paradigm-shifting breakthrough, new SOTA by wide margin, major acquisition/funding | GPT-5 architecture reveal, AlphaFold 3, $1B+ funding |
| 9 | Major model/framework release with broad impact, top-conference Best Paper | Claude 4 release, NeurIPS oral, major open-weight release |
| 8 | Significant new model/tool/method, important partnership or product launch | LangChain v1.0, Anthropic MCP protocol, OpenAI Realtime API |
| 7 | Notable improvement, new benchmark result, company strategy with substance | Mixtral fine-tune, new eval dataset, NVIDIA GPU roadmap |
| 6 | Good incremental update, solid feature addition, interesting research result | Karpathy blog post with code, arXiv paper with novel method |
| 5 | New release with modest impact, tooling update | Minor version bump with notable changes |
| 4 | Opinion piece with insight from authority, tutorial | Industry analysis by known researcher |
| 3 | General news with some technical detail | Product announcement with some technical content |
| 2 | Low-signal announcement, generic roundup | "Company X announces AI strategy" without detail |
| 1 | Marketing fluff, pure PR, no technical substance | Press release with no technical content |

## Chinese Title Guidelines
- Translate the core technical achievement, not the clickbait headline
- Include key numbers/metrics when relevant (e.g., "推理速度提升3倍")
- Avoid: sensational adjectives (震撼, 颠覆), vague terms (全新, 重大)
- Prefer: concrete subjects (架构, 基准, 协议) and precise descriptors

## Tag Standards
Use these canonical tags where applicable:
- Model types: MoE, Transformer, Diffusion, GNN, RLHF
- Techniques: Fine-tuning, Quantization, Distillation, Pruning
- Domains: MultiModal, CodeGen, Reasoning, Alignment
- Agent: MCP, ToolCalling, RAG, MultiAgent, Planning
- Science: AlphaFold, MolecularDynamics, PINN, DrugDiscovery
- Engineering: GenerativeDesign, CAD, Simulation, CFD, Omniverse
- Infrastructure: CUDA, Inference, Serving, Orchestration
