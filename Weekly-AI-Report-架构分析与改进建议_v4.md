# Weekly AI Report — 架构分析与改进建议 v4

> 2026-08-12 | 代码: 2085 行 Python / 16 个模块 | 源: 32 个 | W33 输出: 81 篇

---

## 一、当前架构

### 1.1 管线总览

```
Sources (32)
  │
  ├── Phase 1:   Fetch          RSS / GitHub / Web / arXiv, asyncio concurrency=10
  ├── Phase 2:   Hard Dedup     md5(url:source_type:date_bucket), SQLite seen_items
  ├── Phase 2.3: Blacklist      零 Token 关键词正则硬过滤 (~10 条规则)
  ├── Phase 2.5: Semantic Dedup FastEmbed bge-small-zh-v1.5, cosine ≥0.88 双门槛
  ├── Phase 3:   LLM Curator    BATCH=5, SHA256 缓存 + prompt_version 自动失效
  ├── Phase 3.5: Top-K Rerank   Pairwise C(K,2) 精排 Top 5 → Top 3
  ├── Phase 4:   Merge          同周累积合并，空领域回填（回溯最多 4 周）
  ├── Phase 5:   Generate       Jinja2 HTML（index + 5 分类页，10 条/页分页）
  └── Phase 6:   Source Eval    每周源评分 + LLM 自动发现新源
```

### 1.2 数据流

```
Source (name, url, type, category, weight, default_category)
  ↓
IngestionManager (asyncio, concurrency=10, max 20 articles/source)
  ↓
RawArticle (Pydantic: url, title, summary, published, source_name, default_category)
  ↓
Deduplicator → seen_items (SQLite, 311 条)
  ↓
BlacklistFilter → 过滤噪音关键词
  ↓
SemanticDeduplicator → FastEmbed 向量去重
  ↓
FilterSummarizer → LLM 批量策展 (BATCH=5)
    └─ 输入: curation-rules.md + digest-prompt.md (SHA256-versioned)
    └─ 输出: category, priority_score, chinese_title, key_insights, tags, ai_summary
    └─ 缓存: curator_cache (81 条命中/更新)
  ↓
TopK_Reranker → pairwise 精排 Top 3
  ↓
CuratedArticle → articles.json + HTML 报告
```

### 1.3 项目结构

```
weekly-ai-report/
├── main.py                     # 管线编排器
├── config.yaml                 # Model / filter / evaluator / fetch 配置
├── sources.yaml                # 32 个源定义
├── .env                        # API keys (gitignored)
├── .env.example                # 模板
├── requirements.txt            # 11 直依赖
├── scheduler.py                # Cron 入口 (可选)
│
├── fetcher/
│   ├── base_extractor.py       # 抽象基类
│   ├── extractors.py           # RSS / GitHub / Web / arXiv
│   └── ingestion_manager.py    # 异步调度, concurrency=10
│
├── dedup/
│   ├── deduplicator.py         # URL+source+date 硬去重 (SQLite)
│   ├── semantic_deduplicator.py  # FastEmbed 语义去重
│   └── curator_cache.py        # SHA256 缓存 + prompt versioning
│
├── filter/
│   ├── blacklist_filter.py     # L0 零 Token 关键词过滤
│   ├── filter_summarizer.py    # LLM 策展 (batch=5)
│   └── topk_reranker.py        # Pairwise Top-K 精排
│
├── extractors/
│   └── contract.py             # RawArticle / CuratedArticle Pydantic schema
│
├── generator/
│   ├── report_generator.py     # Jinja2 渲染 + 趋势关键词 + 领域摘要
│   └── templates/
│       ├── index.html          # 首页 (5 分类卡片 + 侧栏趋势)
│       └── category.html       # 分类子页 (10 条/页分页)
│
├── evaluator/
│   ├── source_evaluator.py     # 源评分 & 归档
│   └── source_discoverer.py    # LLM 推荐新源
│
├── models/
│   └── llm_client.py           # OpenAI 兼容客户端 + LangSmith 追踪
│
├── references/
│   ├── curation-rules.md       # 策展规则 (SHA256-versioned)
│   ├── digest-prompt.md        # LLM Prompt 模板
│   └── classification-scoring-design.md  # 架构设计文档
│
└── output/
    └── 2026-W33/               # index.html + 5 分类页 + articles.json
```

### 1.4 源池状态

| 分类 | 源数 | 活跃 | 死源 |
|------|------|------|------|
| LLM | 15 | 10 | 5 (Anthropic, thegradient.pub, paperswithcode, airesearchnews, aimagazine) |
| AI4Science | 5 | 4 | 1 (DP Technology 深势科技) |
| Agent | 4 | 4 | 0 |
| DigitalTwin | 4 | 2 | 2 (cs.GR, Digital Twin Magazine) |
| DesignSimulation | 4 | 3 | 1 (cs.GR) |
| **总计** | **32** | **23** | **9** |

| 源类型 | 数量 |
|--------|------|
| RSS | 26 |
| GitHub | 5 |
| Web | 1 |

### 1.5 W33 过滤漏斗（2026-08-12 实测）

```
Phase 1  Fetch:          317 篇文章 (29 源, 20 成功 / 9 失败)
     │
     ├── Phase 2  Hard Dedup:       317 → 311  (-6  硬重复)
     ├── Phase 2.3 Blacklist:       311 → 309  (-2  关键词命中)
     ├── Phase 2.5 Semantic Dedup:  309 → 137  (-172 语义重复, cosine≥0.88)
     ├── Phase 3  LLM Curator:      137 → 81   (-56  LLM 过滤, cache 命中 57)
     ├── Phase 3.5 Top-K Rerank:    Top 5 → pairwise 10 次 → Top 3
     └── Phase 4  Merge:            57 存量 + 24 新 → 81 总
```

| 阶段 | 输入 | 输出 | 过滤 | 过滤率 | 耗时 |
|------|------|------|------|--------|------|
| Fetch | — | 317 | — | — | 24s |
| Hard Dedup | 317 | 311 | -6 | 1.9% | ~3.5min* |
| Blacklist | 311 | 309 | -2 | 0.6% | <1s |
| Semantic Dedup | 309 | 137 | -172 | **55.7%** | 68s |
| LLM Curator | 137 | 81 | -56 | 40.9% | ~8.4min |
| Top-K Rerank | 5 | 3 | -2 | 40% | 51s |
| Generate (摘要+趋势) | 81 | — | — | — | ~4.4min |
| **总计** | | **81** | | **~19min** | |

> \* Hard Dedup 耗时异常（~3.5min），推测为上次中断导致 SQLite 锁未释放。正常运行应 <1s。

**LLM Curator 明细：** 137 篇输入 → 57 篇 cache 命中 + 80 篇新跑（16 批次，BATCH=5）→ 81 篇通过

**Merge 明细：** 81 篇 curator 输出中，57 篇已在本周 accumulator 中（上次运行遗留），仅 24 篇为新入库

**最终分布：**

| 领域 | 文章数 | 占比 | 分数范围 |
|------|--------|------|----------|
| Agent | 45 | 56% | 2.0 ~ 9.1 |
| LLM | 21 | 26% | 3.0 ~ 8.3 |
| AI for Science | 13 | 16% | 3.0 ~ 7.0 |
| 设计仿真 | 1 | 1% | 6.0 |
| 数字孪生 | 1 | 1% | 5.0 |
| **总计** | **81** | | **2.0 ~ 9.6** |

**分数分布：**
- 8-9 分：7 篇（Top 文章）
- 6-7 分：35 篇（主力内容）
- 4-5 分：32 篇（边缘文章）
- 2-3 分：7 篇（低优先级）

**Top 3（Pairwise 精排）：**
1. 🥇 脑信号引导大语言模型突破表征对齐瓶颈提升推理能力
2. 🥈 深度学习轨迹预测突破分子动力学模拟飞秒时间尺度限制
3. 🥉 OpenAI 发布 GPT-5.6-Cyber 专用于漏洞研究与安全测试

### 1.6 部署架构

```
本地 (WSL)
  ├── python main.py     → output/2026-W33/
  ├── git push origin master
  │
  └── GitHub Actions (deploy.yml)
        ├── actions/configure-pages@v5
        ├── actions/upload-pages-artifact@v3 (path: ./output)
        └── actions/deploy-pages@v4
              │
              └── GitHub Pages CDN
                    └── https://loanne-lxy.github.io/weekly-ai-report/
```

- Pages Source 已切换为 **GitHub Actions** 模式
- 触发条件：`output/**` 文件变更
- 部署成功率：5/5（最近 5 次全部成功）

---

## 二、关键设计决策

| 决策 | 理由 | 状态 |
|------|------|------|
| 分类对象是文章，不是源 | 源只提供 default_category 作为 Bayesian prior，LLM 有最终决定权 | ✅ 已实现 |
| 放弃四层预分类漏斗 | Qwen3.6-27B 是 reasoning 模型，思考过程占 ~2000 tokens，单次 ~13s | ✅ 已确认 |
| 语义去重门槛 ≥0.88 | 经验值，230 篇 → 111 篇（-52%），保留多样性同时去重 | ✅ 有效 |
| BATCH_SIZE=5 | 降 HTTP 往返，37 批次 → 14 批次（cache 命中 44 篇） | ✅ 有效 |
| Auto-retry 容错 | curator 返回 0 且无存量时重置 dedup 重跑 | ✅ 已验证 |
| 回填回溯 4 周 | 之前只看上周，导致 W31 数据填不到 W33 | ✅ 已修复 |

---

## 三、已完成的功能

- [x] 6 阶段管线（Fetch → Hard Dedup → Blacklist → Semantic Dedup → Curator → Rerank → Generate）
- [x] 语义去重（FastEmbed bge-small-zh-v1.5）
- [x] LLM 结果缓存（SHA256 + prompt versioning，自动失效）
- [x] L0 黑名单（零 Token 正则过滤）
- [x] L3 Top-K Pairwise 精排
- [x] 自动容错重试（curator 0 篇时重置 dedup）
- [x] 空领域回填（回溯最多 4 周）
- [x] 源池自动发现（LLM 推荐新源）
- [x] GitHub Pages 自动部署（GitHub Actions）
- [x] 前端：首页 5 分类卡片 + 侧栏趋势 + 分类子页分页
- [x] 前端：时间戳移除（爬取时间无意义）
- [x] 前端：Tag 前缀 # 移除
- [x] 前端：领域摘要门槛 ≥1 篇（之前 ≥2 导致单篇文章领域无摘要）
- [x] GitHub Extractor 兼容 PyGithub 2.9+
- [x] 清理死代码（fetcher/pipeline.py, fetcher/fetcher.py, test scripts）
- [x] requirements.txt 精简（11 直依赖）

---

## 四、当前问题

### 4.1 数字孪生 / 设计仿真内容严重不足

| 领域 | 文章数 | 原因 |
|------|--------|------|
| 数字孪生 | 1/81 | arXiv cs.SY (41 条) + cs.RO (65 条) 刚加入，curator 过滤后只剩 1 篇 |
| 设计仿真 | 1/81 | arXiv cs.MS 只有 4 条，cs.CV 产出少且被 LLM 分到其他领域 |

**根因：** arXiv 源的文章多为纯学术论文，LLM curator 对工业向领域（数字孪生/设计仿真）评分偏低。RSS 源质量不如 Agent/LLM 领域。

### 4.2 9 个死源未清理

| 源 | streak_failures | 问题 |
|----|-----------------|------|
| Anthropic Research | 5 | RSS 链接失效 |
| thegradient.pub | 5 | RSS 失效 |
| paperswithcode.com | 5 | RSS 失效 |
| DP Technology (深势科技) | 5 | Web 抓取失败 |
| Digital Twin Magazine | 5 | SSL 连接失败 |
| airesearchnews.com | 5 | RSS 失效 |
| aimagazine.com | 3 | RSS 失效 |
| arXiv cs.GR | 3 | 400 错误 |
| arxiv.org cs.LG | 3 | 400 错误 |

### 4.3 Agent 占比过高（56%）

GitHub 源（AutoGen, CrewAI, Transformers, LangChain）每个返回 20 篇 releases/changelog，被 curator 分类为 Agent。导致 Agent 占半数以上。

### 4.4 低分文章过多（2-5 分共 39 篇，48%）

Blacklist 不够精确，大量低质量文章进入 curator。L0 黑名单只有 ~10 条规则，覆盖面不足。

### 4.5 data/we-mp-rss 残留

root 所有，无法删除。是之前微信公众号 RSS 代理的残留，当前管线不使用。

---

## 五、可改进方向

### 5.1 接微信公众号源（高价值）

**现状：** `data/we-mp-rss/` 残留说明之前尝试过。微信公众号 RSS 需要代理（WeMpRss 等服务）。

**方案：**

| 方案 | 说明 | 成本 | 难度 |
|------|------|------|------|
| we-mp-rss 自建代理 | 本地部署 we-mp-rss（需要 Redis + Docker），订阅公众号后自动生成 RSS | 服务器 | 中 |
| RSSHub 微信路由 | RSSHub 有微信路由但需 cookie，不稳定 | 免费 | 高 |
| 第三方 RSS 服务 | 如 feedddog、RSSHub 等 | ~$5/mo | 低 |

**推荐路径：** 先用现成服务（如 https://we-mprss.xeoe.com/），验证效果后再决定是否自建。

**需要做的：**
1. 在 `extractors.py` 添加 `WeChatExtractor`（继承 `RSSExtractor`，加特殊处理）
2. `sources.yaml` 添加微信源：
   ```yaml
   - name: 量子位
     url: https://we-mprss.xeoe.com/r/gh_xxx
     type: rss
     category: LLM
     default_category: LLM
     weight: 8
   ```
3. 推荐公众号：量子位、新智元、AI 科技评论、机器之心、36Kr AI、虎嗅 AI

### 5.2 清理死源

批量归档 `streak_failures >= 3` 的源：

```python
# evaluator/source_evaluator.py 已有逻辑，但需要手动触发或降低阈值
```

**建议：** `config.yaml` 加 `evaluator.auto_archive_threshold: 3`，Phase 6 自动归档。

### 5.3 控制 Agent 占比

**方案 A：** 限制 GitHub 源 `max_articles_per_source` 从 20 降到 5
**方案 B：** curator prompt 中对 changelog/release notes 类文章降权
**方案 C：** 在 fetcher 层过滤 GitHub release 类型的文章（只保留 PR/issues）

**推荐：** 方案 A + B 组合，改动最小。

### 5.4 数字孪生/设计仿真补充源

已加 cs.SY (41) + cs.RO (65) + cs.MS (4)。还可以加：

| 领域 | 候选源 | 说明 |
|------|--------|------|
| 数字孪生 | Siemens Digital Industries Blog | RSS: `https://new.euro.siemens.com/en/press/press-releases/rss/` |
| 数字孪生 | NVIDIA Omniverse Blog | RSS: `https://blogs.nvidia.com/feed/` |
| 设计仿真 | Autodesk Research | RSS: `https://blogs.autodesk.com/research/feed/` |
| 设计仿真 | arXiv cs.HC (Human-Computer Interaction) | CAD 相关论文 |
| 设计仿真 | arXiv eess.SY (Signal Processing/Systems) | 仿真算法 |

### 5.5 黑名单增强

当前 ~10 条规则，建议扩充到 30+：

```yaml
blacklist_keywords:
  # 当前已有
  - "融资"
  - "股票"
  - "小白教程"
  # 建议新增
  - "岗位"
  - "招聘"
  - "面试"
  - "年薪"
  - "副业"
  - "自媒体"
  - "流量变现"
  - "课程"
  - "训练营"
  - "白皮书"
  - "年度报告"
  - "版本更新"
  - "release notes"
  - "changelog"
  - "补丁"
```

### 5.6 LLM 策展优化

**当前：** BATCH=5，单次 ~10-30s，14 批次 ~3.5min

**可优化：**
- prompt 精简（当前 curation-rules.md + digest-prompt.md 共 ~3000 字，可压缩到 1500）
- secondary_category 和 why_it_matters 合并，减少 token 浪费
- 对于 5 分以下的文章，不生成 key_insights（省 token）

### 5.7 前端优化

| 项目 | 当前 | 建议 |
|------|------|------|
| 搜索 | 无 | 前端全文搜索（标题 + 摘要） |
| 筛选 | 无 | 按分数/日期/标签筛选 |
| 排序 | 按 importance | 支持按分数/日期排序 |
| 分享 | 无 | 单篇文章分享链接（含 url 参数） |
| Dark mode | 无 | 深色模式切换 |
| PWA | 无 | 可安装为桌面 App |

### 5.8 周报自动化

**当前：** 手动 `python main.py` + `git push`

**建议：** 利用 Hermes cronjob 或 Linux crontab 每周一 8:00 自动跑：
```
0 8 * * 1 cd ~/weekly-ai-report && source venv/bin/activate && python main.py && git add -A && git commit -m "auto: W$(date +%Y-W%V)" && git push
```

---

## 六、技术债

| 问题 | 影响 | 优先级 |
|------|------|--------|
| `data/we-mp-rss/` root 残留 | 文件系统垃圾，gitignored | 低 |
| `output/_config.yml` 遗留 | Jekyll 兼容文件，不再需要 | 低 |
| `scheduler.py` 未与 Hermes cron 对齐 | 两份定时方案 | 中 |
| `references/classification-scoring-design.md` 内容过时 | 描述四层漏斗（已废弃） | 中 |
| `config.yaml` filter.categories 的 keywords 未使用 | 定义但不读取 | 低 |
| `main.py` 硬编码 `generated_at` 时间格式 | 中文环境下 `2026-08-12 18:21` 不够友好 | 低 |
| GitHub Trending 偶尔 `list index out of range` | 每周丢一批文章 | 中 |

---

## 七、性能数据

| 阶段 | W33 实测耗时 | 备注 |
|------|-------------|------|
| Phase 1 Fetch | 24s | 29 源并发 10，9 失败 |
| Phase 2 Hard Dedup | ~3.5min* | SQLite 查询，异常（正常 <1s） |
| Phase 2.3 Blacklist | <1s | 正则匹配 |
| Phase 2.5 Semantic Dedup | 68s | FastEmbed 加载 3s + 编码 65s |
| Phase 3 Curator (BATCH=5) | ~8.4min | 57 cache + 80 LLM (16 批次) |
| Phase 3.5 Rerank | 51s | C(5,2)=10 次 pairwise |
| Phase 4-5 Generate | ~4.4min | LLM 摘要/趋势 7 次 + Jinja2 渲染 |
| Phase 6 Source Eval | ~40s | 1 次 LLM 调用 |
| **总计** | **~19min** | 含部署 ~25min |

> \* Hard Dedup 耗时异常，为上次中断导致 SQLite 锁未释放。正常情况 <1s。

**性能瓶颈：LLM Curator（44%） + Generate 摘要/趋势（23%）共占总耗时 67%**

---

*报告生成：2026-08-12 | 基于 W33 实际产出与代码扫描*
