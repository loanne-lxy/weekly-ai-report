"""Cross-event synthesis with validated event citations and supporting excerpts."""
import json
import re
import logging

logger = logging.getLogger(__name__)


def synthesize(llm, events):
    eligible = [e for e in events if e.get("top_eligible") and e.get("quality_status") == "passed"]
    # Balance domains and bound context so high-volume categories cannot dominate.
    selected, counts = [], {}
    for e in sorted(eligible, key=lambda e: e.get("final_score", 0), reverse=True):
        category = e["category"]
        if counts.get(category, 0) < 5:
            selected.append(e)
            counts[category] = counts.get(category, 0) + 1
    eligible = selected
    by_id = {e["id"]: e for e in eligible}
    payload = [{"id": e["id"], "title": e["title"], "category": e["category"],
                "summary": e["summary"], "limitations": e.get("limitations", "")} for e in eligible]
    if llm is None or len(eligible) < 2:
        return {"observations": [], "domain_signals": [], "status": "insufficient_evidence"}
    prompt = """你是审慎的研究编辑。仅基于所给事件生成本周研究观察和领域信号，不能编造数字、历史对比或因果关系。
返回 JSON 对象：observations 为2-3条跨事件观察；domain_signals为有证据的分类信号。
每条包含 title, text（中文，具体技术变化与研究意义，80-150字）, limitation（证据局限），
category（domain_signals必需），citations:[{event_id, quote}]。quote必须逐字摘自对应summary，至少12字符。
每条研究观察至少引用2个不同事件；不能仅以主题相似声称行业趋势，应解释共同问题和不同方法。
每个领域信号至少引用1个事件。没有历史基线，禁止声称比上周增长、加速或成为主流；表述为本周观察到的进展。
证据不足就少写，不要凑数。仅返回JSON。"""
    feedback = ""
    for attempt in range(2):
        try:
            raw = llm.chat(prompt, json.dumps(payload, ensure_ascii=False) + feedback)
            obj = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            result = {"observations": [], "domain_signals": [], "status": "validated"}
            for key in ("observations", "domain_signals"):
                for item in obj.get(key, []):
                    citations = item.get("citations", [])
                    ids = {c.get("event_id") for c in citations}
                    if len(ids) < (2 if key == "observations" else 1) or not ids <= by_id.keys():
                        continue
                    if key == "domain_signals" and any(by_id[i]["category"] != item.get("category") for i in ids):
                        continue
                    norm = lambda s: re.sub(r"\s+", "", s)
                    if not all(len(c.get("quote", "")) >= 12 and norm(c["quote"]) in norm(by_id[c["event_id"]]["summary"]) for c in citations):
                        continue
                    result[key].append(item)
            result["observations"] = result["observations"][:3]
            logger.info("Research synthesis attempt %s: %s validated observations, %s domain signals", attempt + 1, len(result["observations"]), len(result["domain_signals"]))
            covered = {item.get("category") for item in result["domain_signals"]}
            missing = {e["category"] for e in eligible} - covered
            if len(result["observations"]) >= 2 and not missing:
                return result
            feedback = "\n前次输出未通过校验。请重新输出完整 JSON；每条引用必须逐字复制输入 summary。缺少通过校验的领域信号：" + ", ".join(sorted(missing))
            feedback += "。每个领域只引用该领域事件；研究观察仍需至少2条、每条至少2个不同事件。"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Research synthesis validation failed: %s", str(exc)[:180])
            feedback = "\n前次输出结构校验失败，请返回完整 JSON 且所有 quote 必须逐字摘抄。"
            continue
    return {"observations": [], "domain_signals": [], "status": "synthesis_failed_validation"}
