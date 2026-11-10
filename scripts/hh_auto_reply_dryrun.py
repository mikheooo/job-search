"""Dry-run: which of the REAL incoming HH messages would the AUTO path send by itself?

Read-only. Nothing is sent, nothing is written to state.db, no browser is opened.
It takes the actual employer messages already stored in `hh_message_events`,
rebuilds dialogs, and runs the exact gates the guarded AUTO path uses:

    classify_message()          -> must be REPLY_REQUIRED
    is_safe_for_auto_reply()    -> no forbidden topic anywhere in the dialog
                                   AND the last line is a plain interest probe
                                   AND the profile has desired_roles
    can_auto_send()             -> kill switch + allowlist + non-empty reply
                                   + composer present + fingerprint set

The kill switch is reported as OFF (that is the current state of `.env`), so the
interesting column is the allowlist: it shows what would be auto-sent the moment
`HH_AUTO_REPLY_ENABLED=true` is set.

Run:  .venv/Scripts/python.exe scripts/hh_auto_reply_dryrun.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant.hh_message_reply import (  # noqa: E402
    HHDialog,
    HHMessage,
    MessageClassification,
    _AUTO_ALLOWED_PROBE,
    _AUTO_FORBIDDEN_RE,
    _auto_enabled,
    can_auto_send,
    classify_message,
    is_safe_for_auto_reply,
)

DB = PROJECT_ROOT / "state.db"


def load_dialogs(limit_conversations: int = 25) -> list[HHDialog]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT conversation_id, employer, vacancy_stable_id, sender, text, seen_at
        FROM hh_message_events
        WHERE conversation_id IS NOT NULL AND conversation_id <> ''
        ORDER BY seen_at ASC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    by_conv: dict[str, list[dict]] = {}
    for r in rows:
        by_conv.setdefault(str(r["conversation_id"]), []).append(r)

    dialogs: list[HHDialog] = []
    for cid, items in by_conv.items():
        # newest conversations first
        newest = max((i["seen_at"] or "") for i in items)
        msgs = [
            HHMessage(
                message_id=f"{cid}-{n}",
                text=str(i["text"] or ""),
                sender=str(i["sender"] or "employer"),
                sent_at=i["seen_at"],
            )
            for n, i in enumerate(items)
        ]
        dialogs.append(
            HHDialog(
                conversation_id=cid,
                vacancy_title="",
                vacancy_stable_id=str(items[-1]["vacancy_stable_id"] or ""),
                employer=str(items[-1]["employer"] or "Работодатель"),
                messages=msgs,
            )
        )
        dialogs[-1]._newest = newest  # type: ignore[attr-defined]

    dialogs.sort(key=lambda d: getattr(d, "_newest", ""), reverse=True)
    return dialogs[:limit_conversations]


def main() -> int:
    dialogs = load_dialogs()
    print(f"Диалогов с входящими сообщениями: {len(dialogs)}")
    print(f"Kill switch HH_AUTO_REPLY_ENABLED: {'ON' if _auto_enabled() else 'OFF (сейчас так)'}")
    print("=" * 100)

    would_send = 0
    for d in dialogs:
        last = d.last_message()
        last_text = (last.text or "").strip() if last else ""
        cls = classify_message(d)
        safe = is_safe_for_auto_reply(d, cls)
        gate = can_auto_send(
            d,
            cls,
            profile=None,
            reply="(dry-run placeholder reply)",
            composer_present=True,
            fingerprint="dry-run-fp",
        )
        allow = safe["safe"]
        if allow:
            would_send += 1

        flag = "АВТООТВЕТ" if allow else "в руки"
        print(f"\n[{flag}] {d.employer}  (conv {d.conversation_id})")
        print(f"    classification : {cls.value}")
        print(f"    allowlist      : {'PASS' if allow else 'FAIL'}")
        if not allow:
            print(f"    почему         : {'; '.join(safe['reasons'])}")
        print(f"    последнее      : {last_text[:110]}")
        if _AUTO_FORBIDDEN_RE.search(last_text):
            hit = _AUTO_FORBIDDEN_RE.search(last_text)
            print(f"    запрещённое    : ...{last_text[max(0, hit.start()-20):hit.end()+20]!r}...")
        if _AUTO_ALLOWED_PROBE.search(last_text):
            print("    probe          : да, это простой интерес/доступность")
        print(f"    can_auto_send  : {gate['ok']}  {[c for c in gate['checks'] if not c['ok']]}")

    print("\n" + "=" * 100)
    print(f"ИТОГ: автоответ ушёл бы в {would_send} из {len(dialogs)} диалогов.")
    print("(остальные — HUMAN_REVIEW; при kill switch OFF не уйдёт ничего)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
