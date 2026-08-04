# 📊 Weekly AI Report Agent

> Autonomous agent that scrapes cutting-edge AI news across 5 domains, scores articles by importance, and generates a tech-themed weekly report webpage with auto-deployment to GitHub Pages.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-green" alt="DeepSeek">
  <img src="https://img.shields.io/badge/LangSmith-Tracing-orange" alt="LangSmith">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
  <img src="https://img.shields.io/badge/skill-standardized-blueviolet" alt="Agent Skill">
</p>

---

## ✨ Features

- **🌐 Multi-Source Fetching** — RSS / Web / Nitter RSS via asyncio, 50+ sources
- **🏷️ Smart Classification** — LLM classifies into LLM / Agent / AI for Science / Design Simulation / Digital Twin
- **📈 Quality Scoring** — 1-10 importance score + Chinese title + summary per article
- **🔍 Trend Keywords** — LLM extracts weekly trend keywords per domain
- **📄 Report Webpage** — Light-tech-themed HTML, ranked by importance, paginated
- **🔄 Self-Evolving Source Pool** — Weekly evaluation, auto-discovery, auto-archive
- **⏰ Scheduled Execution** — cron / systemd timer for weekly generation
- **🛡️ Deduplication** — SQLite-based URL dedup, never repeats
- **📌 Empty Category Fallback** — Carries over last week's content when no new articles
- **🌍 GitHub Pages** — Auto-deployed public URL with historical archive

## 📦 Quick Start

```bash
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "DEEPSEEK_API_KEY=***" > .env
python main.py
```

## ⚙️ Scheduled Execution

```bash
# Every Monday at 08:00
crontab -e
# Add: 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py
```

## 🏗️ Architecture

```
Cron / Manual Trigger
      │
      ▼
┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────┐
│ Fetcher  │ → │ Dedup    │ → │ Classifier    │ → │ Report   │
│ RSS/Web  │   │ SQLite   │   │ LLM scoring   │   │ HTML +   │
│ Nitter   │   │ URL check│   │ CN titles     │   │ GH Pages │
└──────────┘   └──────────┘   └──────────────┘   └──────────┘
      │                                              │
      └── Source Pool Self-Evolution ←───────────────┘
```

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
├── dedup/deduplicator.py      # SQLite dedup
├── filter/filter_summarizer.py # Classification + scoring + CN titles
├── evaluator/
│   ├── source_evaluator.py    # Source evaluation & archiving
│   └── source_discoverer.py   # LLM-based source discovery
├── generator/
│   ├── report_generator.py    # Trend analysis + Jinja2 rendering
│   └── templates/weekly.html  # HTML template
├── .github/workflows/deploy.yml # GitHub Pages deployment
└── output/                    # Generated reports
```

## 🔄 Switching Models

Edit `config.yaml`:

```yaml
# DeepSeek
model:
  provider: deepseek
  name: deepseek-chat
  base_url: https://api.deepseek.com
  api_key: ${DEEPSEEK_API_KEY}

# Local Ollama
model:
  provider: ollama
  name: qwen3:14b
  base_url: http://localhost:11434/v1
  api_key: ollama
```

## 🔍 LangSmith Tracing

Set in `.env`:
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=***
LANGSMITH_PROJECT=weekly-ai-report
```

## 📝 License

MIT
