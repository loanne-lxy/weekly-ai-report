"""Evidence-grounded event curation; failures are quarantined, never promoted."""
from __future__ import annotations
import asyncio
import json
import hashlib
import logging
import re
from pathlib import Path
import numpy as np
from event_clustering import _clean_category, coherence
from filter.quality import score_number, grounded_limitations

logger = logging.getLogger(__name__)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "references"
EVENT_CURATOR_SYSTEM = (_PROMPTS_DIR / "event_curator_system.md").read_text()
EVENT_CURATOR_USER_TEMPLATE = (_PROMPTS_DIR / "event_curator_user.md").read_text()
_EVENT_TEMPLATE = "Source categories: {source_category}"
BATCH_SIZE = 2


class EventCurator:
    def __init__(self, llm, threshold=0.72, cache_dir=None):
        self.llm = llm
        self.threshold = threshold
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(self, evt):
        if self.cache_dir is None:
            return None
        material = {"version": 4, "threshold": self.threshold,
                    "system": EVENT_CURATOR_SYSTEM, "user": EVENT_CURATOR_USER_TEMPLATE,
                    "articles": [{k: a.get(k) for k in ("url", "title", "summary", "published", "date_status", "raw_extra")}
                                 for a in evt["articles"]]}
        if evt.get("identity_verified"):
            material["review_contract"] = "grounded-grouping-v1"
            material["identity_checks"] = evt["identity_checks"]
            material["identity_conflicts"] = evt.get("identity_conflicts", [])
        digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / (digest + ".json")

    @staticmethod
    def _fallback_event(evt, embeddings=None):
        return {**evt, "is_relevant": False, "event_title": "[待审核] " + evt.get("title", ""),
                "event_summary": "", "importance": 0.0, "novelty": 0.0, "impact": 0.0,
                "top_articles": [], "evidence_articles": [], "quality_status": "quarantined",
                "quality_reasons": ["curation_failed"], "top_eligible": False}

    async def _partition_large(self, evt):
        """Reduce output size for mixed large proposals without cosine pre-splitting.

        This is only a proposal stage. Every resulting group still receives the
        full evidence, relevance and coherence review below.
        """
        if len(evt["articles"]) <= 8:
            return [evt]
        instruction = """Use the grouping rules in the system prompt, but ONLY propose a partition.
Do NOT output summaries, scores or relevance decisions yet. Return a JSON array of
objects with article_indices (local integers) and shared_problem (specific common
research problem or concrete news development). Assign every article exactly once.
Group different research works on a specific shared problem; do not separate merely
because method/paper names differ. A whole field such as Agent is too broad.
Unrelated articles get separate groups. This is a compact proposal, not final approval."""
        payload = [{"index": i, "title": a.get("title", ""), "text": a.get("summary", "")[:1800]}
                   for i, a in enumerate(evt["articles"])]
        digest = hashlib.sha256((EVENT_CURATOR_SYSTEM + instruction + json.dumps(payload, ensure_ascii=False)).encode()).hexdigest()
        path = self.cache_dir / ("partition-" + digest + ".json") if self.cache_dir else None
        def checked(parts):
            if not isinstance(parts, list) or any(not isinstance(p,dict) or not p.get("article_indices") for p in parts):
                raise ValueError("invalid partition groups")
            ids = [i for p in parts for i in p["article_indices"]]
            if any(type(i) is not int for i in ids) or sorted(ids) != list(range(len(payload))):
                raise ValueError("partition must assign every article exactly once")
            return [{**evt, "articles": [evt["articles"][i] for i in p["article_indices"]],
                     "article_indices": [evt["article_indices"][i] for i in p["article_indices"]]}
                    for p in parts]
        if path and path.exists():
            try:
                return checked(json.loads(path.read_text()))
            except (ValueError, KeyError, TypeError):
                pass
        for attempt in range(2):
            try:
                raw = await asyncio.to_thread(self.llm.chat, EVENT_CURATOR_SYSTEM,
                                             instruction + "\nINPUT:\n" + json.dumps(payload, ensure_ascii=False))
                parts = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
                groups = checked(parts)
                if path:
                    path.write_text(json.dumps(parts, ensure_ascii=False, indent=2))
                logger.info("Large proposal partition: %s articles -> %s groups for full review", len(payload), len(groups))
                return groups
            except Exception as exc:
                logger.warning("Large proposal partition attempt %s failed: %s", attempt+1, str(exc)[:120])
        return [evt]

    async def curate_events(self, events, embeddings=None):
        emb = embeddings if embeddings is not None else np.zeros((0, 0))
        # Never silently truncate: every article receives a curation decision.
        safe = []
        for evt in events:
            safe.extend(await self._partition_large(evt))
        sem = asyncio.Semaphore(5)

        async def run(batch, retry_single=True):
            async with sem:
                payload = []
                for i, evt in enumerate(batch):
                    payload.append({"event_index": i, "source_category": evt.get("category", ""),
                                    "articles": [{"index": j, "title": a.get("title", ""),
                                      "text": a.get("summary", "")[:1800], "url": a.get("url"),
                                      "published": a.get("published"), "publisher": a.get("publisher_domain"),
                                      "publisher_context": (a.get("raw_extra") or {}).get("identity_context", {})}
                                     for j, a in enumerate(evt["articles"])]})
                for item, evt in zip(payload, batch):
                    if evt.get("identity_verified"):
                        by_url={a["url"]:i for i,a in enumerate(evt["articles"])}
                        item["reviewed_pairs"]=[{"indices":[by_url[u] for u in check["urls"]],
                            "relation":check["relation"], "shared_problem":check.get("identity"),
                            "reason":check.get("reason"), "quotes":check.get("quotes", [])}
                            for check in evt.get("identity_checks", [])+evt.get("identity_conflicts", [])
                            if all(u in by_url for u in check["urls"])]
                        item["review_instructions"] = (
                            "These grounded pair reviews propose specific shared problems. Preserve positive "
                            "same_research_problem groupings unless you identify a concrete contradiction in "
                            "the FULL group; different methods, domains or paper names alone are not contradictions. "
                            "Use one bounded problem to synthesize contributions, rather than requiring the same study. "
                            "Do not let a bridge connect unrelated problems. Explain any split in limitations. "
                            "AI relevance includes search, planning, reinforcement learning and intelligent multi-robot "
                            "coordination; a new neural architecture is not required. These articles previously passed "
                            "individual relevance review; do not reject them solely for using different methods.")
                error = None
                for attempt in range(2):
                    try:
                        raw = await asyncio.to_thread(self.llm.chat, EVENT_CURATOR_SYSTEM,
                            EVENT_CURATOR_USER_TEMPLATE + ("\nPrevious output failed validation: " + error + ". Correct the output; do not invent evidence." if error else "") + "\nINPUT:\n" + json.dumps(payload, ensure_ascii=False))
                        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                        objects = json.loads(raw)
                        if not isinstance(objects, list):
                            raise ValueError("expected JSON array")
                        result = []
                        if any(not isinstance(o, dict) or type(o.get("event_index")) is not int or not 0 <= o["event_index"] < len(batch) for o in objects):
                            raise ValueError("invalid event index")
                        for i, evt in enumerate(batch):
                            parts = [o for o in objects if o["event_index"] == i]
                            indices = [k for o in parts for k in o.get("article_indices", [])]
                            if any(type(k) is not int for k in indices) or sorted(indices) != list(range(len(evt["articles"]))):
                                raise ValueError("split must partition ALL input articles exactly once")
                            built = [self._build(evt, o, emb) for o in parts]
                            result.extend(built)
                            path = self.cache_path(evt)
                            if path:
                                path.write_text(json.dumps(parts, ensure_ascii=False, indent=2))
                        return result
                    except Exception as exc:
                        error = str(exc)
                        if isinstance(exc, json.JSONDecodeError):
                            error += " Near: " + repr(raw[max(0, exc.pos-80):exc.pos+80])
                            error += "; escape quotation marks and backslashes inside JSON strings."
                        logger.warning("Curator attempt %s failed: %s", attempt + 1, error[:180])
            # Recover per article outside semaphore. No original giant-cluster fallback.
            if retry_single:
                singles = [{**evt, "articles": [a], "article_indices": [idx]}
                           for evt in batch for a, idx in zip(evt["articles"], evt["article_indices"])]
                recovered = await asyncio.gather(*(run([single], retry_single=False) for single in singles))
                return [event for batch in recovered for event in batch]
            return [self._fallback_event(evt, emb) for evt in batch]

        cached, pending = [], []
        for evt in safe:
            path = self.cache_path(evt)
            try:
                if path and path.exists():
                    parts = json.loads(path.read_text())
                    ids = [i for part in parts for i in part["article_indices"]]
                    if sorted(ids) != list(range(len(evt["articles"]))):
                        raise ValueError("invalid cached partition")
                    cached.extend(self._build(evt, part, emb) for part in parts)
                    continue
            except (ValueError, KeyError, TypeError):
                logger.warning("Ignoring invalid curation cache")
            pending.append(evt)
        logger.info("Curation cache: %s decisions reused; %s proposals pending", len(cached), len(pending))
        batches = [pending[i:i+BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
        results = await asyncio.gather(*(run(batch) for batch in batches))
        all_events = cached + [e for batch in results for e in batch]
        logger.info("Curated %s events: %s publishable, %s quarantined/rejected", len(all_events),
                    sum(e.get("is_relevant", False) for e in all_events),
                    sum(not e.get("is_relevant", False) for e in all_events))
        return all_events

    def _build(self, evt, data, embeddings):
        positions = data["article_indices"]
        arts = [evt["articles"][i] for i in positions]
        ids = [evt["article_indices"][i] for i in positions]
        local = {old: new for new, old in enumerate(positions)}
        result = {**evt, "articles": arts, "article_indices": ids,
                  "article_count": len(arts), "is_relevant": data.get("is_relevant") is True,
                  "quality_reasons": [], "top_eligible": False}
        # A split child must not inherit pair evidence about siblings outside it.
        member_urls = {a["url"] for a in arts}
        for field in ("identity_checks", "identity_conflicts"):
            if field in result:
                result[field] = [check for check in result[field]
                                 if set(check.get("urls", [])) <= member_urls]
        if not result["is_relevant"]:
            reason = str(data.get("relevance_reason", ""))
            source = " ".join(a.get("title", "") + " " + a.get("summary", "")[:1800] for a in arts)
            category_only = re.search(r"无法归入|预定义类别|不符合.*类别|does not satisfy any category|outside.*categor|predefined categor", reason, re.I)
            explicit_ai = re.search(r"reinforcement learning|neural|diffusion model|machine learning|Gaussian Splatting|强化学习|生成模型", source, re.I)
            if category_only and explicit_ai:
                raise ValueError("Category mismatch is not evidence of AI irrelevance. For explicit AI research use the closest existing category: Agent for learned control policies, LLM for multimodal generative models, AI for Science for medical/scientific AI methods. Assess relevance separately from category fit.")
            result.update(quality_status="rejected", quality_reasons=[data.get("relevance_reason", "unrelated")])
            return result
        evidence = []
        for item in data.get("evidence", []):
            if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] not in local:
                raise ValueError("evidence index outside sub-event")
            a = arts[local[item["index"]]]
            quote = str(item.get("quote", "")).strip()
            norm = lambda s: re.sub(r"\s+", "", s).casefold()
            if len(quote) < 12 or norm(quote) not in norm(a.get("title", "") + " " + a.get("summary", "")[:1800]):
                raise ValueError("evidence quote is not grounded in supplied article")
            relevance = score_number(item.get("relevance"))
            evidence.append({"index": local[item["index"]], "url": a["url"], "relevance": relevance,
                             "reason": str(item.get("reason", ""))[:300], "quote": quote[:600]})
        evidence.sort(key=lambda e: e["relevance"], reverse=True)
        if {e["index"] for e in evidence} != set(range(len(arts))) or len(evidence) != len(arts):
            raise ValueError("each member needs exactly one relevance assessment")
        coherent = coherence(ids, embeddings)
        grouping_type = data.get("grouping_type", "news_event")
        research_question = str(data.get("research_question", "")).strip()
        if grouping_type == "news_event" and any(
            check.get("relation") == "same_research_problem" and set(check.get("urls", [])) <= member_urls
            for check in evt.get("identity_checks", [])
        ):
            raise ValueError("Distinct works reviewed as same_research_problem require research_theme and a shared research_question")
        valid_group = (grouping_type == "news_event" and data.get("same_concrete_event") is True) or (
            grouping_type == "research_theme" and data.get("same_research_problem") is True and len(research_question) >= 8)
        if not valid_group or any(e["relevance"] < 0.75 for e in evidence):
            raise ValueError("incoherent members must be split or rejected")
        title = str(data.get("event_title", "")).strip()
        sections = {key: str(data.get(key, "")).strip() for key in ("breakthrough", "problem", "research_value")}
        if not re.search(r"[\u4e00-\u9fff]", title) or any(not re.search(r"[\u4e00-\u9fff]", v) for v in sections.values()):
            raise ValueError("Chinese translation or structured summary missing")
        source_text = " ".join(a.get("title", "") + " " + a.get("summary", "")[:1800] for a in arts)
        claims = title + " " + " ".join(sections.values())
        if "氧化物" in claims and not re.search(r"oxide|氧化物|[A-Z][a-z]?[0-9]*O[0-9]+", source_text, re.I):
            raise ValueError("Unsupported oxide-material scope: source text does not support oxides; priority topics must not add material classes absent from the articles")
        rationales = {}
        for key in ("importance", "novelty", "impact"):
            rationale = str(data.get(key + "_rationale", "")).strip()
            if not rationale or key not in data:
                raise ValueError("score and rationale required")
            rationales[key] = score_number(data[key])
            rationales[key + "_rationale"] = rationale[:500]
        category = _clean_category(str(data.get("category", "")))
        if category not in {"LLM", "Agent", "AI for Science", "设计仿真", "数字孪生"}:
            raise ValueError("invalid category")
        dated = all(a.get("date_status") == "verified" for a in arts)
        summary = "核心突破：" + sections["breakthrough"] + "\n解决的问题：" + sections["problem"] + "\n研究价值：" + sections["research_value"]
        result.update(rationales)
        result.update(event_title=title, event_summary=summary, title=title, summary=summary,
                      category=category, **sections, short_summary=str(data.get("short_summary") or sections["breakthrough"])[:100],
                      evidence=evidence, top_articles=[e["index"] for e in evidence[:3]],
                      evidence_articles=[e["index"] for e in evidence[:3]],
                      coherence=round(coherent, 4) if len(ids)>1 else None,
                      grouping_type=grouping_type, research_question=research_question,
                      identity_validation={"method":"full_event_curation", "same_event":grouping_type == "news_event",
                                           "same_research_problem":grouping_type == "research_theme",
                                           "article_urls":[a["url"] for a in arts]},
                      coherence_status="measured" if len(ids)>1 else "not_applicable", quality_status="passed" if dated else "date_unverified",
                      quality_reasons=[] if dated else ["publication_date_unknown"], top_eligible=dated,
                      limitations=grounded_limitations(data.get("limitations", "未提供独立复现信息"), arts)[:500])
        return result
