from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from . import config
from .db import get_connection, init_db, get_deep_analysis, get_application_package, get_submission, get_all_submissions, get_verification, list_verifications
from .application_tracking import get_application_status, list_applications, get_application_history, ApplicationStatus
from .application_review import get_application_review, list_application_reviews, ReviewStatus
from .application_queue import get_queue_item, list_queue
from .browser_executor import get_browser_session, BrowserStatus
from .vacancy_identity import get_canonical_by_id, get_aliases_for_canonical, get_all_canonical_vacancies, MatchType

logger = logging.getLogger(__name__)


class IntegritySeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class IntegrityIssue:
    severity: IntegritySeverity
    code: str
    vacancy_stable_id: str
    canonical_id: Optional[str] = None
    message: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __lt__(self, other: "IntegrityIssue") -> bool:
        o = {IntegritySeverity.ERROR: 0, IntegritySeverity.WARNING: 1, IntegritySeverity.INFO: 2}
        s1 = o.get(self.severity, 3)
        s2 = o.get(other.severity, 3)
        if s1 != s2:
            return s1 < s2
        c1 = self.canonical_id or ""
        c2 = other.canonical_id or ""
        if c1 != c2:
            return c1 < c2
        v1 = self.vacancy_stable_id or ""
        v2 = other.vacancy_stable_id or ""
        if v1 != v2:
            return v1 < v2
        return self.code < other.code


@dataclass
class IntegrityReport:
    generated_at: str
    total_checked: int
    canonical_checked: int
    scope: str = "full"
    info_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    issues: List[IntegrityIssue] = field(default_factory=list)
    audited_vacancies: int = 0
    audited_canonicals: int = 0
    queue_items: int = 0
    reviews: int = 0
    browser_preparations: int = 0
    submissions: int = 0
    verifications: int = 0
    aliases: int = 0

    @property
    def healthy(self) -> bool:
        return self.error_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_checked": self.total_checked,
            "canonical_checked": self.canonical_checked,
            "scope": self.scope,
            "info_count": self.info_count,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "issues": [{"severity": i.severity.value, "code": i.code, "vacancy_stable_id": i.vacancy_stable_id, "canonical_id": i.canonical_id, "message": i.message, "evidence": i.evidence} for i in self.issues],
            "audited_vacancies": self.audited_vacancies,
            "audited_canonicals": self.audited_canonicals,
            "queue_items": self.queue_items,
            "reviews": self.reviews,
            "browser_preparations": self.browser_preparations,
            "submissions": self.submissions,
            "verifications": self.verifications,
            "aliases": self.aliases,
        }


class IntegrityAuditor:
    def __init__(self, scope: str = "full"):
        self.scope = scope if scope in ("full", "tracked") else "full"
        self.issues: List[IntegrityIssue] = []
        self._audited_vacancies: Set[str] = set()
        self._audited_canonicals: Set[str] = set()

    def _add(self, sev: IntegritySeverity, code: str, vid: str, cid: Optional[str], msg: str, ev: Dict[str, Any] = None):
        self.issues.append(IntegrityIssue(severity=sev, code=code, vacancy_stable_id=vid, canonical_id=cid, message=msg, evidence=ev or {}))

    def _err(self, code: str, vid: str, cid: Optional[str], msg: str, ev: Dict[str, Any] = None):
        self._add(IntegritySeverity.ERROR, code, vid, cid, msg, ev)

    def _warn(self, code: str, vid: str, cid: Optional[str], msg: str, ev: Dict[str, Any] = None):
        self._add(IntegritySeverity.WARNING, code, vid, cid, msg, ev)

    def _info(self, code: str, vid: str, cid: Optional[str], msg: str, ev: Dict[str, Any] = None):
        self._add(IntegritySeverity.INFO, code, vid, cid, msg, ev)

    def _check_canonical_identity_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        if cid.startswith("unknown_"):
            return
        aliases_db = get_aliases_for_canonical(cid)
        if len(aliases_db) <= 1:
            return
        exact = [a for a in aliases_db if a.get("match_type") == MatchType.EXACT.value]
        if len(exact) > 1:
            cids = set(a.get("canonical_id") for a in exact if a.get("canonical_id"))
            if len(cids) > 1:
                self._err("CANONICAL_EXACT_MISMATCH", exact[0]["vacancy_stable_id"], cid, f"EXACT aliases have different canonical_ids: {cids}", {"canonical_ids": list(cids)})

    def _check_canonical_has_aliases(self, cid: str, aliases: List[Tuple[Any, str]]):
        if cid.startswith("unknown_"):
            return
        if not aliases:
            self._err("CANONICAL_NO_ALIASES", "", cid, f"Canonical {cid} has no aliases", {"canonical_id": cid})

    def _check_alias_belongs_to_canonical(self, cid: str, aliases: List[Tuple[Any, str]]):
        if cid.startswith("unknown_"):
            return
        valid = {a["vacancy_stable_id"] for a in get_aliases_for_canonical(cid)}
        for _, sid in aliases:
            if sid not in valid:
                self._err("ALIAS_CANONICAL_MISMATCH", sid, cid, f"Alias {sid} does not belong to canonical {cid}", {"alias": sid})

    def _check_queue_canonical_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        items = list_queue(queue_version="v2")
        by_cid: Dict[str, List[Any]] = {}
        for it in items:
            by_cid.setdefault(it.canonical_id, []).append(it)
        if cid in by_cid and len(by_cid[cid]) > 1:
            self._err("QUEUE_CANONICAL_DUPLICATE", by_cid[cid][0].vacancy_stable_id, cid, f"Canonical {cid} has {len(by_cid[cid])} queue items", {"queue_items": [q.vacancy_stable_id for q in by_cid[cid]]})

    def _check_tracking_queue_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            tr = get_application_status(sid)
            if not tr:
                continue
            st = tr.status.value if hasattr(tr.status, 'value') else str(tr.status)
            term = {ApplicationStatus.APPLIED.value, ApplicationStatus.INTERVIEW.value, ApplicationStatus.OFFER.value, ApplicationStatus.REJECTED.value, ApplicationStatus.WITHDRAWN.value}
            if st in term:
                qi = get_queue_item(sid, "v2")
                if qi:
                    self._err("TERMINAL_IN_READY_QUEUE", sid, cid, f"Terminal {st} is in queue", {"tracking": st})

    def _check_review_tracking_browser_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            rev = get_application_review(sid)
            if not rev:
                continue
            rs = rev.status.value if hasattr(rev.status, 'value') else str(rev.status)
            tr = get_application_status(sid)
            ts = tr.status.value if tr and hasattr(tr.status, 'value') else (str(tr.status) if tr else None)
            bsess = get_browser_session(sid)
            bs = bsess.status.value if bsess and hasattr(bsess.status, 'value') else (str(bsess.status) if bsess else None)
            if rs == ReviewStatus.APPROVED.value and ts not in {ApplicationStatus.READY_TO_APPLY.value, ApplicationStatus.SUBMITTED.value}:
                self._warn("REVIEW_APPROVED_INVALID_TRACKING", sid, cid, f"Review APPROVED but tracking {ts}", {"review": rs, "tracking": ts})
            if bs == BrowserStatus.BLOCKED.value and rs == ReviewStatus.APPROVED.value:
                self._err("REVIEW_BROWSER_MISMATCH", sid, cid, "Review APPROVED but browser BLOCKED", {"review": rs, "browser": bs})

    def _check_browser_vacancy_package_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            bsess = get_browser_session(sid)
            if not bsess:
                continue
            bs = bsess.status.value if hasattr(bsess.status, 'value') else str(bsess.status)
            if bs == BrowserStatus.READY_FOR_REVIEW.value and not get_application_package(sid):
                self._err("BROWSER_READY_NO_PACKAGE", sid, cid, "Browser READY_FOR_REVIEW but no package", {"browser": bs})

    def _check_submission_tracking_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            tr = get_application_status(sid)
            if not tr:
                continue
            ts = tr.status.value if hasattr(tr.status, 'value') else str(tr.status)
            if tr.status == ApplicationStatus.SUBMITTED and not get_all_submissions(sid):
                self._err("SUBMITTED_NO_SUBMISSION_RECORD", sid, cid, "SUBMITTED but no submission record", {"tracking": ts})
            subs = get_all_submissions(sid)
            if subs:
                latest = max(subs, key=lambda s: s[5] or "")
                sub_st = latest[4] if len(latest) > 4 else None
                if sub_st == "SUBMITTED" and ts not in {ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value, ApplicationStatus.APPLIED.value}:
                    self._warn("SUBMISSION_TRACKING_MISMATCH", sid, cid, f"Submission SUBMITTED but tracking {ts}", {"sub": sub_st, "track": ts})

    def _check_verification_submission_consistency(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            tr = get_application_status(sid)
            if not tr:
                continue
            ts = tr.status.value if hasattr(tr.status, 'value') else str(tr.status)
            for sub in get_all_submissions(sid):
                sub_id = sub[1]
                ver = get_verification(sid, sub_id)
                if not ver:
                    continue
                vs = ver.verification_status.value if hasattr(ver.verification_status, 'value') else str(ver.verification_status)
                if tr.status == ApplicationStatus.VERIFIED and vs != "VERIFIED":
                    self._err("VERIFIED_NO_VERIFICATION", sid, cid, "VERIFIED but no VERIFIED verification", {"tracking": ts, "verification": vs})
                if vs in {"FAILED", "AMBIGUOUS", "BLOCKED"} and ts == ApplicationStatus.APPLIED.value:
                    self._err("VERIFICATION_FAILED_BUT_APPLIED", sid, cid, f"Verification {vs} but APPLIED", {"verification": vs, "tracking": ts})

    def _check_lifecycle_transitions(self, cid: str, aliases: List[Tuple[Any, str]]):
        allowed = {
            ApplicationStatus.DISCOVERED: {ApplicationStatus.ANALYZED},
            ApplicationStatus.ANALYZED: {ApplicationStatus.READY_TO_APPLY},
            ApplicationStatus.READY_TO_APPLY: {ApplicationStatus.SUBMITTED},
            ApplicationStatus.SUBMITTED: {ApplicationStatus.VERIFIED, ApplicationStatus.READY_TO_APPLY},
            ApplicationStatus.VERIFIED: {ApplicationStatus.APPLIED, ApplicationStatus.SUBMITTED},
            ApplicationStatus.APPLIED: {ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW},
            ApplicationStatus.INTERVIEW: {ApplicationStatus.OFFER, ApplicationStatus.REJECTED},
        }
        for _, sid in aliases:
            tr = get_application_status(sid)
            if not tr:
                continue
            ts = tr.status.value if hasattr(tr.status, 'value') else str(tr.status)
            hist = get_application_history(sid)
            if not hist:
                continue
            for h in hist:
                old = h.old_status
                new = h.new_status
                if old and new and old != new:
                    try:
                        oe = ApplicationStatus(old) if isinstance(old, str) else old
                        ne = ApplicationStatus(new) if isinstance(new, str) else new
                    except Exception:
                        continue
                    if oe in allowed and ne not in allowed[oe]:
                        self._err("INVALID_LIFECYCLE_TRANSITION", sid, cid, f"Invalid {old} -> {new}", {"from": old, "to": new, "current": ts})

    def _check_submission_records(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            subs = get_all_submissions(sid)
            seen = set()
            for sub in subs:
                sub_id = sub[1]
                if sub_id in seen:
                    self._err("DUPLICATE_SUBMISSION_ID", sid, cid, f"Duplicate {sub_id}", {"submission_id": sub_id})
                seen.add(sub_id)
            for sub in subs:
                sa = sub[5]
                ca = sub[6]
                if sa and ca:
                    try:
                        sdt = datetime.fromisoformat(sa.replace('Z', '+00:00'))
                        cdt = datetime.fromisoformat(ca.replace('Z', '+00:00'))
                        if sdt < cdt:
                            self._err("SUBMISSION_BEFORE_CREATED", sid, cid, f"submitted_at {sa} before created_at {ca}", {"submitted_at": sa, "created_at": ca})
                    except Exception:
                        pass

    def _check_orphan_artifacts(self, cid: str, aliases: List[Tuple[Any, str]]):
        for _, sid in aliases:
            tr = get_application_status(sid)
            if get_queue_item(sid, "v2") and not tr:
                self._err("QUEUE_ITEM_ORPHAN", sid, cid, "Queue item without tracking", {})
            rev = get_application_review(sid)
            if rev and not tr:
                self._err("REVIEW_ORPHAN", sid, cid, "Review without tracking", {})
            bsess = get_browser_session(sid)
            if bsess and not tr:
                self._err("BROWSER_PREP_ORPHAN", sid, cid, "Browser prep without tracking", {})
            subs = get_all_submissions(sid)
            if subs and not tr:
                self._err("SUBMISSION_ORPHAN", sid, cid, "Submission without tracking", {"count": len(subs)})
            for sub in subs:
                sub_id = sub[1]
                ver = get_verification(sid, sub_id)
                if ver and not get_submission(sid, sub_id):
                    self._err("VERIFICATION_ORPHAN", sid, cid, f"Verification {sub_id} without submission", {"submission_id": sub_id})
            if not cid.startswith("unknown_") and not get_canonical_by_id(cid) and aliases:
                self._err("CANONICAL_ORPHAN", cid, cid, "Canonical has aliases but no record", {"alias_count": len(aliases)})

    def _check_global_orphans(self):
        for qi in list_queue(limit=10000, queue_version="v2"):
            if not get_application_status(qi.vacancy_stable_id):
                if not any(i.code=="QUEUE_ITEM_ORPHAN" and i.vacancy_stable_id==qi.vacancy_stable_id for i in self.issues):
                    conn=get_connection()
                    cur=conn.cursor()
                    cur.execute("SELECT canonical_id FROM vacancy_aliases WHERE vacancy_stable_id=?", (qi.vacancy_stable_id,))
                    row=cur.fetchone()
                    conn.close()
                    cid=row[0] if row else None
                    self._err("QUEUE_ITEM_ORPHAN", qi.vacancy_stable_id, cid, "Queue item without tracking (global)", {})
        conn=get_connection()
        cur=conn.cursor()
        cur.execute("SELECT vacancy_stable_id FROM application_reviews")
        for (sid,) in cur.fetchall():
            if not get_application_status(sid):
                if not any(i.code=="REVIEW_ORPHAN" and i.vacancy_stable_id==sid for i in self.issues):
                    conn2=get_connection()
                    cur2=conn2.cursor()
                    cur2.execute("SELECT canonical_id FROM vacancy_aliases WHERE vacancy_stable_id=?", (sid,))
                    r2=cur2.fetchone()
                    conn2.close()
                    cid=r2[0] if r2 else None
                    self._err("REVIEW_ORPHAN", sid, cid, "Review without tracking (global)", {})
        cur.execute("SELECT vacancy_stable_id FROM browser_preparations")
        for (sid,) in cur.fetchall():
            if not get_application_status(sid):
                if not any(i.code=="BROWSER_PREP_ORPHAN" and i.vacancy_stable_id==sid for i in self.issues):
                    conn2=get_connection()
                    cur2=conn2.cursor()
                    cur2.execute("SELECT canonical_id FROM vacancy_aliases WHERE vacancy_stable_id=?", (sid,))
                    r2=cur2.fetchone()
                    conn2.close()
                    cid=r2[0] if r2 else None
                    self._err("BROWSER_PREP_ORPHAN", sid, cid, "Browser prep without tracking (global)", {})
        conn.close()

    def _count_artifacts(self, tracked_sids: Set[str], tracked_cids: Set[str]) -> Dict[str, int]:
        conn = get_connection()
        cur = conn.cursor()

        def count_in(table: str, col: str, ids: Set[str]) -> int:
            if not ids:
                return 0
            ph = ",".join("?" for _ in ids)
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({ph})", sorted(ids))
            return int(cur.fetchone()[0])

        if self.scope == "tracked":
            result = {
                "queue_items": count_in("application_queue", "vacancy_stable_id", tracked_sids),
                "reviews": count_in("application_reviews", "vacancy_stable_id", tracked_sids),
                "browser_preparations": count_in("browser_preparations", "vacancy_stable_id", tracked_sids),
                "submissions": count_in("application_submissions", "vacancy_stable_id", tracked_sids),
                "verifications": count_in("submission_verifications", "vacancy_stable_id", tracked_sids),
                "aliases": count_in("vacancy_aliases", "canonical_id", tracked_cids),
            }
        else:
            def count_all(table: str) -> int:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                return int(cur.fetchone()[0])
            result = {
                "queue_items": count_all("application_queue"),
                "reviews": count_all("application_reviews"),
                "browser_preparations": count_all("browser_preparations"),
                "submissions": count_all("application_submissions"),
                "verifications": count_all("submission_verifications"),
                "aliases": count_all("vacancy_aliases"),
            }
        conn.close()
        return result

    def run_audit(self) -> IntegrityReport:
        init_db()
        all_canonicals = get_all_canonical_vacancies()
        all_tracking = list_applications(limit=10000)
        self._audited_vacancies = {t.vacancy_stable_id for t in all_tracking}
        groups: Dict[str, List[Tuple[Any, str]]] = {}
        for tr in all_tracking:
            sid = tr.vacancy_stable_id
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT canonical_id FROM vacancy_aliases WHERE vacancy_stable_id=?", (sid,))
            row = cur.fetchone()
            conn.close()
            if row:
                cid = row[0]
            else:
                cid = f"unknown_{sid}"
            groups.setdefault(cid, []).append((tr, sid))
        tracked_sids = {sid for als in groups.values() for _, sid in als}
        tracked_cids = {cid for cid in groups if not cid.startswith("unknown_")}
        if self.scope == "tracked":
            self._audited_canonicals = tracked_cids
        else:
            self._audited_canonicals = {c.canonical_id for c in all_canonicals}
        for cid, als in groups.items():
            self._check_canonical_identity_consistency(cid, als)
            self._check_canonical_has_aliases(cid, als)
            self._check_alias_belongs_to_canonical(cid, als)
            self._check_queue_canonical_consistency(cid, als)
            self._check_tracking_queue_consistency(cid, als)
            self._check_review_tracking_browser_consistency(cid, als)
            self._check_browser_vacancy_package_consistency(cid, als)
            self._check_submission_tracking_consistency(cid, als)
            self._check_verification_submission_consistency(cid, als)
            self._check_lifecycle_transitions(cid, als)
            self._check_submission_records(cid, als)
            self._check_orphan_artifacts(cid, als)
        if self.scope == "full":
            self._check_global_orphans()
        self.issues.sort()
        err = sum(1 for i in self.issues if i.severity == IntegritySeverity.ERROR)
        warn = sum(1 for i in self.issues if i.severity == IntegritySeverity.WARNING)
        info = sum(1 for i in self.issues if i.severity == IntegritySeverity.INFO)
        counts = self._count_artifacts(tracked_sids, tracked_cids)
        return IntegrityReport(generated_at=datetime.utcnow().isoformat(), total_checked=len(self._audited_vacancies), canonical_checked=len(self._audited_canonicals), scope=self.scope, info_count=info, warning_count=warn, error_count=err, issues=self.issues, audited_vacancies=len(self._audited_vacancies), audited_canonicals=len(self._audited_canonicals), queue_items=counts["queue_items"], reviews=counts["reviews"], browser_preparations=counts["browser_preparations"], submissions=counts["submissions"], verifications=counts["verifications"], aliases=counts["aliases"])


def run_integrity_audit(scope: str = "full") -> IntegrityReport:
    return IntegrityAuditor(scope=scope).run_audit()
