# Weekly AI Report

Autonomous agent that fetches AI news from multi-source feeds, filters, classifies, scores, and generates a weekly report webpage — auto-deployed to GitHub Pages.

**Live:** https://loanne-lxy.github.io/weekly-ai-report/

## Pipeline

```
Sources (34)
  │
  ├── Phase 1:   Fetch (RSS / GitHub / Web, asyncio concurrency=10)
  ├── Phase 2:   Hard Dedup (md5 url+source+date, SQLite)
  ├── Phase 2.3: Blacklist Filter (keyword regex, zero-token)
  ├── Phase 2.5: Semantic Dedup (FastEmbed vector, cosine ≥0.88)
  ├── Phase 3:   LLM Curator (classify + score + summarize, batch=5)
  ├── Phase 3.5: Top-K Rerank (pairwise comparison for top articles)
  ├── Phase 4:   Merge (cumulative per week, carry-over fallback)
  ├── Phase 6:   Source Eval & Discovery (LLM + link miner + Exa search)
  └── Phase 5:   Generate Report (Jinja2 HTML, 5 categories)
```

## Quick Start

```bash
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env  # set DEEPSEEK_API_KEY, GITHUB_TOKEN, EXA_API_KEY

# Run
python main.py
```

Output: `output/<YEAR>-W<WEEK>/index.html` — open in browser or push for GitHub Pages deploy.

## Project Structure

```
weekly-ai-report/
├── main.py                  # End-to-end pipeline orchestrator
├── config.yaml              # Model, filter, evaluator settings
├── sources.yaml             # 34 source definitions with default_category
├── .env                     # API keys (DEEPSEEK_API_KEY, GITHUB_TOKEN, EXA_API_KEY)
├── .env.example             # Template with all required keys
├── scheduler.py             # Cron entry point (optional)
│
├── fetcher/
│   ├── base_extractor.py    # Base extractor interface
│   ├── extractors.py        # RSS / GitHub / Web / arXiv / HF extractors
│   └── ingestion_manager.py # Async orchestrator, concurrency control
│
├── dedup/
│   ├── deduplicator.py      # URL+source+date bucket dedup (SQLite)
│   ├── semantic_deduplicator.py  # FastEmbed vector dedup + LLM gray-zone
│   └── curator_cache.py     # SHA256 LLM result cache + prompt versioning
│
├── filter/
│   ├── blacklist_filter.py  # Zero-token keyword pre-filter
│   ├── filter_summarizer.py # LLM curator (batch classification + scoring)
│   ├── topk_reranker.py     # Pairwise reranking for top articles
│   ├── link_miner.py        # Outbound link extraction from high-score articles
│   └── exa_discoverer.py    # Exa API neural search for source discovery
│
├── extractors/
│   └── contract.py          # RawArticle / CuratedArticle Pydantic schemas
│
├── generator/
│   ├── report_generator.py  # Jinja2 HTML report (index + 5 category pages)
│   └── templates/
│       ├── index.html       # Homepage with sidebar stats & source dynamics
│       └── category.html    # Category sub-pages with pagination
│
├── evaluator/
│   ├── source_evaluator.py  # Weekly source scoring, archival (stale_weeks=4)
│   └── source_discoverer.py # LLM-based new source recommendations
│
├── models/
│   └── llm_client.py        # OpenAI-compatible client, langsmith tracing
│
├── references/
│   ├── curation-rules.md    # LLM curation rules (SHA256-versioned)
│   ├── digest-prompt.md     # LLM prompt template (SHA256-versioned)
│   └── classification-scoring-design.md  # Pipeline architecture doc
│
└── output/
    └── <YEAR>-W<WEEK>/      # Generated HTML + articles.json
```

## Categories

| Category | Description |
|---|---|
| LLM | Core model tech (architecture, training, inference, evaluation) |
| Agent | AI agents (tool use, multi-agent, RAG, task planning) |
| AI for Science | AI-driven science (protein, drug, materials, physics) |
| Design Simulation | AI-assisted engineering (CAD, CAE, CFD, 3D generation) |
| Digital Twin | Industrial twin (IoT, real-time simulation, predictive maintenance) |

## Key Features

- **6-stage pipeline** — Fetch → Hard Dedup → Blacklist → Semantic Dedup → Curator → Rerank
- **Triple dedup** — Hard hash (md5) + keyword blacklist (zero-token) + semantic (FastEmbed cosine ≥0.88)
- **LLM caching** — SHA256 content hash + prompt versioning (auto-invalidate on prompt change)
- **Auto-retry** — If curator returns 0 and no accumulator, resets dedup and retries
- **Source prior** — `default_category` on vertical sources passed as Bayesian prior to LLM
- **Top-K rerank** — Pairwise comparison (C(K,2)) for top 5 articles after curator
- **Cumulative per week** — Multiple runs within same week merge, newest-first
- **Empty category fallback** — No new articles → carry over from up to 4 previous weeks
- **3-channel source discovery** — LLM recommendation + outbound link mining (zero-token) + Exa neural search
- **Source self-evolution** — Weekly eval + auto-archive stale sources (stale_weeks=4)
- **Frontend source dynamics** — Sidebar shows newly discovered / archived source counts

## Configuration

### Model (config.yaml)

```yaml
model:
  provider: custom
  name: Qwen3.6-27B
  base_url: http://your-endpoint/v1
  api_key: dummy  # read from .env DEEPSEEK_API_KEY
  temperature: 0.3
  max_tokens: 8192
  timeout: 600
```

### Sources (sources.yaml)

```yaml
sources:
  - name: OpenAI Blog
    url: https://openai.com/blog/rss.xml
    type: rss
    category: LLM
    weight: 9
    default_category: LLM    # Optional prior for vertical sources
```

Source types: `rss` | `web` | `github_trending` | `github_repo` | `arxiv` | `hf` | `wechat`

### Blacklist (config.yaml)

```yaml
filter:
  blacklist_keywords:
    - "融资"
    - "股票"
    - "小白教程"
    # ... add your own
```

## LangSmith Tracing

Set in `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=***
LANGSMITH_PROJECT=weekly-ai-report
```

## Cron

```bash
# Every Monday 8:00
# 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py
```

---

Built during internship at **TCL Research, Wuhan** · 2026
