# Weekly AI Report

Autonomous agent that fetches AI news from multi-source feeds, filters, classifies, scores, and generates a weekly report webpage — auto-deployed to GitHub Pages.

**Live:** https://loanne-lxy.github.io/weekly-ai-report/

## Pipeline

```
Sources (source.db, DB-first)
  │
  ├── Phase 1:   Fetch (RSS / GitHub / arXiv, connector registry, asyncio concurrency=10)
  ├── Phase 2:   Hard Dedup (URL Registry, md5)
  ├── Phase 3:   Blacklist Filter (keyword regex, zero-token)
  ├── Phase 4:   LLM Curator (classify + event extract + scoring, batch=5)
  ├── Phase 4.5: Event Dedup (FastEmbed + faiss cosine ≥0.70)
  ├── Phase 5:   Event Clustering (FastEmbed + sentence_cosine greedy)
  ├── Phase 6:   Scoring & Ranking (time-weighted, top_n=50 per category)
  └── Phase 7:   Generate Report (Jinja2 HTML, 5 categories)
```

**Changes since v12 (2026-08):**
- DB-first source registry — `source.db` is the single source of truth; `sources.yaml` is seed only
- Removed article-level semantic dedup (URL Registry alone is sufficient, 0 removed in consecutive runs)
- Event-level dedup at cosine ≥ 0.70 (reduced from 0.75 to prevent false positives)
- Removed `trust`/`weight` fields — only `eval_score` matters at runtime
- Unified embedding via `embedding_model.py` (MiniLM ONNX, offline-capable)
- arXiv post-filter with group-based include (e.g. `[["cs.AI","cs.CL"],["cs.CV"]]`)

## Quick Start

```bash
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env  # set QWEN_API_KEY, GITHUB_TOKEN, EXA_API_KEY

# Run
python main.py

# Debug reset
python main.py --reset
```

Output: `output/<YEAR>-W<WEEK>/index.html` — open in browser or push to `gh-pages` for GitHub Pages deploy.

## Project Structure

```
weekly-ai-report/
├── main.py                  # End-to-end pipeline orchestrator
├── config.yaml              # Model, filter, evaluator settings
├── sources.yaml             # Seed source definitions (migrated to source.db)
├── .env                     # API keys (QWEN_API_KEY, GITHUB_TOKEN, EXA_API_KEY)
├── .env.example             # Template with all required keys
├── scheduler.py             # Cron entry point (optional)
│
├── source_registry.py       # DB-first source registry (source.db)
├── source_db.py             # SQLite source store (eval_score, streak_failures, status)
├── article_db.py            # SQLite article/event/event_articles store
├── candidate_pool.py        # Source candidate pool (discovery → verify → DB)
├── url_registry.py          # Hard URL dedup (md5 + SQLite)
├── embedding_model.py       # Unified embedding loader (MiniLM ONNX)
│
├── fetcher/
│   ├── base_extractor.py    # Base extractor interface
│   ├── extractors.py        # RSS / GitHub / Web / arXiv / HF extractors
│   ├── connector_registry.py  # Source type → connector mapping
│   └── fetch_manager.py     # Async orchestrator, concurrency control
│
├── dedup/
│   └── deduplicator.py      # URL+source+date hard dedup (SQLite)
│
├── filter/
│   ├── blacklist_filter.py  # Zero-token keyword pre-filter
│   ├── event_curator.py     # LLM event extraction + scoring
│   ├── filter_summarizer.py # LLM curator (batch classification + scoring)
│   ├── scoring.py           # Time-weighted scoring (new +3, old -2)
│   ├── link_miner.py        # Outbound link extraction from high-score articles
│   └── ***.py               # Exa API neural search for source discovery
│
├── event_clustering.py      # Event clustering (sentence_cosine greedy, threshold=0.5)
│
├── generator/
│   ├── report_generator.py  # Jinja2 HTML report (index + 5 category pages)
│   └── templates/
│       ├── index.html       # Homepage with sidebar stats & source dynamics
│       └── category.html    # Category sub-pages
│
├── evaluator/
│   ├── source_evaluator.py  # Weekly source scoring, archival (stale_weeks=4)
│   └── source_discoverer.py # LLM-based new source recommendations
│
├── models/
│   └── llm_client.py        # OpenAI-compatible client, langsmith tracing
│
├── extractors/
│   └── contract.py          # RawArticle / CuratedArticle Pydantic schemas
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

- **7-stage pipeline** — Fetch → Hard Dedup → Blacklist → LLM Curator → Event Dedup → Clustering → Report
- **Event-level dedup** — FastEmbed + faiss cosine ≥ 0.70 (catches near-duplicate events)
- **LLM caching** — SHA256 content hash + prompt versioning (auto-invalidate on prompt change)
- **DB-first sources** — `source.db` is the runtime truth; YAML is seed only
- **Source self-evolution** — Weekly eval + auto-archive stale sources (stale_weeks=4)
- **Candidate pool** — LLM recommendation + link mining → verify → write DB → auto-inject
- **Unified embedding** — `embedding_model.py` loads MiniLM ONNX; works offline (`HF_HUB_OFFLINE=1`)
- **Cumulative per week** — Multiple runs within same week merge, newest-first
- **Empty category fallback** — No new articles → carry over from up to 4 previous weeks
- **Time-weighted scoring** — New events +3, old -2; top 50 per category
- **Frontend source dynamics** — Sidebar shows newly discovered / archived source counts

## Configuration

### Model (config.yaml)

```yaml
model:
  provider: custom
  name: Qwen3.6-27B
  base_url: http://your-endpoint/v1
  api_key: ${QWEN_API_KEY}
  temperature: 0.3
  max_tokens: 8192
  timeout: 600
```

### Sources (sources.yaml — seed only)

```yaml
sources:
  - name: OpenAI Blog
    url: https://openai.com/blog/rss.xml
    type: rss
    default_category: LLM
```

Source types: `rss` | `web` | `github_trending` | `github_repo` | `arxiv` | `hf` | `wechat`

**Note:** Runtime source state (eval_score, status, streak_failures) lives in `source.db`. YAML is only used as the initial seed migration file.

### Blacklist (config.yaml)

```yaml
filter:
  blacklist_keywords:
    - "融资"
    - "股票"
    - "NFT"
    - "web3"
    # ... add your own
```

## Deploy to GitHub Pages

```bash
# After running main.py, deploy output to gh-pages:
git worktree add /tmp/gh-pages origin/gh-pages
cp -r output/2026-W35 /tmp/gh-pages/
cd /tmp/gh-pages && git add -A && git commit -m "deploy W35" && git push origin HEAD:gh-pages
```

## Cron

```bash
# Every Monday 8:00
# 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python main.py --reset
```

---

Built during internship at **TCL Research, Wuhan** · 2026