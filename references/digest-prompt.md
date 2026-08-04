# Digest Prompt — Output Format Specification

## Output Structure
Every article must be output as a single JSON object with all fields populated. Missing fields cause the article to be dropped from the digest.

## Required Fields

| Field | Type | Description | Constraints |
|-------|------|-------------|-------------|
| `is_relevant` | boolean | Whether article belongs to any of the 5 domains | Must be `true` for processing |
| `priority_score` | integer 1-5 | Importance relative to other articles this week | LLM/Agent content boosted by +1 minimum |
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
| 5 | Paradigm-shifting breakthrough, new SOTA by wide margin, major acquisition/funding | GPT-5 architecture reveal, AlphaFold 3, $1B+ funding |
| 4 | Significant new model/framework release, top-conference Best Paper, major partnership | Claude 4 release, LangChain v1.0, NeurIPS oral |
| 3 | Notable improvement, new benchmark, company strategy announcement | Mixtral fine-tune, new eval dataset, product launch |
| 2 | Incremental update, opinion piece from authority, tutorial with insight | Blog post by Karpathy, minor version bump |
| 1 | General news, roundup, low-signal announcement | "Company X announces AI strategy" without details |

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
