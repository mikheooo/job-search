"""Диагностика: что РЕАЛЬНО лежит в карточках переговоров на HH.

Только чтение: поднимает headless-сессию, сканирует страницу переговоров,
печатает по каждой карточке сырые `lines` и `text`, а также то, какой
из REJECTION_NEEDLES сработал. Ничего не пишет в БД, ничего не отправляет.

Зачем: разобраться, почему карточки со статусом "Не просмотрен" глушатся
фильтром отказов в `hh_chat_monitor.process_negotiation_alerts`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_assistant.hh_chat_monitor import scan_hh_negotiations  # noqa: E402

REJECTION_NEEDLES = ("отказ", "отклонен", "отклонён", "отказано")


def _needle_hits(text: str) -> list[str]:
    low = text.lower()
    return [n for n in REJECTION_NEEDLES if n in low]


def main() -> int:
    cards = scan_hh_negotiations()
    if not cards:
        print("Карточек не получено (сессия/страница не отдали данные).")
        return 1

    print(f"Всего карточек: {len(cards)}\n")
    flagged = 0
    for i, card in enumerate(cards, 1):
        lines = card.get("lines", [])
        status = card.get("status", "?")
        role = lines[0] if lines else "?"
        employer = lines[1] if len(lines) > 1 else "?"

        joined = " ".join(lines)
        hits = _needle_hits(joined)

        # как Python нормализует роль-«псевдостатус»
        nbsp_role = "\u00a0" in role
        role_is_nbsp_status = (
            "не просмотрен" in role.replace("\u00a0", " ").lower()
        )

        interesting = bool(hits) or nbsp_role or role_is_nbsp_status
        if not interesting:
            continue
        flagged += 1

        print(f"--- #{i} ---")
        print(f"  status (из JS) : {status!r}")
        print(f"  role           : {role!r}")
        print(f"  employer       : {employer!r}")
        print(f"  NBSP в role    : {nbsp_role}")
        print(f"  role = псевдостатус 'Не просмотрен': {role_is_nbsp_status}")
        print(f"  needles в lines: {hits}")
        if hits:
            low = joined.lower()
            for n in hits:
                idx = low.find(n)
                print(f"    '{n}' @ {idx}: ...{joined[max(0,idx-40):idx+40]!r}...")
        print("  lines:")
        for j, ln in enumerate(lines):
            print(f"    [{j}] {ln!r}")
        print("  chat_url       :", card.get("chat_url"))
        print()

    print(f"Показано подозрительных карточек: {flagged} из {len(cards)}")

    # Полный дамп в файл — на случай, если нужно копать глубже.
    out = Path(__file__).resolve().parent.parent / "logs" / "neg_cards_dump.json"
    out.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Полный дамп: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
