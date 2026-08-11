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
| `chinese_title` | string | Clear, complete Chinese title generated from article body/summary | 15-35 chars, states the core technical contribution or finding, zero marketing language. See Title Quality Standards below. |
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

## Title Quality Standards

### Generation Process
1. Read the full summary/body content first
2. Identify: WHO did WHAT using HOW, achieving WHAT RESULT
3. Compose a title that states the core fact directly
4. Strip ALL marketing/clickbait language before composing

### Format Rules
- Length: 15-35 Chinese characters — must be substantive, not too short
- Structure: [Subject/Entity] + [Action/Contribution] + [Key Metric/Result]
- Must be self-contained — reader understands the article from title alone
- Use Chinese technical terminology (e.g., "多模态", "推理", "微调", "开源")

### Banned Patterns
- Marketing hype: 震撼, 颠覆, 重磅, 惊艳, 革命性
- Vague placeholders: 全新, 重大, 最新, 重磅发布
- Clickbait: 竟然, 揭秘, 终于, 史上首次 (unless genuinely first)
- Reproducing the original English title verbatim
- Overly short titles (<10 chars) that lack context

### Good Examples
- "AutoGen 0.7.1 支持嵌套团队编排与 Redis 持久化存储"
- "OpenAI 发布 GPT-5.6 高效架构，推理成本降低 40%"
- "Transformers 5.13.0 原生集成 KimiK 2.5/2.6/2.7 模型族"

### Bad Examples
- "OpenAI 重磅发布！" (too vague, marketing)
- "Transformers 更新" (too short, no info)
- "mattpocock/skills" (just the repo name, no context)
- "惊艳！AI 智能体新突破" (clickbait, no substance)

## Tag Standards
Use these canonical tags where applicable:
- Model types: MoE, Transformer, Diffusion, GNN, RLHF
- Techniques: Fine-tuning, Quantization, Distillation, Pruning
- Domains: MultiModal, CodeGen, Reasoning, Alignment
- Agent: MCP, ToolCalling, RAG, MultiAgent, Planning
- Science: AlphaFold, MolecularDynamics, PINN, DrugDiscovery
- Engineering: GenerativeDesign, CAD, Simulation, CFD, Omniverse
- Infrastructure: CUDA, Inference, Serving, Orchestration
