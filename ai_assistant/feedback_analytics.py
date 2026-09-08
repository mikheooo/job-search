"""Stage 90 / Stage 90.1 / Stage 91: Feedback Analytics & Preference Calibration Subsystem.

Provides:
1. Strict evidence provenance auditing and validation:
   - EXPLICIT_HUMAN_FEEDBACK (Telegram button clicks by authorized owner on genuine production vacancies)
   - EXPLICIT_HUMAN_APPLICATION_INTENT (Explicit human CLI review/move with human confirmation)
   - CONFIRMED_REAL_APPLICATION (Verified external application submission on production platform)
   - Filters out AUTOMATED_PIPELINE_STATE, SYSTEM_GENERATED_STATE, LEGACY_STATE, TEST_OR_DRY_RUN, UNKNOWN.
2. Temporal supersession & progression:
   - Newer user actions supersede older preferences for the same vacancy without destroying audit logs.
   - Multi-step application progression (INTERESTED -> PREPARE -> SUBMITTED) counts as 1 opportunity with upgraded strength.
   - Follow-up reason clicks correlate to the same event.
3. Distinction between NOT_INTERESTED (strong negative) and SKIP (weak/contextual negative).
4. Reason-aware signal routing (COMPANY reasons stay company-specific; SALARY/LOCATION do not penalize role families; TECH_STACK targets skills).
5. Dimension-specific calibration readiness & feedback coverage metrics.
6. Safe bounded ranking adjustments (clamped, explainable, zero candidate profile mutation).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ai_assistant import config, db
from ai_assistant.schema import Vacancy, is_genuine_production_vacancy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal Hierarchy & Provenance Taxonomy
# ---------------------------------------------------------------------------

class SignalStrength(str, Enum):
    VERY_STRONG_POSITIVE = "VERY_STRONG_POSITIVE"  # weight 1.5, raw_val +1.0 (PREPARE, APPLIED, SUBMITTED)
    STRONG_POSITIVE = "STRONG_POSITIVE"            # weight 1.0, raw_val +0.8 (INTERESTED)
    STRONG_NEGATIVE = "STRONG_NEGATIVE"            # weight 1.0, raw_val -0.8 (NOT_INTERESTED)
    NEGATIVE = "NEGATIVE"                          # weight 0.8, raw_val -0.5 (SKIP with negative reason)
    WEAK_NEGATIVE = "WEAK_NEGATIVE"                # weight 0.5, raw_val -0.3 (Plain SKIP / contextual pass)


SIGNAL_WEIGHT_MAP: dict[SignalStrength, tuple[float, float]] = {
    SignalStrength.VERY_STRONG_POSITIVE: (1.5, 1.0),
    SignalStrength.STRONG_POSITIVE: (1.0, 0.8),
    SignalStrength.STRONG_NEGATIVE: (1.0, -0.8),
    SignalStrength.NEGATIVE: (0.8, -0.5),
    SignalStrength.WEAK_NEGATIVE: (0.5, -0.3),
}


class EvidenceProvenance(str, Enum):
    EXPLICIT_HUMAN_FEEDBACK = "EXPLICIT_HUMAN_FEEDBACK"               # Telegram button clicks by authorized owner
    EXPLICIT_HUMAN_APPLICATION_INTENT = "EXPLICIT_HUMAN_APPLICATION_INTENT" # Explicit CLI review/move with human confirmation
    CONFIRMED_REAL_APPLICATION = "CONFIRMED_REAL_APPLICATION"         # Verified external application submission
    AUTOMATED_PIPELINE_STATE = "AUTOMATED_PIPELINE_STATE"             # Scraped chats, auto-classification, pipeline updates
    SYSTEM_GENERATED_STATE = "SYSTEM_GENERATED_STATE"                 # System/matcher internal state
    LEGACY_STATE = "LEGACY_STATE"                                     # Legacy or unverified historical database rows
    TEST_OR_DRY_RUN = "TEST_OR_DRY_RUN"                               # Synthetic callbacks, fixtures, dry-runs, vacancies_json
    UNKNOWN = "UNKNOWN"


PROVENANCE_PRECEDENCE: dict[EvidenceProvenance, int] = {
    EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK: 1,
    EvidenceProvenance.CONFIRMED_REAL_APPLICATION: 2,
    EvidenceProvenance.EXPLICIT_HUMAN_APPLICATION_INTENT: 3,
    EvidenceProvenance.AUTOMATED_PIPELINE_STATE: 10,
    EvidenceProvenance.SYSTEM_GENERATED_STATE: 11,
    EvidenceProvenance.LEGACY_STATE: 12,
    EvidenceProvenance.TEST_OR_DRY_RUN: 20,
    EvidenceProvenance.UNKNOWN: 99,
}


class SkipReason(str, Enum):
    ROLE = "ROLE"
    SALARY = "SALARY"
    COMPANY = "COMPANY"
    LOCATION = "LOCATION"
    TECH_STACK = "TECH_STACK"
    SENIORITY = "SENIORITY"
    LANGUAGE = "LANGUAGE"
    EMPLOYMENT_TYPE = "EMPLOYMENT_TYPE"
    TOO_COMPLEX = "TOO_COMPLEX"
    TOO_JUNIOR = "TOO_JUNIOR"
    TOO_SENIOR = "TOO_SENIOR"
    REMOTE = "REMOTE"
    CAREER_GROWTH = "CAREER_GROWTH"
    ALREADY_SEEN = "ALREADY_SEEN"
    OTHER = "OTHER"


FeedbackReason = SkipReason


# Canonical tracked technology/skill tokens for preference analytics
TRACKED_SKILL_PATTERNS: dict[str, list[str]] = {
    "python": [r"\bpython\b", r"\bpython3\b"],
    "n8n": [r"\bn8n\b", r"\bn8n\.io\b"],
    "ai_agents": [r"\bai agent\b", r"\bai agents\b", r"\bagentic\b", r"\bautogen\b", r"\bcrewai\b"],
    "llm": [r"\bllm\b", r"\bllms\b", r"\bgpt\b", r"\bopenai\b", r"\bgemini\b", r"\bclaude\b", r"\brag\b"],
    "fastapi": [r"\bfastapi\b"],
    "django": [r"\bdjango\b"],
    "linux": [r"\blinux\b", r"\bubuntu\b", r"\bdebian\b", r"\bcentos\b"],
    "sql": [r"\bsql\b", r"\bpostgres\b", r"\bpostgresql\b", r"\bmysql\b", r"\bsqlite\b"],
    "docker": [r"\bdocker\b", r"\bcontainer\b", r"\bcontainers\b"],
    "active_directory": [r"\bactive directory\b", r"\bad\b", r"\bldap\b"],
    "langchain": [r"\blangchain\b", r"\blanggraph\b"],
    "kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "aws": [r"\baws\b", r"\bamazon web services\b"],
    "telethon": [r"\btelethon\b", r"\baiogram\b", r"\bpyrogram\b"],
}


# Canonical Role Concept Patterns
ROLE_CONCEPT_PATTERNS: list[tuple[str, list[str]]] = [
    ("AI Automation Engineer", [r"ai automation", r"n8n", r"agentic", r"ai engineer", r"ai developer", r"ai specialist"]),
    ("Application Support Engineer", [r"application support", r"app support", r"l2 support", r"l3 support"]),
    ("Technical Support Engineer", [r"tech support", r"technical support", r"help desk", r"it support", r"service desk"]),
    ("Python Developer", [r"python developer", r"python engineer", r"python backend", r"backend developer", r"backend engineer"]),
    ("System Administrator", [r"system administrator", r"sysadmin", r"linux administrator", r"windows administrator"]),
    ("DevOps Engineer", [r"devops", r"site reliability", r"sre", r"infrastructure engineer"]),
    ("Data Engineer", [r"data engineer", r"etl developer", r"pipeline engineer"]),
]


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class PreferenceEvidenceEvent:
    vacancy_stable_id: str = ""
    action: str = ""
    signal_strength: SignalStrength = SignalStrength.STRONG_POSITIVE
    occurred_at: str = ""
    created_at: str = ""
    provenance: EvidenceProvenance = EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK
    is_production_eligible: bool = True
    human_confirmed: bool = True
    correlation_key: str = ""
    source_table: str = ""
    event_id: str = ""
    skip_reason: str | None = None
    feedback_reason: str | None = None
    title: str = ""
    company: str = ""
    source: str = ""
    role_family: str = "OTHER"
    role_concept: str = ""
    skills: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    match_score: float | None = None
    match_decision: str | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.occurred_at and self.created_at:
            self.occurred_at = self.created_at
        elif not self.created_at and self.occurred_at:
            self.created_at = self.occurred_at
        if not self.correlation_key:
            self.correlation_key = self.vacancy_stable_id
        if not self.event_id:
            self.event_id = f"ev_{self.vacancy_stable_id}_{self.action}"
        if not self.feedback_reason and self.skip_reason:
            self.feedback_reason = self.skip_reason
        elif not self.skip_reason and self.feedback_reason:
            self.skip_reason = self.feedback_reason

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_strength"] = self.signal_strength.value if hasattr(self.signal_strength, "value") else str(self.signal_strength)
        d["provenance"] = self.provenance.value if hasattr(self.provenance, "value") else str(self.provenance)
        return d


# Alias for backward compatibility in internal functions
PreferenceEvent = PreferenceEvidenceEvent


@dataclass
class DimensionSignal:
    dimension_key: str
    name: str
    positive_count: int = 0
    negative_count: int = 0
    total_evidence: int = 0
    raw_signal: float = 0.0         # weighted sum of signals [-1.0, +1.0]
    confidence: float = 0.0         # [0.0, 1.0]
    status: str = "NO_EVIDENCE"     # NO_EVIDENCE, RECORD_ONLY, WEAK_HINT, LOW_CONFIDENCE, HIGH_CONFIDENCE, AMBIGUOUS_PREFERENCE
    readiness: str = "COLLECTING"   # NO_EVIDENCE, COLLECTING, CALIBRATION_ELIGIBLE
    last_feedback_at: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreferenceProfile:
    generated_at: str = ""
    raw_evidence_count: int = 0
    production_eligible_events_count: int = 0
    excluded_events_count: int = 0
    total_evidence_events: int = 0
    unique_vacancies_evaluated: int = 0
    events_needed_for_threshold: int = 0
    calibration_readiness: str = "NO_EVIDENCE"  # NO_EVIDENCE, COLLECTING, LOW_CONFIDENCE, CALIBRATION_ELIGIBLE
    calibration_status: str = "INSUFFICIENT_EVIDENCE_FOR_AUTOMATIC_CALIBRATION"
    role_families: dict[str, DimensionSignal] = field(default_factory=dict)
    role_concepts: dict[str, DimensionSignal] = field(default_factory=dict)
    skills: dict[str, DimensionSignal] = field(default_factory=dict)
    companies: dict[str, DimensionSignal] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    feedback_reasons: dict[str, int] = field(default_factory=dict)
    provenance_summary: dict[str, int] = field(default_factory=dict)
    dimension_readiness: dict[str, str] = field(default_factory=dict)
    selection_bias_notes: list[str] = field(default_factory=list)
    evidence_events: list[PreferenceEvidenceEvent] = field(default_factory=list)

    def __post_init__(self):
        if not self.total_evidence_events:
            self.total_evidence_events = self.production_eligible_events_count
        if not self.unique_vacancies_evaluated:
            self.unique_vacancies_evaluated = self.production_eligible_events_count
        if not self.production_eligible_events_count and self.total_evidence_events:
            self.production_eligible_events_count = self.total_evidence_events
        threshold = getattr(config, "PREFERENCE_MIN_EVIDENCE_THRESHOLD", 5)
        self.events_needed_for_threshold = max(0, threshold - self.production_eligible_events_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "raw_evidence_count": self.raw_evidence_count,
            "production_eligible_events_count": self.production_eligible_events_count,
            "excluded_events_count": self.excluded_events_count,
            "total_evidence_events": self.total_evidence_events,
            "unique_vacancies_evaluated": self.unique_vacancies_evaluated,
            "events_needed_for_threshold": self.events_needed_for_threshold,
            "calibration_readiness": self.calibration_readiness,
            "calibration_status": self.calibration_status,
            "provenance_summary": self.provenance_summary,
            "dimension_readiness": self.dimension_readiness,
            "role_families": {k: v.to_dict() for k, v in self.role_families.items()},
            "role_concepts": {k: v.to_dict() for k, v in self.role_concepts.items()},
            "skills": {k: v.to_dict() for k, v in self.skills.items()},
            "companies": {k: v.to_dict() for k, v in self.companies.items()},
            "skip_reasons": self.skip_reasons,
            "feedback_reasons": self.feedback_reasons,
            "selection_bias_notes": self.selection_bias_notes,
            "evidence_events": [e.to_dict() for e in self.evidence_events],
        }


# ---------------------------------------------------------------------------
# Feature Extraction Helpers
# ---------------------------------------------------------------------------

def extract_role_concept(title: str) -> str:
    """Normalize a vacancy title into a canonical role concept."""
    if not title:
        return "Other"
    tl = title.lower()
    for concept_name, patterns in ROLE_CONCEPT_PATTERNS:
        for pat in patterns:
            if re.search(pat, tl):
                return concept_name
    return title.strip()[:40]


def extract_vacancy_skills(title: str, description: str = "") -> list[str]:
    """Extract canonical tracked skill tags from text."""
    combined = f"{title or ''} {description or ''}".lower()
    matched = []
    for skill_name, patterns in TRACKED_SKILL_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, combined):
                matched.append(skill_name)
                break
    return matched


def extract_vacancy_seniority(title: str) -> list[str]:
    """Extract seniority indicators from title."""
    tl = (title or "").lower()
    levels = []
    if re.search(r"\b(junior|jun|джуниор|младший)\b", tl):
        levels.append("junior")
    if re.search(r"\b(middle|mid|мидл)\b", tl):
        levels.append("middle")
    if re.search(r"\b(senior|sr|сеньор|старший|lead|лид|руководитель)\b", tl):
        levels.append("senior")
    return levels or ["mid_or_unspecified"]


def calculate_recency_weight(created_at: str, now_dt: datetime | None = None) -> float:
    """Compute recency decay weight based on age in days."""
    if not created_at:
        return 0.5
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (now_dt - ts).total_seconds() / 86400.0
    except Exception:
        return 0.5

    if age_days <= 30.0:
        return 1.0
    elif age_days <= 90.0:
        return 0.7
    elif age_days <= 180.0:
        return 0.4
    else:
        return 0.2


# ---------------------------------------------------------------------------
# Signal Aggregator Engine
# ---------------------------------------------------------------------------

def aggregate_events_to_signal(
    dimension_key: str,
    name: str,
    events: list[PreferenceEvidenceEvent],
    now_dt: datetime | None = None,
) -> DimensionSignal:
    """Deterministic aggregation of events into a confidence-scored DimensionSignal."""
    if not events:
        return DimensionSignal(dimension_key=dimension_key, name=name)

    if now_dt is None:
        now_dt = datetime.now(timezone.utc)

    pos_count = 0
    neg_count = 0
    weighted_pos = 0.0
    weighted_neg = 0.0
    recency_sum = 0.0
    latest_ts = None

    for ev in events:
        w, val = SIGNAL_WEIGHT_MAP.get(ev.signal_strength, (1.0, 0.8))
        rec_w = calculate_recency_weight(ev.occurred_at, now_dt)
        recency_sum += rec_w

        if val > 0:
            pos_count += 1
            weighted_pos += w * val * rec_w
        else:
            neg_count += 1
            weighted_neg += w * abs(val) * rec_w

        if not latest_ts or ev.occurred_at > latest_ts:
            latest_ts = ev.occurred_at

    total_count = pos_count + neg_count
    total_weighted = weighted_pos + weighted_neg
    avg_recency = recency_sum / total_count if total_count > 0 else 0.5

    if total_weighted <= 0.0:
        raw_signal = 0.0
        consistency = 0.0
    else:
        raw_signal = (weighted_pos - weighted_neg) / total_weighted
        consistency = abs(weighted_pos - weighted_neg) / total_weighted

    # Minimum evidence & count scaling
    count_factor = min(1.0, total_count / 10.0)

    # Ambiguity detection: mixed signals with >= 3 events and low consistency
    is_ambiguous = (total_count >= 3 and consistency < 0.40)

    notes = []
    if is_ambiguous:
        status = "AMBIGUOUS_PREFERENCE"
        readiness = "COLLECTING"
        confidence = 0.0
        notes.append("Contradictory positive and negative feedback detected; confidence suppressed to zero.")
    elif total_count == 1:
        status = "RECORD_ONLY"
        readiness = "NO_EVIDENCE"
        confidence = 0.0
        notes.append("Single feedback event recorded (below minimum calibration threshold).")
    elif total_count == 2:
        status = "WEAK_HINT"
        readiness = "COLLECTING"
        confidence = round(0.20 * consistency * avg_recency, 3)
        notes.append("Weak hint (2 events); conservative influence.")
    elif total_count < 5:
        status = "LOW_CONFIDENCE"
        readiness = "COLLECTING"
        confidence = round(0.50 * consistency * avg_recency, 3)
        notes.append(f"Low-confidence preference ({total_count} events).")
    else:
        status = "HIGH_CONFIDENCE"
        readiness = "CALIBRATION_ELIGIBLE"
        confidence = round(count_factor * consistency * avg_recency, 3)
        notes.append(f"High-confidence preference ({total_count} events).")

    return DimensionSignal(
        dimension_key=dimension_key,
        name=name,
        positive_count=pos_count,
        negative_count=neg_count,
        total_evidence=total_count,
        raw_signal=round(raw_signal, 3),
        confidence=confidence,
        status=status,
        readiness=readiness,
        last_feedback_at=latest_ts,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Canonical Evidence Extractor & Provenance Auditing (Stage 90.1 / Stage 91)
# ---------------------------------------------------------------------------

def extract_all_preference_evidence(
    now_dt: datetime | None = None,
    storage_dir: str | None = None,
    include_non_production: bool = False,
) -> list[PreferenceEvidenceEvent]:
    """Extract, audit provenance, and deduplicate all preference signals across canonical state tables.
    
    Stage 91 Enhancements:
    1. Distinction between NOT_INTERESTED (STRONG_NEGATIVE) and SKIP (WEAK_NEGATIVE).
    2. Optional structured reasons preserved.
    3. Temporal supersession: later human actions supersede earlier feedback for the same vacancy.
    4. Progression merging: multi-step state advances (INTERESTED -> PREPARE -> SUBMITTED) merge into 1 opportunity with upgraded strength.
    """
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()

    # Cache all vacancies metadata
    cur.execute('''
        SELECT stable_id, source, source_job_id, title, company, description, job_url, match_score, match_decision
        FROM vacancies
    ''')
    vac_rows = cur.fetchall()
    vac_map: dict[str, dict[str, Any]] = {}
    for r in vac_rows:
        sid = r[0]
        src = r[1] or ""
        sjid = r[2] or ""
        title = r[3] or ""
        comp = r[4] or ""
        desc = r[5] or ""
        jurl = r[6] or ""
        
        dummy_vac = Vacancy(
            source=src,
            source_job_id=sjid,
            title=title,
            company=comp,
            description=desc,
            job_url=jurl or f"https://hh.ru/{sjid}",
        )
        is_gen_prod, prod_reason = is_genuine_production_vacancy(dummy_vac)

        vac_map[sid] = {
            "source": src,
            "source_job_id": sjid,
            "title": title,
            "company": comp,
            "description": desc,
            "job_url": jurl,
            "match_score": r[7],
            "match_decision": r[8],
            "role_concept": extract_role_concept(title),
            "skills": extract_vacancy_skills(title, desc),
            "seniority": extract_vacancy_seniority(title),
            "is_genuine_production": is_gen_prod,
            "production_provenance_reason": prod_reason,
        }

    try:
        from ai_assistant.matcher import classify_role_family
        for sid, vinfo in vac_map.items():
            rf = classify_role_family(vinfo["title"], vinfo["description"])
            vinfo["role_family"] = rf.value if hasattr(rf, "value") else str(rf)
    except Exception:
        for sid, vinfo in vac_map.items():
            vinfo["role_family"] = "OTHER"

    raw_events: list[PreferenceEvidenceEvent] = []
    owner_id = str(getattr(config, "TELEGRAM_OWNER_ID", "") or "392046103").strip()

    # 1. Telegram Feedback Records
    cur.execute('''
        SELECT id, vacancy_stable_id, action, telegram_user_id, callback_query_id, created_at, payload_json
        FROM telegram_feedback_records
        ORDER BY created_at ASC, id ASC
    ''')
    for row in cur.fetchall():
        rec_id, sid, act, uid, cb_id, ts, p_json = row[0], row[1], row[2], str(row[3] or ""), str(row[4] or ""), row[5], row[6]
        payload = {}
        try:
            if p_json:
                payload = json.loads(p_json)
        except Exception:
            pass

        skip_res = payload.get("skip_reason") or payload.get("feedback_reason")
        vinfo = vac_map.get(sid, {})
        is_gen_prod = vinfo.get("is_genuine_production", False)

        if act == "INTERESTED":
            strength = SignalStrength.STRONG_POSITIVE
        elif act == "PREPARE_APPLICATION":
            strength = SignalStrength.VERY_STRONG_POSITIVE
        elif act == "NOT_INTERESTED":
            strength = SignalStrength.STRONG_NEGATIVE
        elif act == "SKIP":
            strength = SignalStrength.NEGATIVE if skip_res else SignalStrength.WEAK_NEGATIVE
        else:
            strength = SignalStrength.STRONG_POSITIVE

        # Provenance validation for Telegram feedback
        is_test_cb = (
            cb_id.startswith("live_val_") or cb_id.startswith("test_") or
            cb_id.startswith("mock_") or cb_id.startswith("synth_") or
            not cb_id.isdigit()
        )
        is_authorized_owner = (uid == owner_id or uid == "392046103")

        notes = []
        if is_test_cb:
            prov = EvidenceProvenance.TEST_OR_DRY_RUN
            is_eligible = False
            notes.append(f"Synthetic validation callback ID '{cb_id}' excluded from production preference ground truth.")
        elif not is_authorized_owner:
            prov = EvidenceProvenance.UNKNOWN
            is_eligible = False
            notes.append(f"Unauthorized user ID '{uid}' excluded.")
        elif not is_gen_prod:
            prov = EvidenceProvenance.TEST_OR_DRY_RUN
            is_eligible = False
            notes.append(f"Non-production vacancy ({vinfo.get('production_provenance_reason')}) excluded.")
        else:
            prov = EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK
            is_eligible = True
            notes.append("Legitimate Telegram client interaction by authorized owner on genuine production vacancy.")

        raw_events.append(PreferenceEvidenceEvent(
            event_id=f"tg_fb_{rec_id}",
            vacancy_stable_id=sid,
            action=act,
            signal_strength=strength,
            occurred_at=ts,
            provenance=prov,
            is_production_eligible=is_eligible,
            human_confirmed=(prov == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK),
            correlation_key=sid,
            source_table="telegram_feedback_records",
            skip_reason=skip_res,
            feedback_reason=skip_res,
            title=vinfo.get("title", ""),
            company=vinfo.get("company", ""),
            source=vinfo.get("source", ""),
            role_family=vinfo.get("role_family", "OTHER"),
            role_concept=vinfo.get("role_concept", ""),
            skills=vinfo.get("skills", []),
            seniority=vinfo.get("seniority", []),
            match_score=vinfo.get("match_score"),
            match_decision=vinfo.get("match_decision"),
            notes=notes,
        ))

    # 2. Application Reviews
    cur.execute('''
        SELECT vacancy_stable_id, status, review_version, note, updated_at
        FROM application_reviews
        WHERE status IN ('APPROVED', 'REJECTED')
        ORDER BY updated_at ASC
    ''')
    for row in cur.fetchall():
        sid, status, rev_ver, note, ts = row[0], row[1], row[2], row[3] or "", row[4] or datetime.now(timezone.utc).isoformat()
        vinfo = vac_map.get(sid, {})
        is_gen_prod = vinfo.get("is_genuine_production", False)

        if status == "APPROVED":
            act = "PREPARE_APPLICATION"
            strength = SignalStrength.VERY_STRONG_POSITIVE
        else:
            act = "SKIP"
            strength = SignalStrength.WEAK_NEGATIVE

        notes = []
        is_fixture = (vinfo.get("source") == "vacancies_json" or sid.startswith("vacancies_json:"))
        is_human_cli = ("Approved via Web UI" in note or "human_confirmed" in note or "manual_human_review" in note)

        if is_fixture:
            prov = EvidenceProvenance.TEST_OR_DRY_RUN
            is_eligible = False
            notes.append("Fixture from vacancies_json excluded from production preference ground truth.")
        elif not is_gen_prod:
            prov = EvidenceProvenance.TEST_OR_DRY_RUN
            is_eligible = False
            notes.append(f"Non-production vacancy ({vinfo.get('production_provenance_reason')}) excluded.")
        elif is_human_cli:
            prov = EvidenceProvenance.EXPLICIT_HUMAN_APPLICATION_INTENT
            is_eligible = True
            notes.append(f"Explicit human review note: '{note}'.")
        else:
            prov = EvidenceProvenance.AUTOMATED_PIPELINE_STATE
            is_eligible = False
            notes.append("Unverified review entry without explicit human confirmation record.")

        raw_events.append(PreferenceEvidenceEvent(
            event_id=f"app_rev_{sid}",
            vacancy_stable_id=sid,
            action=act,
            signal_strength=strength,
            occurred_at=ts,
            provenance=prov,
            is_production_eligible=is_eligible,
            human_confirmed=(prov == EvidenceProvenance.EXPLICIT_HUMAN_APPLICATION_INTENT),
            correlation_key=sid,
            source_table="application_reviews",
            skip_reason="REVIEW_REJECTED" if status == "REJECTED" else None,
            feedback_reason="REVIEW_REJECTED" if status == "REJECTED" else None,
            title=vinfo.get("title", ""),
            company=vinfo.get("company", ""),
            source=vinfo.get("source", ""),
            role_family=vinfo.get("role_family", "OTHER"),
            role_concept=vinfo.get("role_concept", ""),
            skills=vinfo.get("skills", []),
            seniority=vinfo.get("seniority", []),
            match_score=vinfo.get("match_score"),
            match_decision=vinfo.get("match_decision"),
            notes=notes,
        ))

    # 3. HH Applications & Real Submissions
    cur.execute('''
        SELECT application_id, vacancy_stable_id, state, last_transition_reason, created_at, updated_at
        FROM hh_applications
        ORDER BY updated_at ASC, created_at ASC
    ''')
    for row in cur.fetchall():
        app_id, sid, status, note, created_ts, updated_ts = row[0], row[1] or "", row[2], row[3] or "", row[4], row[5]
        if not sid:
            continue
        vinfo = vac_map.get(sid, {})
        is_gen_prod = vinfo.get("is_genuine_production", False)

        notes = []
        is_real_confirmed = (
            status == "SUBMITTED" and (
                "controlled_runner_submit_confirmed" in note or
                "questionnaire_submitted_with_human_confirmation" in note
            )
        )
        is_chat_only = ("message_classified_as_" in note or "sensitive_topic_requires_human_decision" in note)

        if not is_gen_prod:
            prov = EvidenceProvenance.TEST_OR_DRY_RUN
            is_eligible = False
            notes.append(f"Non-production vacancy ({vinfo.get('production_provenance_reason')}) excluded.")
        elif is_chat_only:
            prov = EvidenceProvenance.AUTOMATED_PIPELINE_STATE
            is_eligible = False
            notes.append(f"Inbound recruiter chat message state '{note}' is not candidate preference.")
        elif is_real_confirmed:
            prov = EvidenceProvenance.CONFIRMED_REAL_APPLICATION
            is_eligible = True
            notes.append(f"Confirmed real application with human confirmation note: '{note}'.")
        else:
            prov = EvidenceProvenance.AUTOMATED_PIPELINE_STATE
            is_eligible = False
            notes.append(f"Unconfirmed application state '{status}' ({note}).")

        raw_events.append(PreferenceEvidenceEvent(
            event_id=f"hh_app_{app_id}",
            vacancy_stable_id=sid,
            action="APPLIED",
            signal_strength=SignalStrength.VERY_STRONG_POSITIVE,
            occurred_at=updated_ts or created_ts,
            provenance=prov,
            is_production_eligible=is_eligible,
            human_confirmed=(prov == EvidenceProvenance.CONFIRMED_REAL_APPLICATION),
            correlation_key=sid,
            source_table="hh_applications",
            title=vinfo.get("title", ""),
            company=vinfo.get("company", ""),
            source=vinfo.get("source", ""),
            role_family=vinfo.get("role_family", "OTHER"),
            role_concept=vinfo.get("role_concept", ""),
            skills=vinfo.get("skills", []),
            seniority=vinfo.get("seniority", []),
            match_score=vinfo.get("match_score"),
            match_decision=vinfo.get("match_decision"),
            notes=notes,
        ))

    conn.close()

    if include_non_production:
        return raw_events

    # Group by correlation_key (vacancy_stable_id)
    events_by_key: dict[str, list[PreferenceEvidenceEvent]] = {}
    for ev in raw_events:
        events_by_key.setdefault(ev.correlation_key, []).append(ev)

    filtered_ground_truth: list[PreferenceEvidenceEvent] = []
    for key, group in events_by_key.items():
        eligible = [e for e in group if e.is_production_eligible]
        if not eligible:
            continue

        # Sort chronologically by occurred_at
        eligible.sort(key=lambda x: x.occurred_at or "")

        # If multiple Telegram feedback actions exist (e.g. user changed mind or added reason)
        tg_events = [e for e in eligible if e.provenance == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK]
        if tg_events:
            latest_tg = tg_events[-1]
            # If earlier feedback had a reason and latest didn't, inherit reason
            if not latest_tg.feedback_reason:
                for past in reversed(tg_events[:-1]):
                    if past.feedback_reason:
                        latest_tg.feedback_reason = past.feedback_reason
                        latest_tg.skip_reason = past.skip_reason
                        break
            filtered_ground_truth.append(latest_tg)
        else:
            # Pick highest precedence
            eligible.sort(key=lambda x: PROVENANCE_PRECEDENCE.get(x.provenance, 99))
            filtered_ground_truth.append(eligible[0])

    return filtered_ground_truth


# ---------------------------------------------------------------------------
# Feedback Coverage Metrics (Stage 91 Task 12)
# ---------------------------------------------------------------------------

def get_feedback_coverage_metrics() -> dict[str, Any]:
    """Compute read-only feedback coverage metrics across digest-delivered vacancies.
    
    Stage 91.1 Clarification:
    Separates Telegram digest button interactions from external confirmed job applications.
    """
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()

    # Total delivered unique vacancies
    cur.execute('''
        SELECT delivery_key
        FROM telegram_delivery_records
        WHERE delivery_key LIKE 'digest:%' AND status = 'DELIVERED'
    ''')
    deliv_keys = cur.fetchall()
    delivered_vids: set[str] = set()
    for (k,) in deliv_keys:
        vid = k.split("digest:", 1)[-1].strip()
        if vid and not vid.startswith("can_"):
            delivered_vids.add(vid)

    conn.close()

    # Extracted ground truth human events
    evidence_events = extract_all_preference_evidence(include_non_production=False)
    
    tg_events = [e for e in evidence_events if e.provenance == EvidenceProvenance.EXPLICIT_HUMAN_FEEDBACK]
    app_events = [e for e in evidence_events if e.provenance == EvidenceProvenance.CONFIRMED_REAL_APPLICATION]
    
    tg_vids = {e.vacancy_stable_id for e in tg_events}
    app_vids = {e.vacancy_stable_id for e in app_events}
    all_human_vids = {e.vacancy_stable_id for e in evidence_events}

    delivered_count = max(len(delivered_vids), len(all_human_vids))
    tg_count = len(tg_vids)
    app_count = len(app_vids)
    canonical_human_count = len(all_human_vids)

    tg_coverage_rate = round(tg_count / delivered_count, 3) if delivered_count > 0 else 0.0
    human_coverage_rate = round(canonical_human_count / delivered_count, 3) if delivered_count > 0 else 0.0

    pos_count = sum(1 for e in evidence_events if e.action in ("INTERESTED", "PREPARE_APPLICATION", "APPLIED"))
    neg_count = sum(1 for e in evidence_events if e.action == "NOT_INTERESTED")
    skip_count = sum(1 for e in evidence_events if e.action == "SKIP")
    prep_count = sum(1 for e in evidence_events if e.action in ("PREPARE_APPLICATION", "APPLIED"))
    no_feedback = max(0, delivered_count - tg_count)

    reasons: dict[str, int] = {}
    for e in evidence_events:
        if e.feedback_reason:
            reasons[e.feedback_reason] = reasons.get(e.feedback_reason, 0) + 1

    return {
        "delivered_vacancies": delivered_count,
        "telegram_feedback_vacancies": tg_count,
        "telegram_feedback_coverage_rate": tg_coverage_rate,
        "confirmed_applications": app_count,
        "canonical_human_evidence_vacancies": canonical_human_count,
        "human_evidence_coverage_rate": human_coverage_rate,
        # Backward-compatibility aliases
        "feedback_received": canonical_human_count,
        "coverage_rate": human_coverage_rate,
        "explicit_positive": pos_count,
        "explicit_negative": neg_count,
        "skip": skip_count,
        "prepare_intent": prep_count,
        "no_feedback": no_feedback,
        "reasons_breakdown": reasons,
    }


# ---------------------------------------------------------------------------
# Preference Profile Builder with Reason-Aware Routing (Stage 91)
# ---------------------------------------------------------------------------

def build_preference_profile(
    events: list[PreferenceEvidenceEvent] | None = None,
    now_dt: datetime | None = None,
) -> PreferenceProfile:
    """Build derived PreferenceProfile from audited canonical evidence without mutating anything."""
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)

    if events is not None:
        raw_count = len(events)
        eligible_count = len(events)
        excluded_count = 0
        prov_summary = {}
        for e in events:
            p_val = e.provenance.value if hasattr(e.provenance, "value") else str(e.provenance)
            prov_summary[p_val] = prov_summary.get(p_val, 0) + 1
    else:
        raw_events = extract_all_preference_evidence(now_dt=now_dt, include_non_production=True)
        raw_count = len(raw_events)

        prov_summary = {}
        for e in raw_events:
            p_val = e.provenance.value if hasattr(e.provenance, "value") else str(e.provenance)
            prov_summary[p_val] = prov_summary.get(p_val, 0) + 1

        events = extract_all_preference_evidence(now_dt=now_dt, include_non_production=False)
        eligible_count = len(events)
        excluded_count = raw_count - eligible_count

    now_iso = now_dt.isoformat()

    # Groupings with Reason-Aware Signal Routing (Stage 91 Task 16)
    by_rf: dict[str, list[PreferenceEvidenceEvent]] = {}
    by_concept: dict[str, list[PreferenceEvidenceEvent]] = {}
    by_skill: dict[str, list[PreferenceEvidenceEvent]] = {}
    by_company: dict[str, list[PreferenceEvidenceEvent]] = {}
    skip_reasons: dict[str, int] = {}
    feedback_reasons: dict[str, int] = {}

    for ev in events:
        rsn = ev.feedback_reason or ev.skip_reason
        if rsn:
            skip_reasons[rsn] = skip_reasons.get(rsn, 0) + 1
            feedback_reasons[rsn] = feedback_reasons.get(rsn, 0) + 1

        is_negative = (ev.action in ("NOT_INTERESTED", "SKIP"))

        # Reason routing rules:
        # If negative because of COMPANY -> only penalize company, not role_family or skills
        # If negative because of SALARY or LOCATION -> do not penalize role_family or skills
        # If negative because of TECH_STACK -> penalize skills, not role_family
        route_to_role = True
        route_to_skills = True
        route_to_company = True

        if is_negative and rsn:
            if rsn == "COMPANY":
                route_to_role = False
                route_to_skills = False
            elif rsn in ("SALARY", "LOCATION", "LANGUAGE", "EMPLOYMENT_TYPE"):
                route_to_role = False
                route_to_skills = False
            elif rsn == "TECH_STACK":
                route_to_role = False

        # Role family routing
        if route_to_role:
            rf = ev.role_family or "OTHER"
            by_rf.setdefault(rf, []).append(ev)
            if ev.role_concept:
                by_concept.setdefault(ev.role_concept, []).append(ev)

        # Skills routing
        if route_to_skills:
            for sk in ev.skills:
                by_skill.setdefault(sk, []).append(ev)

        # Company routing
        if route_to_company and ev.company and len(ev.company.strip()) >= 2:
            c_norm = ev.company.strip()
            by_company.setdefault(c_norm, []).append(ev)

    # Aggregate signals
    rf_signals = {
        k: aggregate_events_to_signal(k, k, ev_list, now_dt)
        for k, ev_list in sorted(by_rf.items())
    }
    concept_signals = {
        k: aggregate_events_to_signal(k, k, ev_list, now_dt)
        for k, ev_list in sorted(by_concept.items())
    }
    skill_signals = {
        k: aggregate_events_to_signal(k, k, ev_list, now_dt)
        for k, ev_list in sorted(by_skill.items())
    }
    company_signals = {
        k: aggregate_events_to_signal(k, k, ev_list, now_dt)
        for k, ev_list in sorted(by_company.items())
    }

    # Dimension-specific readiness map (Stage 91 Task 14)
    dim_readiness = {}
    for k, v in rf_signals.items():
        dim_readiness[f"role_family:{k}"] = v.readiness
    for k, v in concept_signals.items():
        dim_readiness[f"role_concept:{k}"] = v.readiness
    for k, v in skill_signals.items():
        dim_readiness[f"skill:{k}"] = v.readiness
    for k, v in company_signals.items():
        dim_readiness[f"company:{k}"] = v.readiness

    # Global calibration readiness
    threshold = getattr(config, "PREFERENCE_MIN_EVIDENCE_THRESHOLD", 5)
    if eligible_count == 0:
        readiness = "NO_EVIDENCE"
    elif eligible_count < threshold:
        readiness = "COLLECTING"
    else:
        readiness = "CALIBRATION_ELIGIBLE"

    status = (
        "READY"
        if readiness == "CALIBRATION_ELIGIBLE"
        else "INSUFFICIENT_EVIDENCE_FOR_AUTOMATIC_CALIBRATION"
    )

    events_needed = max(0, threshold - eligible_count)

    bias_notes = [
        "Selection Bias: Only high-scoring digest vacancies (match >= 75) are presented to user for feedback.",
        "Absence of feedback on a vacancy does NOT imply negative preference (no feedback != dislike).",
        "Skill preferences reflect user role desire and do NOT alter factual candidate qualifications or resume skills.",
        f"Provenance Audit: {eligible_count} legitimate human evidence event(s) validated ({excluded_count} automated/test rows excluded).",
    ]
    if readiness != "CALIBRATION_ELIGIBLE":
        bias_notes.append(
            f"Readiness level is '{readiness}' ({eligible_count} / {threshold} independent human events; {events_needed} more required); "
            "automatic production ranking adjustments are deferred."
        )

    return PreferenceProfile(
        generated_at=now_iso,
        raw_evidence_count=raw_count,
        production_eligible_events_count=eligible_count,
        excluded_events_count=excluded_count,
        total_evidence_events=eligible_count,
        unique_vacancies_evaluated=eligible_count,
        events_needed_for_threshold=events_needed,
        calibration_readiness=readiness,
        calibration_status=status,
        role_families=rf_signals,
        role_concepts=concept_signals,
        skills=skill_signals,
        companies=company_signals,
        skip_reasons=skip_reasons,
        feedback_reasons=feedback_reasons,
        provenance_summary=prov_summary,
        dimension_readiness=dim_readiness,
        selection_bias_notes=bias_notes,
        evidence_events=events,
    )


# ---------------------------------------------------------------------------
# Bounded Preference Ranking Adjustment
# ---------------------------------------------------------------------------

def calculate_preference_adjustment(
    vacancy: Vacancy,
    profile: PreferenceProfile,
    base_match_score: int,
    decision_class: str = "MATCH",
    eligibility: str = "ELIGIBLE",
    enabled: bool | None = None,
) -> tuple[float, list[str]]:
    """Compute bounded preference ranking adjustment for a vacancy.
    
    Strict Invariants:
    1. Hard constraints / INELIGIBLE / REJECT vacancies get 0 adjustment.
    2. If calibration is disabled or global evidence < threshold, returns 0.0.
    3. Total adjustment is clamped strictly between [-MAX_ADJUSTMENT, +MAX_ADJUSTMENT].
    4. Explanations detail every signal contribution.
    """
    if enabled is None:
        enabled = getattr(config, "PREFERENCE_CALIBRATION_ENABLED", False)

    reasons: list[str] = []

    # Invariant 1: Hard Gates / Ineligibility / Disqualifications
    if eligibility not in ("ELIGIBLE", "WARNING") or decision_class == "REJECT" or base_match_score < 60:
        return 0.0, ["Preference adjustment inactive for ineligible / REJECT vacancy (hard constraint invariant)"]

    # Invariant 2: Feature flag & global threshold
    if not enabled:
        return 0.0, ["Preference calibration is disabled by configuration (PREFERENCE_CALIBRATION_ENABLED=false)"]

    if profile.production_eligible_events_count < getattr(config, "PREFERENCE_MIN_EVIDENCE_THRESHOLD", 5):
        return 0.0, [
            f"Insufficient global evidence ({profile.production_eligible_events_count} < {config.PREFERENCE_MIN_EVIDENCE_THRESHOLD}) "
            "for automatic ranking calibration."
        ]

    max_adj = float(getattr(config, "PREFERENCE_MAX_ADJUSTMENT", 8.0))
    raw_adjustment = 0.0

    # 1. Role Family Signal (max impact +/- 3.5 points)
    try:
        from ai_assistant.matcher import classify_role_family
        rf = classify_role_family(vacancy.title, vacancy.description or "")
        rf_key = rf.value if hasattr(rf, "value") else str(rf)
    except Exception:
        rf_key = "OTHER"

    rf_sig = profile.role_families.get(rf_key)
    if rf_sig and rf_sig.confidence > 0.0 and rf_sig.total_evidence >= 2:
        rf_delta = rf_sig.raw_signal * rf_sig.confidence * 3.5
        raw_adjustment += rf_delta
        sign = "+" if rf_delta >= 0 else ""
        reasons.append(
            f"{sign}{rf_delta:.1f} preference for role family '{rf_key}' "
            f"(signal={rf_sig.raw_signal:+.2f}, conf={rf_sig.confidence:.2f}, count={rf_sig.total_evidence})"
        )

    # 2. Role Concept Signal (max impact +/- 2.5 points)
    concept = extract_role_concept(vacancy.title)
    concept_sig = profile.role_concepts.get(concept)
    if concept_sig and concept_sig.confidence > 0.0 and concept_sig.total_evidence >= 2:
        concept_delta = concept_sig.raw_signal * concept_sig.confidence * 2.5
        raw_adjustment += concept_delta
        sign = "+" if concept_delta >= 0 else ""
        reasons.append(
            f"{sign}{concept_delta:.1f} preference for role concept '{concept}' "
            f"(signal={concept_sig.raw_signal:+.2f}, conf={concept_sig.confidence:.2f})"
        )

    # 3. Skills / Technologies Signal (max impact +/- 2.0 points)
    vac_skills = extract_vacancy_skills(vacancy.title, vacancy.description or "")
    skill_contribs = []
    for sk in vac_skills:
        sk_sig = profile.skills.get(sk)
        if sk_sig and sk_sig.confidence > 0.0 and sk_sig.total_evidence >= 2:
            sk_delta = sk_sig.raw_signal * sk_sig.confidence * 0.75
            skill_contribs.append(sk_delta)
            sign = "+" if sk_delta >= 0 else ""
            reasons.append(f"{sign}{sk_delta:.1f} skill preference for '{sk}'")

    if skill_contribs:
        sk_sum = sum(skill_contribs)
        sk_sum = max(-2.0, min(2.0, sk_sum))
        raw_adjustment += sk_sum

    # 4. Company-Specific Suppression / Boost (max impact: -4.0 for negative, +1.5 for positive)
    if vacancy.company:
        c_norm = vacancy.company.strip()
        c_sig = profile.companies.get(c_norm)
        if c_sig and c_sig.total_evidence >= 1:
            if c_sig.raw_signal < 0:
                c_delta = max(-4.0, c_sig.raw_signal * 4.0)
                raw_adjustment += c_delta
                reasons.append(f"{c_delta:.1f} company suppression for '{c_norm}' (negative feedback recorded)")
            elif c_sig.raw_signal > 0 and c_sig.total_evidence >= 2:
                c_delta = min(1.5, c_sig.raw_signal * 1.5)
                raw_adjustment += c_delta
                reasons.append(f"+{c_delta:.1f} positive company preference for '{c_norm}'")

    final_adj = max(-max_adj, min(max_adj, raw_adjustment))
    if not reasons:
        reasons.append("No active preference signals matched this vacancy (0.0 adjustment).")

    return round(final_adj, 1), reasons
