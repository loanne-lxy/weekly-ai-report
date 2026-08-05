# 📊 Weekly AI Report Agent

> Autonomous agent that scrapes cutting-edge AI news across 5 domains, filters through a 5-stage pipeline, scores articles by importance with LLM curation, and generates a clean weekly report webpage auto-deployed to GitHub Pages.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-green" alt="DeepSeek">
  <img src="https://img.shields.io/badge/Classifier-MiniLM-purple" alt="MiniLM">
  <img src="https://img.shields.io/badge/Cache-SHA256-yellow" alt="Cache">
  <img src="https://img.shields.io/badge/Arch-Connectors-cyan" alt="Connectors">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
</p>

## ✨ Features

- **🔌 Pluggable Connector Architecture** — RSS / Web / arXiv / Twitter / GitHub connectors; add a source type without touching the pipeline
- **🌐 Multi-Source Fetching** — 70+ sources via asyncio + connector registry
- **🔍 5-Stage Filtering** — Source priority → Keyword+regex → MiniLM classifier → SHA256 cache → LLM curator
- **🏷️ 3-Layer Classification** — Keywords → MiniLM semantic → LLM curator confirmation
- **📈 LLM Importance Scoring** — 1-5 priority, Chinese title, TL;DR, key insights, tags
- **📄 Clean Report** — Full-width, per-domain summaries, paginated, auto-deployed
- **🔄 Self-Evolving Source Pool** — Weekly evaluation, auto-discovery, auto-archive
- **🧩 Modular Prompts** — curation-rules.md + digest-prompt.md
- **💰 Cost** — ~$0.02/run with 67% cache hit rate

## 🏗️ Pipeline

```
Source Pool → Connector Layer → Dedup → Source Priority → Keyword+Regex
    → MiniLM Classifier → SHA256 Cache → LLM Curator → Report → GH Pages
```

## 📂 Project Structure

```
weekly-ai-report/
├── SKILL.md
├── main.py
├── config.yaml / sources.yaml / .env
├── fetcher/
│   ├── connectors.py          # Pluggable connector classes
│   └── fetcher.py             # Orchestrator (connector registry)
├── filter/
│   ├── source_priority.py / keyword_filter.py
│   ├── lightweight_classifier.py / filter_summarizer.py
├── dedup/
│   ├── deduplicator.py / curator_cache.py
├── generator/
│   ├── report_generator.py / templates/weekly.html
├── references/
│   ├── curation-rules.md / digest-prompt.md
└── output/
```

## 📦 Quick Start

```bash
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "DEEPSEEK_API_KEY=***" > .env
python main.py
```

## 🌍 Deployment

```bash
git add output/ && git commit -m "update report" && git push
```

Auto-deployed to https://loanne-lxy.github.io/weekly-ai-report/
