"""PREVIEW ONLY — what a widened AUTO allowlist would capture on the REAL dialogs.

Read-only. Nothing is sent, nothing is written, no browser is opened.
The candidate allowlist below is defined IN THIS SCRIPT and is deliberately
NOT applied to the module. This exists to answer one question before any
policy change: "if we let AUTO answer technical questions, what exactly
would go out to employers?"

It runs the real 25 stored dialogs through three lenses:

  1. TODAY'S guarded path
       classify_message() + is_safe_for_auto_reply()  -> allowlist verdict
       generate_reply()                               -> the text that would
                                                         ACTUALLY be sent
  2. The detailed classifier (Stage 30D.5, used by the dashboard/triage)
       classify_hh_conversation_detailed()            -> grounded draft from
                                                         candidate_profile.json
  3. A CANDIDATE widened allowlist (preview)
       -> which dialogs would newly become auto-eligible, and what text the
          current AUTO path would send for them

The point of lens 3: widening the allowlist does NOT change the text. The AUTO
path still calls generate_reply(), which is a generic "interested, let's talk"
stub. So the preview shows, side by side, the stub vs the grounded draft.

Run:  .venv/Scripts/python.exe scripts/hh_auto_reply_candidate_allowlist.py
"""

from __future__ import annotations

import re
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant.hh_message_reply import (  # noqa: E402
    _AUTO_ALLOWED_PROBE,
    _AUTO_FORBIDDEN_RE,
    MessageClassification,
    classify_hh_conversation_detailed,
    classify_message,
    generate_reply,
    is_safe_for_auto_reply,
)

# Reuse the exact same loader as the dry-run so both reports describe the same
# 25 dialogs. Importing the module runs only its top-level definitions.
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from hh_auto_reply_dryrun import load_dialogs  # noqa: E402


# ---------------------------------------------------------------------------
# CANDIDATE widened allowlist — PREVIEW, NOT APPLIED ANYWHERE.
#
# What changes vs the live _AUTO_FORBIDDEN_RE:
#   REMOVED from the forbidden set (so technical questions become eligible):
#       опыт | years of experience | лет опыта | года опыта
#       стек | технологи | навык | python | n8n | llm | java | golang | telegram
#   KEPT forbidden (irreversible / needs a human):
#       money      -> salary expectations are a negotiation, not a fact
#       calendar   -> interview scheduling needs a real calendar
#       interview  -> past-interview questions are candidate history
#       status     -> application status is a fact the recruiter can look up
#       offer/legal-> accepting an offer is a commitment
#       timezone   -> overlaps with scheduling
# ---------------------------------------------------------------------------
_CANDIDATE_FORBIDDEN_RE = re.compile(
    r"зарплат|salary|оклад|оплат|компенсац|ставк|денег|сколько.*получ|"
    r"уровень дохода|"
    r"когда.*(мож|смож|доступ)|в какое время|какую дату|start date|available to start|"
    r"when can you|when could you|"
    r"интервью|собеседован|interview|проходили|прошлых|предыдущ.*(интерв|собес)|did you.*interview|"
    r"статус|статусе|status|решение по ваканси|"
    r"принять предлож|отклон|accept.*offer|offer|контракт|договор|юридич|финанс|"
    r"условия работы|work conditions|часовой пояс",
    re.IGNORECASE,
)

# Widened probe: a plain interest line OR any question that is actually asking
# about experience/stack. Mirrors _TECH_ASK_CUE_RE from the detailed classifier.
_CANDIDATE_ALLOWED_PROBE = re.compile(
    r"(интересн|заинтересован|interested|want to discuss|готовы обсудить|"
    r"можем ли|would you like|still interested|ещё интересует|по-прежнему|"
    r"[?？]|"
    r"опыт|работал|знае|приходилос|использовал|стаж|владе|навык|уме|"
    r"\bexperience\b|\bworked\b|\bused\b|\bfamiliar\b|\bskills?\b|know how)",
    re.IGNORECASE,
)


def _candidate_safe(dialog, classification) -> tuple[bool, list[str]]:
    """Same shape as is_safe_for_auto_reply(), but with the candidate regexes."""
    reasons: list[str] = []
    if classification != MessageClassification.REPLY_REQUIRED:
        reasons.append("classification is not REPLY_REQUIRED")
    context = "\n".join((m.text or "") for m in (dialog.messages or []))
    if _CANDIDATE_FORBIDDEN_RE.search(context):
        reasons.append("forbidden topic (money/schedule/interview/status/offer)")
    last = (dialog.last_message().text or "") if dialog.last_message() else ""
    if not _CANDIDATE_ALLOWED_PROBE.search(last):
        reasons.append("not an interest probe and not a tech/experience question")
    return (not reasons), reasons


def main() -> int:
    dialogs = load_dialogs()
    print(f"Диалогов: {len(dialogs)}")
    print("Режим: PREVIEW ONLY — ничего не отправлено, модуль не тронут.")
    print("=" * 100)

    today_allow = 0
    cand_allow = 0
    cand_stub_nonanswers = 0
    rows: list[dict] = []

    for d in dialogs:
        last = d.last_message()
        last_text = (last.text or "").strip() if last else ""
        cls = classify_message(d)
        today = is_safe_for_auto_reply(d, cls)["safe"]
        cand, cand_reasons = _candidate_safe(d, cls)

        stub = generate_reply(d).get("reply", "") if cls == MessageClassification.REPLY_REQUIRED else ""
        detailed = classify_hh_conversation_detailed(d)
        grounded = detailed.get("prepared_reply") or ""

        if today:
            today_allow += 1
        if cand:
            cand_allow += 1
            # Would the AUTO path, as written today, send a real answer?
            # generate_reply() only knows the generic interest stub, so unless
            # the message is exactly that, the text is a non-answer.
            if "рассматриваю роли" in stub:
                cand_stub_nonanswers += 1

        rows.append({
            "employer": d.employer, "cid": d.conversation_id,
            "cls": cls.value, "today": today, "cand": cand,
            "cand_reasons": cand_reasons, "last": last_text,
            "stub": stub, "grounded": grounded,
            "detailed_cls": detailed.get("classification"),
            "detailed_reason": detailed.get("reason"),
        })

    # Only print the dialogs where the candidate allowlist changes the verdict.
    print("\n### Диалоги, которые РАСШИРЕННЫЙ allowlist переведёт в автоответ\n")
    changed = [r for r in rows if r["cand"] and not r["today"]]
    if not changed:
        print("(нет — расширенный allowlist не добавляет ни одного диалога)")
    for r in changed:
        print(f"[НОВЫЙ АВТООТВЕТ] {r['employer']}  (conv {r['cid']})")
        print(f"    classification   : {r['cls']}   (detailed: {r['detailed_cls']})")
        print(f"    последнее        : {r['last'][:110]}")
        print(f"    ЧТО УЙДЁТ СЕЙЧАС  : {r['stub'][:150] or '(пусто — генератор ничего не построил)'}")
        print(f"    ЧТО ГОТОВИТ ДЕТАЙЛ: {r['grounded'][:150] or '(нет черновика)'}")
        print()

    print("=" * 100)
    print(f"ИТОГ")
    print(f"  сегодняшний allowlist  : автоответ прошёл бы в {today_allow} из {len(rows)}")
    print(f"  расширенный allowlist  : автоответ прошёл бы в {cand_allow} из {len(rows)}")
    print(f"  из них сейчас уйдёт НЕ-ОТВЕТ (стаб generate_reply): {cand_stub_nonanswers}")
    print()
    print("ВЫВОД: расширение allowlist само по себе не даёт ответов на технические")
    print("вопросы — AUTO-путь всё равно зовёт generate_reply() (заглушка).")
    print("Нужны ДВА изменения: (1) allowlist, (2) генератор -> черновик")
    print("classify_hh_conversation_detailed(). Иначе работодатель получит")
    print("«Спасибо за сообщение... рассматриваю роли: ...» вместо ответа.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
