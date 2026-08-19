"""
Candidate Pool — 源筛选漏斗机制。

流程:
  Source Registry / Discovery
        ↓
  L0 Structural Validation      (URL/Connector 结构验证，纯准入)
        ↓
  L1 Connectivity Check          (能不能连通，HTTP/API 可达性)
        ↓
  L2 Fetch & Extraction Check    (能不能抓到正文，提取器是否正常)
        ↓
  L3 Relevance & Spam Check      (是否 AI 相关，是否垃圾/低质)
        ↓
  PASS / REVIEW / REJECT

核心原则:
  - L0 只做确定性结构检查，不参与质量判断
  - L3 负责引入 Embedding/LLM/Agent 做复杂判断
  - 每个阶段输出结构化结果，方便写入 Source 数据库
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Decision outcomes
# ──────────────────────────────────────────────

PASS = "pass"
REVIEW = "review"
REJECT = "reject"

# ──────────────────────────────────────────────
# Structured result types
# ──────────────────────────────────────────────

@dataclass
class L0ValidationResult:
    """L0: 结构性准入验证 — 只回答"格式对不对"，不回答"质量好不好"。"""
    valid_url: bool = False
    domain_valid: bool = False
    domain: str = ""
    source_type: str = "unknown"       # rss | atom | github_repo | exa_search | web | arxiv
    connector_available: bool = False
    is_blocked: bool = False
    supported: bool = False             # 是否进入下一阶段
    status: str = "pending"            # pass / reject
    reasons: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    structural_hints: list[str] = field(default_factory=list)  # rss/atom 等提示，仅记录不加分


@dataclass
class L1ConnectivityResult:
    """L1: 连通性检查 — 目标是否可达。"""
    reachable: bool = False
    http_status: int = 0
    response_time_ms: int = 0
    status: str = "pending"
    reasons: list[str] = field(default_factory=list)
    error_type: str = ""
    error_message: str = ""


@dataclass
class L2ExtractionResult:
    """L2: 抓取与提取检查 — 能不能拿到正文。"""
    content_length: int = 0
    has_title: bool = False
    has_summary: bool = False
    extraction_method: str = ""        # feedparser | trafilatura | crawl4ai | github_api | arxiv_api
    sample_content: str = ""           # 前 500 字符，供 L3 使用
    status: str = "pending"
    reasons: list[str] = field(default_factory=list)
    error_type: str = ""
    error_message: str = ""


@dataclass
class L3RelevanceResult:
    """L3: 相关性与垃圾检测 — 是否 AI 相关，是否是垃圾。"""
    ai_relevant: bool = False
    spam_score: float = 0.0            # 0-1, 越高越可能是垃圾
    category_match: str = ""
    content_signals: list[str] = field(default_factory=list)
    status: str = "pending"
    reasons: list[str] = field(default_factory=list)


@dataclass
class CandidateDecision:
    """最终决策：PASS / REVIEW / REJECT"""
    endpoint: str = ""
    connector: str = ""
    category: str = ""
    decision: str = "reject"           # pass / review / reject
    confidence: float = 0.0            # 0-1
    score: float = 0.0                 # 0-100, for display
    l0: L0ValidationResult = field(default_factory=L0ValidationResult)
    l1: L1ConnectivityResult = field(default_factory=L1ConnectivityResult)
    l2: L2ExtractionResult = field(default_factory=L2ExtractionResult)
    l3: L3RelevanceResult = field(default_factory=L3RelevanceResult)


# ──────────────────────────────────────────────
# SQLite persistence
# ──────────────────────────────────────────────

def _init_db(db_path: str = "data/candidates.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            endpoint TEXT PRIMARY KEY,
            connector TEXT,
            category TEXT,
            discovered_via TEXT,
            discovered_at TEXT,
            score REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',  -- pending, rejected, promoted, review
            details TEXT,  -- JSON: validation results
            last_evaluated TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON candidates(status)")
    conn.commit()
    return conn


def _upsert_candidate(conn: sqlite3.Connection, candidate: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO candidates
           (endpoint, connector, category, discovered_via, discovered_at,
            score, status, details, last_evaluated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate["endpoint"],
            candidate.get("connector", "web"),
            candidate.get("category", "LLM"),
            candidate.get("discovered_via", "manual"),
            candidate.get("discovered_at", datetime.now(timezone.utc).isoformat()),
            candidate.get("score", 0),
            candidate.get("status", "pending"),
            candidate.get("details", "{}"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_pending_candidates(db_path: str = "data/candidates.db") -> list[dict]:
    conn = _init_db(db_path)
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status = 'pending' ORDER BY discovered_at DESC"
    ).fetchall()
    conn.close()
    cols = [desc[0] for desc in rows.description]
    return [dict(zip(cols, row)) for row in rows]


# ──────────────────────────────────────────────
# Available connectors (source of truth)
# ──────────────────────────────────────────────

# Import from source_resolver as the single source of truth
from fetcher.connector_registry import connector_registry

# ──────────────────────────────────────────────
# Blocklist — exact domain match only, no substring
# ──────────────────────────────────────────────

BLOCKED_DOMAINS = {
    # Advertising / analytics / tracking
    "ads.google.com", "analytics.google.com",
    # Cloudflare / CDN error pages
    "cloudflare.com", "cdn.cloudflare.com",
    # Known content farms (exact match only)
    "buzzfeed.com", "listverse.com",
    # E-commerce (not news/tech)
    "amazon.com", "calendly.com",
}


# ══════════════════════════════════════════════
# L0: Structural Validation
# ══════════════════════════════════════════════

def validate_structure(candidate: dict) -> L0ValidationResult:
    """
    L0: 结构性准入验证。

    只检查:
      1. URL/Domain 是否合法
      2. Source 类型是否可识别
      3. 是否存在对应 Connector
      4. 是否属于明确不支持的 Source (精确匹配 blocklist)
      5. 记录 RSS/Atom 等结构性 hint (仅记录不加分)

    不检查:
      - URL 是否"像博客"
      - 是否有 path
      - 是否包含 blog/news/article 关键词
    """
    result = L0ValidationResult()
    endpoint = candidate.get("endpoint") or candidate.get("url", "")
    connector = candidate.get("connector", "web")

    # ── 1. URL 合法性 ──────────────────────────────────
    if not endpoint:
        result.supported = False
        result.status = REJECT
        result.reasons.append("empty_endpoint")
        result.validation_errors.append("endpoint is empty")
        return result

    try:
        parsed = urlparse(endpoint.strip())
        if parsed.scheme not in ("http", "https"):
            result.valid_url = False
            result.supported = False
            result.status = REJECT
            result.reasons.append(f"invalid_scheme: {parsed.scheme}")
            result.validation_errors.append(f"Only http/https supported, got {parsed.scheme}")
            return result
        result.valid_url = True
    except Exception as e:
        result.valid_url = False
        result.supported = False
        result.status = REJECT
        result.reasons.append(f"parse_error")
        result.validation_errors.append(f"URL parse error: {type(e).__name__}: {e}")
        return result

    # ── 2. Domain 合法性 ───────────────────────────────
    try:
        domain = (parsed.hostname or "").lower()
        if not domain or "." not in domain:
            result.domain_valid = False
            result.supported = False
            result.status = REJECT
            result.reasons.append(f"invalid_domain: {domain}")
            result.validation_errors.append(f"Domain missing or invalid: {domain}")
            return result
        result.domain_valid = True
        result.domain = domain
    except Exception as e:
        result.domain_valid = False
        result.supported = False
        result.status = REJECT
        result.reasons.append(f"domain_error")
        result.validation_errors.append(f"Domain extraction error: {type(e).__name__}: {e}")
        return result

    # ── 3. Source 类型识别 ─────────────────────────────
    source_type = _infer_source_type(endpoint, connector)
    result.source_type = source_type

    # ── 4. Connector 存在性 ────────────────────────────
    result.connector_available = connector_registry.is_supported(connector)
    if not result.connector_available:
        result.supported = False
        result.status = REJECT
        result.reasons.append(f"no_connector: {connector}")
        result.validation_errors.append(
            f"No connector for type '{connector}'. "
            f"Available: {sorted(connector_registry.available_names())}"
        )
        return result

    # ── 5. Blocklist — exact domain match only ─────────
    # 精确匹配，不使用 `in` 做子串判断
    if domain in BLOCKED_DOMAINS:
        result.is_blocked = True
        result.supported = False
        result.status = REJECT
        result.reasons.append(f"blocked_domain: {domain}")
        result.validation_errors.append(f"Domain '{domain}' is in the blocklist")
        return result

    # Also check if any parent domain is blocked
    domain_parts = domain.split(".")
    for i in range(len(domain_parts)):
        parent = ".".join(domain_parts[i:])
        if parent in BLOCKED_DOMAINS:
            result.is_blocked = True
            result.supported = False
            result.status = REJECT
            result.reasons.append(f"blocked_parent_domain: {parent}")
            result.validation_errors.append(f"Parent domain '{parent}' is in the blocklist")
            return result

    # ── 6. Structural hints (informational only) ───────
    path = parsed.path.lower()
    if path.endswith(".rss") or path.endswith(".xml") or "/feed" in path or "/rss/" in path:
        result.structural_hints.append("rss_feed")
    if "/atom" in path or path.endswith(".atom"):
        result.structural_hints.append("atom_feed")
    if domain.endswith("arxiv.org"):
        result.structural_hints.append("arxiv_source")

    # ── Decision: structural validation passed ─────────
    result.supported = True
    result.status = PASS
    result.reasons.append("structural_valid")

    return result


def _infer_source_type(endpoint: str, connector: str) -> str:
    """根据 endpoint 和 connector 推断 source 类型。"""
    path = urlparse(endpoint).path.lower()

    if connector == "exa_search":
        return "exa_search"
    if connector == "github":
        return "github_repo"
    if connector == "arxiv":
        return "arxiv"

    # RSS / Atom detection
    if any(ext in path for ext in (".rss", ".xml", "/feed/", "/rss/", "/atom")):
        if "/atom" in path or path.endswith(".atom"):
            return "atom"
        return "rss"

    return "web"


# ══════════════════════════════════════════════
# L1: Connectivity Check
# ══════════════════════════════════════════════

async def check_connectivity(
    candidate: dict,
    session: aiohttp.ClientSession,
    l0: L0ValidationResult,
) -> L1ConnectivityResult:
    """
    L1: 连通性检查。

    只回答"能不能连上"，不关心内容。
    """
    result = L1ConnectivityResult()
    endpoint = candidate.get("endpoint") or candidate.get("url", "")

    start = time.monotonic()
    try:
        async with session.head(
            endpoint,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Weekly-AI-Report-Agent/1.0"},
            allow_redirects=True,
        ) as resp:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            result.response_time_ms = elapsed_ms
            result.http_status = resp.status

            if 200 <= resp.status < 400:
                result.reachable = True
                result.status = PASS
                result.reasons.append(f"http_{resp.status}")
            elif resp.status == 403:
                result.status = REVIEW
                result.reasons.append("http_403_forbidden")
                result.error_message = "Forbidden — may need auth or specific headers"
            elif resp.status >= 400:
                result.status = REJECT
                result.reasons.append(f"http_{resp.status}")
                result.error_message = f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result.response_time_ms = elapsed_ms
        result.status = REJECT
        result.error_type = "timeout"
        result.error_message = f"Connection timed out after 10s ({elapsed_ms}ms)"
        result.reasons.append("timeout")
    except aiohttp.ClientError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result.response_time_ms = elapsed_ms
        result.status = REJECT
        result.error_type = type(e).__name__
        result.error_message = str(e)
        result.reasons.append(f"client_error: {type(e).__name__}")
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result.response_time_ms = elapsed_ms
        result.status = REJECT
        result.error_type = type(e).__name__
        result.error_message = str(e)
        result.reasons.append(f"connectivity_error: {type(e).__name__}")

    return result


# ══════════════════════════════════════════════
# L2: Fetch & Extraction Check
# ══════════════════════════════════════════════

async def check_extraction(
    candidate: dict,
    session: aiohttp.ClientSession,
    l0: L0ValidationResult,
    l1: L1ConnectivityResult,
) -> L2ExtractionResult:
    """
    L2: 抓取与提取检查。

    使用实际的 Connector 提取内容，判断"能不能拿到正文"。
    """
    result = L2ExtractionResult()
    endpoint = candidate.get("endpoint") or candidate.get("url", "")
    connector = candidate.get("connector", "web")
    source = dict(candidate)
    # Normalize fields for extractors
    source.setdefault("url", endpoint)
    source.setdefault("endpoint", endpoint)

    # If L1 already rejected connectivity, skip
    if l1.status == REJECT:
        result.status = REJECT
        result.error_type = "connectivity_failed"
        result.error_message = f"L1 rejected: {', '.join(l1.reasons)}"
        result.reasons.append("connectivity_failed")
        return result

    try:
        from fetcher.extractors import get_extractor
        extractor = get_extractor(connector)

        # Call the actual extractor
        raw_articles = await extractor.extract(session, source)

        if raw_articles and len(raw_articles) > 0:
            first = raw_articles[0]
            result.has_title = bool(first.get("title", "").strip())
            summary = first.get("summary", "") or ""
            result.has_summary = len(summary) > 50
            result.content_length = len(summary)
            result.sample_content = summary[:500]

            if result.has_title and (result.has_summary or len(raw_articles) > 3):
                result.status = PASS
                result.extraction_method = f"{connector}_extractor"
                result.reasons.append(f"extracted_{len(raw_articles)}_articles")
            else:
                result.status = REVIEW
                result.reasons.append("thin_extraction")
                result.error_message = f"Only {len(raw_articles)} articles, title={bool(result.has_title)}, summary_len={result.content_length}"
        else:
            result.status = REJECT
            result.error_type = "no_content"
            result.error_message = f"Extractor returned 0 articles from {connector}"
            result.reasons.append("no_content_extracted")

    except Exception as e:
        result.status = REJECT
        result.error_type = type(e).__name__
        result.error_message = str(e)
        result.reasons.append(f"extraction_error: {type(e).__name__}")

    return result


# ══════════════════════════════════════════════
# L3: Relevance & Spam Check
# ══════════════════════════════════════════════

async def check_relevance(
    candidate: dict,
    l0: L0ValidationResult,
    l1: L1ConnectivityResult,
    l2: L2ExtractionResult,
) -> L3RelevanceResult:
    """
    L3: 相关性与垃圾检测。

    规则层面:
      - AI 关键词检测 (content-based, not URL-based)
      - 垃圾/广告信号检测
      - 重复域名检测

    复杂判断 (REVIEW 分支):
      - Embedding 语义相似度
      - LLM 权威性判断
    """
    result = L3RelevanceResult()
    category = candidate.get("category", "LLM")
    content = l2.sample_content or ""
    domain = l0.domain or ""

    # ── Content-based AI relevance ────────────────────
    if not content:
        # No content to analyze — mark for review
        result.status = REVIEW
        result.reasons.append("no_content_for_relevance_check")
        result.content_signals.append("insufficient_content")
        return result

    # AI-relevant keywords in content (not URL!)
    ai_keywords = {
        "AI", "LLM", "agent", "model", "training", "transformer",
        "simulation", "digital twin", "CAD", "CFD", "generative",
        "diffusion", "reinforcement learning", "fine-tune", "fine-tuning",
        "embedding", "inference", "RAG", "MCP",
        "大模型", "智能体", "仿真", "孪生", "生成式", "微调", "推理",
    }
    content_lower = content.lower()
    hits = {kw for kw in ai_keywords if kw.lower() in content_lower}
    ai_relevance_count = len(hits)

    if ai_relevance_count >= 3:
        result.ai_relevant = True
        result.content_signals.append(f"ai_keywords:{ai_relevance_count}")
    elif ai_relevance_count >= 1:
        result.content_signals.append(f"ai_keyword_weak:{ai_relevance_count}")

    # Category match (if candidate has category hint)
    if category and category.lower() in content_lower:
        result.category_match = category
        result.content_signals.append(f"category_match:{category}")

    # ── Spam/low-quality signals ───────────────────────
    spam_signals = []

    # Excessive ads/promo language
    promo_patterns = ["subscribe now", "click here", "buy now", "limited time",
                       "free trial", "sign up today", "discount"]
    promo_hits = sum(1 for p in promo_patterns if p in content_lower)
    if promo_hits >= 3:
        spam_signals.append(f"promotional_content:{promo_hits}")
        result.spam_score += 0.4

    # Very short content (likely landing page)
    if len(content) < 200 and len(content) > 0:
        spam_signals.append("short_content")
        result.spam_score += 0.2

    # High link-to-text ratio (content farm signal)
    if content and len(content) > 0:
        word_count = len(content.split())
        url_count = content.count("http")
        if word_count > 0 and url_count / max(word_count, 1) > 0.1:
            spam_signals.append("high_link_ratio")
            result.spam_score += 0.3

    result.content_signals.extend(spam_signals)
    result.spam_score = min(1.0, result.spam_score)

    # ── Decision ───────────────────────────────────────
    if result.ai_relevant and result.spam_score < 0.3:
        result.status = PASS
        result.reasons.append("ai_relevant_clean")
    elif result.ai_relevant and result.spam_score >= 0.3:
        result.status = REVIEW
        result.reasons.append("ai_relevant_but_spam_risk")
    elif not result.ai_relevant and result.spam_score < 0.3:
        result.status = REVIEW
        result.reasons.append("unclear_relevance")
    else:
        result.status = REJECT
        result.reasons.append(f"spam_score:{result.spam_score:.1f}")

    return result


# ══════════════════════════════════════════════
# Funnel orchestrator
# ══════════════════════════════════════════════

class CandidateEvaluator:
    """
    Evaluate candidates through the 4-stage funnel:
      L0 Structural → L1 Connectivity → L2 Extraction → L3 Relevance
      → PASS / REVIEW / REJECT
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def evaluate(
        self,
        candidates: list[dict],
        existing_endpoints: set[str],
        db_path: str = "data/candidates.db",
    ) -> tuple[list[dict], list[dict]]:
        """
        Run candidates through the funnel.

        Returns (promoted, rejected) lists.
        """
        conn = _init_db(db_path)

        # Filter: already in registry
        fresh = [c for c in candidates if c.get("endpoint", "") not in existing_endpoints]
        skipped = len(candidates) - len(fresh)
        if skipped:
            logger.info(f"Candidate pool: {skipped} already in registry, skipped")

        # Create aiohttp session for connectivity checks
        connector = aiohttp.TCPConnector(limit=5)
        session = aiohttp.ClientSession(connector=connector)

        promoted: list[dict] = []
        rejected: list[dict] = []

        try:
            tasks = [self._evaluate_one(session, c, conn) for c in fresh]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, r in enumerate(results):
                c = fresh[i]
                if isinstance(r, Exception):
                    logger.warning(f"Candidate eval error for {c.get('endpoint', '?')}: {r}")
                    _upsert_candidate(conn, {
                        **c, "status": REJECT, "score": 0,
                        "details": f"eval_error: {r}",
                    })
                    rejected.append(c)
                    continue

                decision: CandidateDecision = r  # type: ignore[assignment]
                if decision.decision == PASS:
                    promoted.append(self._decision_to_source(c, decision))
                    _upsert_candidate(conn, {
                        **c, "status": "promoted", "score": decision.score,
                        "details": self._decision_to_json(decision),
                    })
                elif decision.decision == REJECT:
                    rejected.append(c)
                    _upsert_candidate(conn, {
                        **c, "status": REJECT, "score": decision.score,
                        "details": self._decision_to_json(decision),
                    })
                else:  # REVIEW
                    _upsert_candidate(conn, {
                        **c, "status": "review", "score": decision.score,
                        "details": self._decision_to_json(decision),
                    })
                    logger.info(f"Candidate [{decision.endpoint}] marked for REVIEW")

        finally:
            await session.close()
            conn.close()

        logger.info(
            f"Candidate funnel: {len(promoted)} promoted, "
            f"{len(rejected)} rejected, "
            f"{sum(1 for c in fresh if c not in promoted and c not in rejected)} in review"
        )
        return promoted, rejected

    async def _evaluate_one(
        self,
        session: aiohttp.ClientSession,
        candidate: dict,
        conn: sqlite3.Connection,
    ) -> CandidateDecision:
        """Run a single candidate through L0 → L1 → L2 → L3."""
        decision = CandidateDecision(
            endpoint=candidate.get("endpoint", candidate.get("url", "")),
            connector=candidate.get("connector", "web"),
            category=candidate.get("category", "LLM"),
        )

        # ── L0: Structural Validation ──────────────────
        decision.l0 = validate_structure(candidate)
        if decision.l0.status == REJECT:
            decision.decision = REJECT
            return decision

        # ── L1: Connectivity Check ─────────────────────
        decision.l1 = await check_connectivity(candidate, session, decision.l0)
        if decision.l1.status == REJECT:
            decision.decision = REJECT
            return decision

        # ── L2: Fetch & Extraction Check ───────────────
        decision.l2 = await check_extraction(candidate, session, decision.l0, decision.l1)
        if decision.l2.status == REJECT:
            decision.decision = REJECT
            return decision

        # ── L3: Relevance & Spam Check ─────────────────
        decision.l3 = await check_relevance(
            candidate, decision.l0, decision.l1, decision.l2
        )

        # ── Final decision ─────────────────────────────
        decision.decision = decision.l3.status

        # Compute composite score (for display/sorting, not used for pass/reject)
        score = 0
        if decision.l1.reachable:
            score += 20
        if decision.l1.response_time_ms < 2000:
            score += 10
        if decision.l2.status == PASS:
            score += 30
        if decision.l2.has_title:
            score += 5
        if decision.l2.has_summary:
            score += 15
        if decision.l3.ai_relevant:
            score += 25
        if decision.l3.spam_score > 0:
            score -= int(decision.l3.spam_score * 20)
        decision.score = max(0, min(100, score))

        return decision

    # ── Helpers ────────────────────────────────────────

    def _decision_to_source(self, candidate: dict, decision: CandidateDecision) -> dict:
        """Convert a PASS decision into a source dict for merge."""
        return {
            **candidate,
            "score": decision.score,
            "weight": min(10, max(1, int(decision.score / 10))),
            "trust": 0.5,  # Start at 0.5, proven sources get 1.0
            "eval_score": decision.score,
        }

    def _decision_to_json(self, decision: CandidateDecision) -> str:
        """Serialize CandidateDecision to JSON string for SQLite storage."""
        return json.dumps({
            "decision": decision.decision,
            "score": decision.score,
            "l0": asdict(decision.l0),
            "l1": asdict(decision.l1),
            "l2": {
                "status": decision.l2.status,
                "content_length": decision.l2.content_length,
                "has_title": decision.l2.has_title,
                "has_summary": decision.l2.has_summary,
                "extraction_method": decision.l2.extraction_method,
                "reasons": decision.l2.reasons,
                "error_type": decision.l2.error_type,
                "error_message": decision.l2.error_message[:200],
            },
            "l3": {
                "status": decision.l3.status,
                "ai_relevant": decision.l3.ai_relevant,
                "spam_score": decision.l3.spam_score,
                "category_match": decision.l3.category_match,
                "content_signals": decision.l3.content_signals,
                "reasons": decision.l3.reasons,
            },
        }, ensure_ascii=False)

