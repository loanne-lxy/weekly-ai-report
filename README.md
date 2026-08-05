# 📊 Weekly AI Report Agent

> Autonomous agent that scrapes cutting-edge AI news across 5 domains, filters through a 5-stage pipeline, scores articles by importance with LLM curation, and generates a clean weekly report webpage auto-deployed to GitHub Pages.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-green" alt="DeepSeek">
  <img src="https://img.shields.io/badge/Classifier-MiniLM-purple" alt="MiniLM">
  <img src="https://img.shields.io/badge/Cache-SHA256-yellow" alt="Cache">
  <img src="https://img.shields.io/badge/LangSmith-Tracing-orange" alt="LangSmith">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
</p>

## ✨ Features

- **🌐 Multi-Source Fetching** — 70+ RSS / Web / Nitter RSS sources via asyncio
- **🔍 5-Stage Filtering Pipeline** — Source priority → Keyword+regex → MiniLM classifier → LLM curator → Cache
- **🏷️ 3-Layer Classification** — Keywords → MiniLM semantic → LLM curator confirmation
- **💾 SHA256 Content Cache** — Skip LLM on duplicate content, permanent cost savings
- **🤖 MiniLM CPU Classifier** — 80MB all-MiniLM-L6-v2, zero API cost for initial classification
- **📈 LLM Importance Scoring** — 1-5 priority, Chinese title, TL;DR, key insights, why-it-matters, tags
- **📄 Clean Report Webpage** — Full-width design, per-domain summaries, paginated (5/page)
- **🔄 Self-Evolving Source Pool** — Weekly evaluation, auto-discovery, auto-archive
- **🧩 Modular Prompts** — curation-rules.md + digest-prompt.md loaded at runtime
- **🌍 GitHub Pages** — Auto-deployed public URL with historical archive
- **📊 LangSmith Tracing** — All LLM calls observable

## 📂 Project Structure

```
weekly-ai-report/
├── SKILL.md                   # Standardized Agent Skill definition
├── main.py                    # Pipeline orchestrator (7 phases)
├── scheduler.py               # Cron entry point
├── config.yaml                # Model & parameter config
├── sources.yaml               # Source pool (auto-maintained)
├── .env                       # API keys (gitignored)
├── models/llm_client.py       # Unified LLM interface + LangSmith
├── fetcher/fetcher.py          # Concurrent fetching (asyncio)
├── dedup/
│   ├── deduplicator.py        # URL dedup (SQLite)
│   └── curator_cache.py       # SHA256 content cache
├── filter/
│   ├── source_priority.py     # Tier-based source weighting
│   ├── keyword_filter.py      # Domain keywords + regex scoring
│   ├── lightweight_classifier.py # MiniLM CPU classifier
│   └── filter_summarizer.py   # LLM curator + modular prompts
├── evaluator/
│   ├── source_evaluator.py    # Source evaluation & archiving
│   └── source_discoverer.py   # LLM-based source discovery
├── generator/
│   ├── report_generator.py    # Trend analysis + Jinja2 rendering
│   └── templates/weekly.html  # Clean full-width HTML template
├── references/
│   ├── curation-rules.md      # Prompt: filtering rules
│   └── digest-prompt.md       # Prompt: output format spec
├── .github/workflows/deploy.yml # GitHub Pages deployment
└── output/                    # Generated reports

## 🏗️ Pipeline

```
Fetch (70+ sources) → Dedup → Source Priority → Keyword+Regex
    → MiniLM Classifier → SHA256 Cache → LLM Curator → Report → GH Pages
                              ↓ hit               ↓ miss
                         skip LLM (free)      score + summarize
```

## 📦 Quick Start

```bash
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "DEEPSEEK_API_KEY=***" > .env

# Normal run
python main.py

# Test mode (clear dedup + cache)
python main.py --reset

# Skip fetching
python main.py --no-fetch
```

## 🔄 Switching Models

```yaml
# config.yaml
model:
  provider: deepseek  # or ollama / openai
  name: deepseek-chat
  base_url: https://api.deepseek.com
  api_key: ${DEEPSEEK_API_KEY}
```

## 🌍 Deployment

```bash
git add output/ && git commit -m "update report" && git push
```

Auto-deployed to https://loanne-lxy.github.io/weekly-ai-report/ via GitHub Actions.

## 📈 LangSmith Tracing

Set in `.env`:
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=***
LANGSMITH_PROJECT=weekly-ai-report
```

All LLM calls automatically traced at [smith.langchain.com](https://smith.langchain.com).

## 📝 License

MIT
