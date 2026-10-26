# W44 聚类与证据关联修复报告

本次修复针对“事件数几乎等于文章数”的质疑，改动落在采集、事件核验、数据流水线和验证脚本，没有重构前端。

## 结论

最终候选审核结果为 **84 个事件、85 篇文章**，其中 83 个单篇事件、1 个双篇事件。数量与上一版相同，但现在经过了 214 对文章的事件身份核验：91 对不同事件、122 对同主题不同事件、1 对同一事件，无未完成的身份判断。

不能用事件数量下降证明聚类有效。当前样本大多是独立论文；本次没有发现有证据支持的额外合并。新的机制已经能够在初始聚类之后找回同一事件，并且真实的论文／解读关系和隔离正例都通过了验证。

## 根因与实证

1. 初始 0.72 完整链接阈值把 139 篇文章分成 138 个候选。之前后续审核只能拆分，无法跨候选合并。
2. 仅降低阈值会把不同研究合并：0.60 时变为 127 个候选，0.50 时为 92 个，不能据此当作正确事件。
3. 150 篇原始输入中 arXiv 与 Nature 合计 97 篇，Exa 仅 4 篇，输入中独立研究多、重复报道少。
4. RSS 摘要遗漏文章类型和引用／关联关系。这是本轮发现的实质性数据问题。

Nature 的 `Physics-informed machine learning: a hybrid lens for fluid dynamics` 是 News & Views，出版社在 associated content 中明确关联 `Lagrangian–Eulerian learning of flow field and trajectories with TrajectoryFlowNet`。只读 RSS 时模型因 DOI 不同将其拆成两个事件；补充出版社元数据后正确恢复为一个事件。普通参考文献不等于同一事件，提取与核验明确区分这两种关系。

来源：https://www.nature.com/articles/s42256-026-01319-4
关联研究：https://www.nature.com/articles/s42256-026-01316-7

## 实现

| 模块 | 修改 |
|---|---|
| `filter/event_identity.py`（新增） | 0.45 / top-8 语义候选，加上 DOI、arXiv ID、仓库及特征名称召回；逐对核验同一对象、动作、版本和证据；复核已有多篇事件；所有成员两两成立才合并，防止链式大簇；按模型隔离缓存并记录审计 |
| `filter/article_context.py`（新增） | 提取出版物类型、DOI、出版社关联内容和普通引用；正常采集补充短篇期刊摘要的元数据；不把导航或参考文献自动当成同一事件 |
| `filter/event_curator.py` | 证据核验通过的合并不再被 0.72 二次拆散；合并后重新审核摘要、成员证据和评分；单篇一致性标记为不适用 |
| `main.py` | 接入跨簇核验和最终文章去向审计；回放不联网补抓正文；正常运行接入有上限的补充搜索 |
| `filter/evidence_discovery.py`（新增） | 对本周已采集、具有 AI 内容的文章按名称补检报道，每次最多 3 个查询、每个最多 3 个结果；保留种子 URL 和查询来源；全部结果仍通过日期、正文和事件审核 |
| `config.yaml`、`sources.yaml` | 区分保守初始阈值与候选召回阈值；移除查询中的历史日期字面量，日期统一由 Exa 的七天起止参数控制 |
| `generator/research_observations.py` | 修复“研究观察存在但领域信号为空也提前通过”的漏洞，要求所有可发布领域都有有证据的信号 |
| `scripts/verify_quality_report.py` | 逐对检查多篇事件身份和引用，继续验证所有文章去向、历史周报、排序和链接 |
| 测试与评估脚本 | 新增真实控制样本、合成正负例及独立评估结果；明确区分合成测试和真实准确率 |

## 回放方法与范围

- 原始 150 个 URL 保持不变，没有为压低事件数量添加文章。
- 原 RSS 冻结回放与补充元数据回放分别保存。5 篇文章补充出版社元数据，正文摘要和 URL 保持不变。
- 时间窗口仍是 2026-10-19T00:00:01Z 至 2026-10-26T00:00:01Z，共七天。
- 正常采集时新增的定向 Exa 补检没有混入本次冻结回放，实际新增报道数量需在后续正常运行观察。
- 隔离运行目录：`data/w44-identity-stage/`。
- 最终审计：`data/w44-identity-stage/runs/2026-W44/`，包括 audit.json、identity_evaluation.json、curated_events.json、verification.json。

## 验证结果与边界

- 16 项原质量／领域信号测试通过。
- 12 项身份核验、出版社元数据、反链式合并、失败恢复和补检限制测试通过。
- W37 历史回归的 38 项检查通过。
- 实际模型的 4 组合成对照通过：中英同一次发布合并；同一论文与解读合并；不同版本和不同论文保持独立。合成文章仅在隔离测试目录中，未进入周报。
- 真实控制样本共有 8 组负例、1 组出版社关联正例。最终发布数据中实际覆盖 5 组负例和 1 组正例，均正确；另外 3 组因部分文章已被内容审核排除，不计入聚类效果统计。
- 不声称达到大规模精确率 ≥95%、召回率 ≥85%。目前没有独立人工标注的充分正例集合；1 个真实正例和合成用例只能证明相关路径可工作，不能证明总体召回率。
- 元数据提取目前重点支持短期刊摘要及出版社公开关联信息；不绕过付费正文访问限制。

## 复现

```bash
venv/bin/python -m unittest discover -s tests -p 'test_w44_quality.py'
venv/bin/python -m unittest discover -s tests -p 'test_event_identity.py'
venv/bin/python tests/test_w37_fix.py
venv/bin/python scripts/check_identity_positive_controls.py
venv/bin/python scripts/evaluate_event_identity.py --run-dir data/w44-identity-stage/runs/2026-W44
```

端到端回放使用 `--replay --from-cache data/w44-identity-stage/enriched-input.json --week 2026-W44 --cutoff 2026-10-26T00:00:01Z`，指定独立数据及输出目录。合并失败保留已审核的原事件；已有簇身份不成立且无法安全重写时，流水线拒绝发布。
