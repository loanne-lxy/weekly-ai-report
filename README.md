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
python main.py
```

## 💰 Cost

| Phase | Model | Cost |
|-------|-------|------|
| MiniLM Classifier | local CPU | Free |
| LLM Curator (~60 calls) | DeepSeek | ~$0.03 |
| Trends + Summaries | DeepSeek | ~$0.01 |
| Cache hits | — | $0 saved |
| **Total per run** | | **~$0.05** |

Cache hit rate grows over time → cost trends toward ~$0.02/run.

## ⚙️ Schedule

```bash
# Every Monday 08:00
crontab -e
# 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py
```

## 🔄 Model Switching

```yaml
# config.yaml
model:
  provider: deepseek  # or ollama / openai
  name: deepseek-chat
  base_url: https://api.deepseek.com
  api_key: ${DEEPSEEK_API_KEY}
```

## 📝 License

MIT
