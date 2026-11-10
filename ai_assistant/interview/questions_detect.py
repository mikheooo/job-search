"""Interviewer question detection (Phase 5B-2).

Pure rule-based layer over ready transcripts: no model calls, no network,
no database. Splits transcript segments into sentences, scores
question-likeness for Russian and English, buffers incomplete phrases, and
never emits the same question twice.

The detector only PROPOSES candidates. The user confirms or edits every
candidate in the UI before anything reaches the Coach (fast/hint), so the
AI never runs on every speech fragment.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

#: Minimum normalized length: shorter is a fragment, never a question.
MIN_QUESTION_CHARS = 4
#: Emit a complete sentence at or above this confidence.
EMIT_THRESHOLD = 0.6
#: Stale buffered fragments emit as incomplete at or above this.
FLUSH_THRESHOLD = 0.5
#: Bound on remembered fingerprints (memory cap, not a behaviour change).
MAX_REMEMBERED = 200

_RU_INTERROGATIVE = (
    "как", "что", "почему", "зачем", "когда", "где", "куда", "откуда",
    "сколько", "какой", "какая", "какое", "какие", "какого", "какую",
    "чей", "чья", "чьё", "чьи", "а как", "а что", "можете ли", "могли бы",
    "приходилось", "сталкивались", "случалось",
)
_RU_IMPERATIVE = (
    "расскажите", "опишите", "поделитесь", "перечислите", "назовите",
    "приведите пример",
)
_EN_INTERROGATIVE = (
    "what", "how", "why", "when", "where", "which", "who", "whose", "whom",
    "do ", "does ", "did ", "is ", "are ", "was ", "were", "have ", "has ",
    "can ", "could ", "would ", "should ", "tell me about", "describe ",
    "explain ", "walk me through",
)

_SPEAKER_RE = re.compile(
    r"^\s*(?:\[[0-9:.]+\]\s*|\(\s*[0-9:.]+\s*\)\s*|[^:]{1,40}:\s+)?")
_WS_RE = re.compile(r"\s+")
_PUNCT_TAIL_RE = re.compile(r"[.?!…\s]+$")
_SENT_SPLIT_RE = re.compile(r"([^.?!…]+[.?!…]+|.+$)")


def normalize(text: str) -> str:
    """Lowercase, strip speaker/timestamp labels, collapse space/punct."""
    cleaned = _SPEAKER_RE.sub("", text or "")
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[«»\"'—–-]", " ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = _PUNCT_TAIL_RE.sub("", cleaned)
    return cleaned


def split_sentences(text: str) -> list[tuple[str, bool]]:
    """Split into (sentence, is_complete) pairs.

    Complete = ends with terminal punctuation (.?!…). The tail without
    terminal punctuation is incomplete (a phrase still in progress).
    """
    out: list[tuple[str, bool]] = []
    for match in _SENT_SPLIT_RE.finditer(text or ""):
        piece = match.group(1).strip()
        if not piece:
            continue
        complete = bool(re.search(r"[.?!…]\s*$", piece))
        out.append((piece, complete))
    return out


def _starts_with(sentence_norm: str, starters: tuple[str, ...]) -> str | None:
    for starter in starters:
        if sentence_norm == starter or sentence_norm.startswith(starter):
            return starter
    return None


def score_question(sentence: str, lang: str) -> tuple[bool, float, str]:
    """Score one complete sentence. Returns (is_question, confidence, reason)."""
    norm = normalize(sentence)
    if len(norm) < MIN_QUESTION_CHARS:
        return False, 0.0, "too-short"
    ends_q = bool(re.search(r"\?\s*$", sentence.strip()))
    score = 0.0
    reasons: list[str] = []
    if ends_q:
        score += 0.55
        reasons.append("question-mark")
        if len(norm) < 20:
            score += 0.10
            reasons.append("short-direct")
    if lang == "ru":
        hit = _starts_with(norm, _RU_INTERROGATIVE)
        if hit:
            score += 0.35
            reasons.append(f"interrogative:{hit}")
        hit = _starts_with(norm, _RU_IMPERATIVE)
        if hit:
            score += 0.65
            reasons.append(f"imperative:{hit}")
        if re.search(r"\bли\b", norm):
            score += 0.15
            reasons.append("particle-li")
    else:
        hit = _starts_with(norm, _EN_INTERROGATIVE)
        if hit:
            if hit in ("tell me about", "describe ", "explain ", "walk me through"):
                score += 0.65
                reasons.append(f"imperative:{hit.strip()}")
            else:
                score += 0.35
                reasons.append(f"interrogative:{hit.strip()}")
    score = round(min(score, 0.95), 2)
    return score >= EMIT_THRESHOLD, score, "+".join(reasons) or "statement"


class QuestionDetector:
    """Stateful per-interview detector: buffering + never-repeat.

    ``clock`` is injectable (tests); ``stale_after_s`` bounds how long a
    fragment may wait for its continuation before ``flush_stale`` settles it.
    """

    def __init__(self, stale_after_s: float = 20.0,
                 clock: Callable[[], float] | None = None) -> None:
        self._stale_after = stale_after_s
        self._clock = clock or time.monotonic
        self._buffer = ""
        self._buffer_since = 0.0
        self._emitted: list[str] = []

    def _remember(self, norm: str) -> None:
        if norm not in self._emitted:
            self._emitted.append(norm)
            del self._emitted[:-MAX_REMEMBERED]

    def _fresh(self, norm: str) -> bool:
        return bool(norm) and norm not in self._emitted

    def _emit(self, text: str, lang: str, confidence: float, reason: str,
              incomplete: bool) -> dict[str, Any] | None:
        norm = normalize(text)
        if not self._fresh(norm):
            return None
        self._remember(norm)
        return {"text": text.strip(), "lang": lang, "confidence": confidence,
                "reason": reason, "incomplete": incomplete, "norm": norm}

    def add_segment(self, text: str, lang: str = "ru") -> list[dict[str, Any]]:
        """Feed one transcript piece. Returns newly detected candidates."""
        found: list[dict[str, Any]] = []
        chunk = (text or "").strip()
        if not chunk:
            return found
        if self._buffer:
            chunk = f"{self._buffer} {chunk}"
            self._buffer = ""
        for sentence, complete in split_sentences(chunk):
            if not complete:
                self._buffer = f"{self._buffer} {sentence}".strip()
                self._buffer_since = self._clock()
                continue
            is_q, conf, reason = score_question(sentence, lang)
            if is_q:
                cand = self._emit(sentence, lang, conf, reason, False)
                if cand:
                    found.append(cand)
        return found

    def flush_stale(self, lang: str = "ru", force: bool = False) -> list[dict[str, Any]]:
        """Settle a buffered fragment: emit as incomplete or drop it.

        Without ``force``, only fragments older than ``stale_after_s`` settle —
        a phrase the speaker may still be continuing must not be finalized
        (and must never trigger the AI). Batch callers (the /detect endpoint,
        which receives a complete utterance window) pass ``force=True``.
        """
        frag = self._buffer.strip()
        if not frag:
            return []
        if not force and (self._clock() - self._buffer_since) < self._stale_after:
            return []
        self._buffer = ""
        _is_q, conf, reason = score_question(frag + ".", lang)
        if conf >= FLUSH_THRESHOLD:
            cand = self._emit(frag, lang, conf, reason + "+incomplete", True)
            return [cand] if cand else []
        return []

    def reset(self) -> None:
        self._buffer = ""
        self._emitted.clear()


__all__ = ["QuestionDetector", "normalize", "score_question",
           "split_sentences", "EMIT_THRESHOLD", "FLUSH_THRESHOLD"]
