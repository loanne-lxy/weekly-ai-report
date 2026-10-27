"""Cross-event synthesis with validated event citations and supporting excerpts."""
import json
import re
import logging

logger = logging.getLogger(__name__)


def synthesize(llm, events, _scope="all"):
    eligible = [e for e in events if e.get("top_eligible") and e.get("quality_status") == "passed"]
    # Balance domains and bound context so high-volume categories cannot dominate.
    selected, counts = [], {}
    for e in sorted(eligible, key=lambda e: e.get("final_score", 0), reverse=True):
        category = e["category"]
        if counts.get(category, 0) < 3:
            selected.append(e)
            counts[category] = counts.get(category, 0) + 1
    eligible = selected
    by_id = {e["id"]: e for e in eligible}
    payload = [{"id": e["id"], "title": e["title"], "category": e["category"],
                "summary": e["summary"], "limitations": e.get("limitations", "")} for e in eligible]
    if llm is None or len(eligible) < (1 if _scope == "domain" else 2):
        return {"observations": [], "domain_signals": [], "status": "insufficient_evidence"}
    # Keep structured calls below the endpoint's long-context reasoning cliff.
    if _scope == "all" and len(eligible) > 8:
        from concurrent.futures import ThreadPoolExecutor
        groups = [[e for e in eligible if e["category"] == c] for c in counts]
        observation_events = [e for group in groups for e in group[:2]][:6]
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(synthesize, llm, group, "domain") for group in groups]
            observation_job = pool.submit(synthesize, llm, observation_events, "observations")
            signals = [item for job in jobs for item in job.result()["domain_signals"]]
            observations = observation_job.result()["observations"]
        complete = len(observations) >= 2 and set(counts) <= {s["category"] for s in signals}
        return {"observations": observations, "domain_signals": signals,
                "status": "validated" if complete else "partial_validation"}
    prompt = """你是审慎的研究编辑。仅基于所给事件生成本周研究观察和领域信号，不能编造数字、历史对比或因果关系。
返回 JSON 对象：observations 为2-3条跨事件观察；domain_signals为有证据的分类信号。
每条包含 title, text（中文，具体技术变化与研究意义，80-150字）, limitation（证据局限），
category（domain_signals必需），citations:[{event_id, quote}]。quote必须逐字摘自对应summary，12-60字符，引用最短的完整支持片段，不要复制整个摘要。
每条研究观察至少引用2个不同事件；不能仅以主题相似声称行业趋势，应解释共同问题和不同方法。
每个领域信号至少引用1个事件。没有历史基线，禁止声称比上周增长、加速或成为主流；表述为本周观察到的进展。
证据不足就少写，不要凑数。每个领域最多一条信号，整个输出保持简短。仅返回JSON。"""
    if _scope == "domain":
        prompt += "\n本次仅生成该领域的一条 domain_signals，observations 返回空数组。"
    elif _scope == "observations":
        prompt += "\n本次仅生成两条 observations，domain_signals 返回空数组。"
    feedback = ""
    accepted_observations = []
    accepted_signals = {}
    for attempt in range(3):
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
            if len(result["observations"]) > len(accepted_observations):
                accepted_observations = result["observations"]
            for item in result["domain_signals"]:
                accepted_signals[item["category"]] = item
            result["observations"] = accepted_observations
            result["domain_signals"] = list(accepted_signals.values())
            covered = set(accepted_signals)
            missing = ({e["category"] for e in eligible} - covered) if _scope != "observations" else set()
            if (_scope == "domain" or len(result["observations"]) >= 2) and not missing:
                return result
            feedback = "\n前次输出未通过校验。已通过校验的条目会保留；请优先补齐以下领域信号，quote必须逐字复制输入 summary：" + ", ".join(sorted(missing))
            feedback += "。每个领域只引用该领域事件。"
            if _scope != "domain":
                feedback += "研究观察仍需至少2条、每条至少2个不同事件。"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Research synthesis validation failed: %s", str(exc)[:180])
            feedback = "\n前次输出结构校验失败，请返回完整 JSON 且所有 quote 必须逐字摘抄。"
            continue
    return {"observations": accepted_observations, "domain_signals": list(accepted_signals.values()),
            "status": "partial_validation" if accepted_observations or accepted_signals else "synthesis_failed_validation"}
