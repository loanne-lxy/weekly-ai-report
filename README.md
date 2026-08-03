# 📊 Weekly AI Report Agent

> 自动抓取前沿 AI 资讯（LLM · Agent · AI for Science · 设计仿真 · 数字孪生），LLM 智能评分排序，生成周报网页。每周一自动运行，信息源池自我进化。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-green" alt="DeepSeek">
  <img src="https://img.shields.io/badge/LangSmith-Tracing-orange" alt="LangSmith">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
</p>

---

## ✨ 功能

- **🌐 多源抓取** — RSS / 网页 / Nitter RSS，asyncio 并发，45+ 信息源
- **🏷️ 智能分类** — LLM 将文章归类到 LLM / Agent / AI for Science / 设计仿真 / 数字孪生 五大领域
- **📈 质量评分** — 每条文章 1-10 分重要性评估 + 中文标题生成
- **🔍 趋势关键词** — LLM 提取当周五大领域趋势关键词
- **📄 周报网页** — 浅色科技风，按重要性排序，支持分页
- **🔄 源池自进化** — 每周评估信息源质量，自动发现新源，连续失效自动归档
- **⏰ 定时运行** — 配合 cron / systemd timer 每周自动生成
- **🛡️ 去重** — SQLite 记录已抓 URL，永不重复

---

## 📦 快速开始

```bash
# 1. 克隆
git clone https://github.com/loanne-lxy/weekly-ai-report.git
cd weekly-ai-report

# 2. 安装依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 配置 API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 4. 运行
python main.py

# 5. 查看周报
# 浏览器打开 output/2026-WXX/index.html
```

---

## ⚙️ 定时运行

```bash
# 每周一 08:00 运行
crontab -e
# 添加: 0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python scheduler.py
```

---

## 🏗️ 架构

```
Cron / 手动触发
      │
      ▼
┌─────────────┐    ┌──────────────┐    ┌──────────┐    ┌─────────────┐
│  抓取层      │ →  │  筛选层       │ →  │  评分层   │ →  │  生成层      │
│  RSS/Web/   │    │  关键词预筛    │    │  1-10 分   │    │  Jinja2      │
│  Nitter     │    │  LLM 分类     │    │  中文标题   │    │  网页输出     │
└─────────────┘    └──────────────┘    └──────────┘    └─────────────┘
      │                                                     │
      └──────────── 每周评估 → 源池自动更新 ←───────────────┘
```

---

## 📂 项目结构

```
weekly-ai-report/
├── main.py                    # 主流程编排
├── scheduler.py               # 定时调度入口
├── config.yaml                # 模型 & 参数配置
├── sources.yaml               # 信息源池（自动更新）
├── .env                       # API Key（不上传 Git）
├── models/llm_client.py       # LLM 统一接口（支持切换模型）
├── fetcher/fetcher.py          # 并发抓取
├── dedup/deduplicator.py      # URL 去重
├── filter/filter_summarizer.py # 分类 + 评分 + 中文标题
├── evaluator/
│   ├── source_evaluator.py    # 源池评估 & 自动归档
│   └── source_discoverer.py   # LLM 自动发现新源
├── generator/
│   ├── report_generator.py    # 趋势分析 + 报告渲染
│   └── templates/weekly.html  # 网页模板
└── output/                    # 生成的周报
    └── 2026-W32/index.html
```

---

## 🔄 切换模型

编辑 `config.yaml`：

```yaml
# 本地 Ollama
model:
  provider: ollama
  name: qwen3:14b
  base_url: http://localhost:11434/v1
  api_key: ollama

# DeepSeek
model:
  provider: deepseek
  name: deepseek-chat
  base_url: https://api.deepseek.com
  api_key: ${DEEPSEEK_API_KEY}
```

---

## 🔍 LangSmith 追踪

项目已集成 [LangSmith](https://smith.langchain.com)，在 `.env` 中设置 `LANGSMITH_TRACING=true` 即可追踪每次 LLM 调用的延迟、token 消耗和响应内容。

---

## 📝 许可证

MIT
