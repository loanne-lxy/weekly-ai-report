"""W37 修复回归测试 — 确定性，无网络/LLM/模型依赖。

运行: python3 tests/test_w37_fix.py   (项目根目录)

覆盖:
  1. 字段流贯通: RawArticle(category=...) 序列化 → article_category / DB 读链
  2. 别名归一: _clean_category (AI4Science/DesignSimulation/DigitalTwin/Uncategorized)
  3. 聚类先验: _build_event / _single_event category 来自源默认类别
  4. 报告分组: _group_events_by_category 不再静默塞 LLM；未分类 → 其他
  5. Curator fallback: 源先验类别 + 英文 [未翻译] 标记
  6. prompt 约束: system 含禁编造/禁新闻体；event block 含 Source categories
  7. 模板: 文章列表渲染摘要行
"""
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from extractors.contract import RawArticle  # noqa: E402
from event_clustering import (  # noqa: E402
    _build_event, _clean_category, _single_event, article_category,
)
from generator.report_generator import _group_events_by_category  # noqa: E402
from filter.event_curator import EventCurator, EVENT_CURATOR_SYSTEM, _EVENT_TEMPLATE  # noqa: E402

PASS = 0


def ok(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")


CATS = ["LLM", "Agent", "AI for Science", "设计仿真", "数字孪生"]

print("1) 字段流: RawArticle → 序列化 → 读链")
ra = RawArticle(
    url="https://arxiv.org/abs/9999.00001", title="t", source_id="s1",
    source_name="arxiv", source_type="arxiv", category="AI4Science",
)
d = ra.model_dump()
ok("RawArticle 接受 category 字段", d["category"] == "AI4Science")
ok("article_category 读 category", article_category(d) == "AI for Science")
ok("article_category 读 default_category(旧键)",
   article_category({"default_category": "DesignSimulation"}) == "设计仿真")
ok("article_category 全空 → 空串", article_category({"title": "x"}) == "")
ok("article_category 空串不 fallback 到默认",
   article_category({"category": "", "default_category": "LLM"}) == "LLM")

print("2) 别名归一")
ok("AI4Science → AI for Science", _clean_category("AI4Science") == "AI for Science")
ok("DesignSimulation → 设计仿真", _clean_category("DesignSimulation") == "设计仿真")
ok("DigitalTwin → 数字孪生", _clean_category("DigitalTwin") == "数字孪生")
ok("LLM/Agent 原样", _clean_category("LLM") == "LLM" and _clean_category("Agent") == "Agent")
ok("Uncategorized → 空", _clean_category("Uncategorized") == "")
ok("空 → 空", _clean_category("") == "")

print("3) 聚类先验")
arts = [
    {"title": f"a{i}", "summary": "s", "default_category": "AI4Science",
     "priority_score": 6} for i in range(3)
]
ev = _build_event(arts, [0, 1, 2])
ok("_build_event 众数=AI for Science(归一后)", ev.category == "AI for Science")
ev2 = _single_event(0, {"title": "x", "default_category": "DigitalTwin"})
ok("_single_event 源默认类别", ev2.category == "数字孪生")
ev3 = _build_event([{"title": "x", "summary": "s"}], [0])
ok("无类别簇 → 空串(不再硬编码 Uncategorized)", ev3.category == "")

print("4) 报告分组")
events = [
    {"event_title": "e1", "category": "AI4Science"},          # 旧源码
    {"event_title": "e2", "category": ""},                     # 空
    {"event_title": "e3", "category": "LLM"},
    {"event_title": "e4", "category": "SomeNewField"},         # 未知
]
g = _group_events_by_category(events, CATS)
ok("LLM 桶只有 e3(不被污染)",
   [e["event_title"] for e in g["LLM"]] == ["e3"])
ok("AI4Science 归入 AI for Science 桶",
   [e["event_title"] for e in g["AI for Science"]] == ["e1"])
ok("空类别 → 其他", "e2" in [e["event_title"] for e in g["其他"]])
ok("未知类别 → 其他", "e4" in [e["event_title"] for e in g["其他"]])
ok("其他桶不混入 LLM 内容", all(e["category"] != "LLM" for e in g["其他"]))

print("5) Curator fallback")
emb = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
fb_evt = {
    "title": "Stochastic Operator Inference for reduced modeling",
    "summary": "Modeling complex physical phenomena ...",
    "category": "",
    "article_indices": [0, 1],
    "articles": [
        {"title": "a0", "default_category": "AI4Science"},
        {"title": "a1", "category": "AI for Science"},
    ],
}
fb = EventCurator._fallback_event(fb_evt, emb)
ok("fallback 类别=源先验", fb["category"] == "AI for Science")
ok("fallback 英文标题加 [未翻译]", fb["event_title"].startswith("[未翻译]"))
ok("fallback 英文摘要加 [未翻译]", fb["event_summary"].startswith("[未翻译]"))
fb2 = EventCurator._fallback_event(
    {"title": "子空间推理提升RLHF", "summary": "中文摘要", "category": "LLM",
     "article_indices": [], "articles": []}, None)
ok("fallback 中文标题不加标记", not fb2["event_title"].startswith("[未翻译]"))
fb3 = EventCurator._fallback_event(
    {"title": "no cat event", "summary": "", "category": "LLM",
     "article_indices": [], "articles": []}, None)
ok("fallback 无源先验 → 空类别(不再硬编码 LLM)", fb3["category"] == "")

print("6) prompt 约束")
ok("system 含禁编造", "禁止编造" in EVENT_CURATOR_SYSTEM)
ok("system 含禁新闻体", "亲述" in EVENT_CURATOR_SYSTEM)
ok("system 含源类别优先", "Source categories" in EVENT_CURATOR_SYSTEM)
block = _EVENT_TEMPLATE.format(idx=0, bucket="social", stats_text="s",
                               source_category="AI for Science",
                               articles_text="")
ok("event block 注入 Source categories", "AI for Science" in block)
import filter.event_curator as ec  # noqa: E402
ok("user template 标题要求实质化",
   "禁止新闻体" in ec.EVENT_CURATOR_USER_TEMPLATE)

print("7) 模板文章摘要行")
tpl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "generator", "templates", "category.html")
tpl = open(tpl_path, encoding="utf-8").read()
ok("category.html 渲染 article-summary", "article-summary" in tpl)

print("8) DB 写入链(临时库)")
from article_db import ArticleDB  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    dbp = os.path.join(td, "k.db")
    db = ArticleDB(db_path=dbp)
    db.upsert_article({
        "url": "https://x.test/1", "title": "t", "summary": "s",
        "source_id": "s1", "source_name": "n", "default_category": "AI4Science",
    })
    conn = sqlite3.connect(dbp)
    cat = conn.execute("SELECT category FROM articles WHERE url=?",
                       ("https://x.test/1",)).fetchone()[0]
    conn.close()
    db.close() if hasattr(db, "close") else None
    ok("upsert_article default_category 落库", cat == "AI4Science")

print("9) 脏摘要清洗（RSS 残留）")
from generator.report_generator import _clean_text  # noqa: E402
ok("移除'点击查看原文'", _clean_text("正常摘要。点击查看原文> ") == "正常摘要。")
ok("纯 HTML 脏文本 → 空",
   _clean_text('<div align="right"><a href="x">点击查看原文</a></div>') == "")
ok("移除 utm 参数", _clean_text("正文 utm_campaign=abc 结尾") == "正文  结尾")
ok("空/None 安全", _clean_text("") == "" and _clean_text(None) == "")
ok("干净文本不受影响", _clean_text("纯中文摘要，无脏字符。") == "纯中文摘要，无脏字符。")

print("10) BERTopic noise 键碰撞回归 (W37 GPT-6 事故)")
from event_clustering import _labels_to_clusters, NOISE_KEY_OFFSET  # noqa: E402
# 复现事故: 真实 topic 0..8 + 6 个 noise(-1) 交错。旧代码 noise 从 0 起号整体赋值,
# 覆盖真实 topic 0/2/7 等成员 → 文章整簇丢失 (W37 本地跑丢了 46 篇 social 含 GPT-6)。
topics = [-1, 0, 0, -1, 1, 2, -1, 3, 0, 4, -1, 5, 6, -1, 7, 8, -1]
indices = list(range(len(topics)))
clusters = _labels_to_clusters(topics, indices)
covered = [i for members in clusters.values() for i in members]
ok("全覆盖: 无文章丢簇", sorted(covered) == sorted(indices))
t0 = sorted(i for i, t in enumerate(topics) if t == 0)
ok("真实 topic 0 成员未被 noise 覆盖", sorted(clusters[0]) == t0)
noise_keys = [k for k in clusters if k >= NOISE_KEY_OFFSET]
ok("noise 单例簇数量正确", len(noise_keys) == sum(1 for t in topics if t == -1))
ok("noise 键全部偏移、不与真实标签冲突",
   all(k >= NOISE_KEY_OFFSET for k in noise_keys)
   and not (set(noise_keys) & set(t for t in topics if t != -1)))

print(f"\nALL {PASS} CHECKS PASSED")