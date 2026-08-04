---
name: weekly-ai-report
description: "Autonomous AI news agent that scrapes 50+ sources, classifies articles into 5 domains (LLM/Agent/AI for Science/Design Simulation/Digital Twin), scores importance, generates structured HTML weekly reports with trend analysis, and self-evolves its source pool."
version: 1.0.0
author: loanne-lxy
license: MIT
tags: [news, research, report, scraper, rss, ai-news, weekly-digest, github-pages]
metadata:
  hermes:
    category: productivity
    requires_toolsets: [terminal, file, web]
---

# Weekly AI Report Agent

Autonomous agent that scrapes cutting-edge AI news from 50+ RSS/Web/Nitter sources across 5 domains (LLM, Agent, AI for Science, Design Simulation, Digital Twin), scores articles by importance, and generates a tech-themed weekly report webpage with auto-deployment to GitHub Pages.

## Architecture

```
Cron / Manual Trigger (weekly)
       │
       ▼
┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────┐
│ Fetcher  │ → │ Dedup    │ → │ Classifier    │ → │ Report   │
│ RSS/Web  │   │ SQLite   │   │ LLM scoring   │   │ HTML +   │
│ Nitter   │   │ URL check│   │ CN titles     │   │ GH Pages │
└──────────┘   └──────────┘   └──────────────┘   └──────────┘
      │                                              │
      └── Source Pool Self-Evolution ←───────────────┘
           (weekly eval + auto-discover)
```

## Input

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sources.yaml` | file | yes | 50+ RSS/Web/Nitter source definitions with categories and weights |
| `config.yaml` | file | yes | Model config (provider/name/base_url/temperature), filter keywords, evaluator settings |
| `.env` | file | yes | `DEEPSEEK_API_KEY` (or any OpenAI-compatible API key) |
| cron/manual trigger | event | yes | Weekly execution via `python main.py` or `python scheduler.py` |

### Source Format (sources.yaml)
```yaml
sources:
  - name: "OpenAI Blog"
    url: "https://openai.com/blog/rss.xml"
    type: rss          # rss | web | nitter_rss
    category: LLM
    weight: 9
```

## Output

| Output | Format | Location | Description |
|--------|--------|----------|-------------|
| Weekly Report | HTML | `output/<WEEK>/index.html` | Tech-themed webpage with trends, statistics, and ranked articles |
| Article Data | JSON | `output/<WEEK>/articles.json` | All classified/scored articles for fallback |
| Source Pool | YAML | `sources.yaml` | Updated source list with eval scores (auto-maintained) |
| Public URL | Web | `https://loanne-lxy.github.io/weekly-ai-report/` | Auto-deployed via GitHub Actions |

## Capabilities

### 1. Multi-Source Concurrent Fetching
- RSS feeds (feedparser), Web pages (BeautifulSoup), Nitter RSS (Twitter alternative)
- `asyncio.gather` with `Semaphore` for concurrency control (default 10)
- Each source independent — one failure doesn't affect others

### 2. URL Deduplication
- SQLite database tracks all seen URLs
- First run captures everything, subsequent runs only add new articles

### 3. LLM Classification & Scoring
- Articles classified into 5 domains with precise category definitions in prompt
- Each article receives: Chinese title + 80-char Chinese summary + 1-10 importance score
- 5 concurrent LLM calls for classification and enrichment

### 4. Source Pool Self-Evolution
- Weekly evaluation: each source scored by article output
- Auto-archive: 4 consecutive weeks with 0 output → marked inactive
- Auto-discovery: LLM recommends 3-7 new sources based on top articles, merged into pool

### 5. Empty Category Fallback
- If a category has 0 new articles this week, loads from last week's `articles.json`
- Articles marked with "📌 carried over" badge + original publish date

### 6. Trend Analysis
- LLM generates 2-4 character Chinese trend keywords per domain
- Displayed in gradient bar at top of report

### 7. GitHub Pages Auto-Deployment
- `.github/workflows/deploy.yml` deploys on push to master
- Root index.html redirects to latest week
- All historical weeks permanently archived

## Configuration

### Model Switching (config.yaml)
```yaml
# DeepSeek
model:
  provider: deepseek
  name: deepseek-chat
  base_url: https://api.deepseek.com
  api_key: ${DEEPSEEK_API_KEY}

# Ollama (local)
model:
  provider: ollama
  name: qwen3:14b
  base_url: http://localhost:11434/v1
  api_key: ollama
```

### Domain Definitions
- **LLM**: Core model technology (architecture, training, inference, evaluation) — NOT application stories
- **Agent**: AI agents (tool use, multi-agent, RAG, robots, task planning)
- **AI for Science**: AI-driven scientific discovery (protein, drug, materials, weather, math)
- **Design Simulation**: AI-assisted engineering (generative design, CAD, CAE, 3D generation)
- **Digital Twin**: Industrial digital twin (IoT, real-time simulation, predictive maintenance)

## Usage

```bash
# Install
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
echo "DEEPSEEK_API_KEY=***" > .env

# Run
python main.py              # Normal mode
python main.py --reset      # Test mode (clear dedup DB)
python main.py --no-fetch   # Skip fetching

# Weekly cron
# 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py

# Deploy to public
git add output/ && git commit -m "update report" && git push
```

## LangSmith Tracing

Set in `.env`:
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=***
LANGSMITH_PROJECT=weekly-ai-report
```

All LLM calls are automatically traced at [smith.langchain.com](https://smith.langchain.com).

## Limitations & Future Work

1. **Two-stage filtering**: Currently all keyword-passing articles go to main LLM. A rule-based pre-scoring stage would save ~60% API calls
2. **Content quality ceiling**: Classification/scoring accuracy is model-dependent (DeepSeek ~75%, GPT-4 ~90%). Not fixable via prompt alone
3. **No local model support**: Requires cloud API (Qwen-7B needs 5+ GB VRAM, developer machine has 128MB)
4. **Sync error propagation**: Linear pipeline — any stage failure loses all prior work
5. **No incremental checkpointing**: Restart means full re-run
