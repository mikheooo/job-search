"""До/после РАСШИРЕНИЯ ПРОФИЛЯ — изолированно, на одних и тех же данных.

Read-only. Ничего не пишет в state.db.

Отвечает на вопрос «дало ли расширение профиля что-нибудь?», а не «что дала
правка кода». Поэтому матчер ОДИН И ТОТ ЖЕ (текущий), а профилей два:

  * candidate_profile.json                                  — расширенный
  * candidate_profile.json.bak-20261010-t-skills-expansion  — до расширения

Запуск: .venv/Scripts/python.exe scripts/profile_expansion_compare.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from collections import Counter

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant import config, db  # noqa: E402
from ai_assistant.candidate_profile import load_candidate_profile  # noqa: E402
from ai_assistant.cli import _row_to_vacancy  # noqa: E402
from ai_assistant.matcher import JobMatcher  # noqa: E402

LIMIT = 450
BACKUP = PROJECT_ROOT / "candidate_profile.json.bak-20261010-t-skills-expansion"

# Вакансии, по которым правка кода дала просадку, — смотрим, отыграл ли её
# расширенный профиль.
WATCH = [
    "webflow",
    "cribl",
    "старший инженер технической поддержки",
    "bluegeko",
    "data engineer",
    "python (ml)",
]


def main() -> int:
    if not BACKUP.exists():
        raise SystemExit(f"нет бэкапа профиля: {BACKUP}")

    db.init_db()
    rows = [r for r in db.list_vacancies(limit=50000) if r]
    conn = sqlite3.connect(config.DB_FILE)
    relevant = {
        r[0] for r in conn.execute(
            "SELECT stable_id FROM vacancies WHERE match_decision IN ('APPLY','REVIEW')"
        ).fetchall()
    }
    conn.close()

    pairs = [(_row_to_vacancy(r), r) for r in rows]
    pairs = [(v, r) for v, r in pairs if v is not None]
    keep = [(v, r) for v, r in pairs if v.stable_id() in relevant]
    rest = [(v, r) for v, r in pairs if v.stable_id() not in relevant]
    stride = max(1, len(rest) // max(1, LIMIT - len(keep)))
    sample = keep + rest[::stride]
    print(f"Выборка: {len(keep)} APPLY/REVIEW + {len(sample) - len(keep)} прочих = {len(sample)}")

    p_old = load_candidate_profile(BACKUP)
    p_new = load_candidate_profile()
    m_old = JobMatcher(p_old)
    m_new = JobMatcher(p_new)

    changed: list[tuple[int, str, int, int]] = []
    flips: list[tuple[str, str, int, int, str]] = []
    dec_old, dec_new = Counter(), Counter()
    watched: list[tuple[str, int, int, str, str]] = []
    skill_dim_up = skill_dim_down = 0

    for vac, _row in sample:
        r_old = m_old.match(vac)
        r_new = m_new.match(vac)
        dec_old[r_old.decision] += 1
        dec_new[r_new.decision] += 1

        s_old = int((r_old.dimensions.get("skills") or {}).get("score") or 0)
        s_new = int((r_new.dimensions.get("skills") or {}).get("score") or 0)
        if s_new > s_old:
            skill_dim_up += 1
        elif s_new < s_old:
            skill_dim_down += 1

        if r_old.score != r_new.score:
            changed.append((r_new.score - r_old.score, vac.title or "", r_old.score, r_new.score))
        if r_old.decision != r_new.decision:
            flips.append((r_old.decision, r_new.decision, r_old.score, r_new.score, vac.title or ""))

        low = (vac.title or "").lower()
        if any(w in low for w in WATCH):
            watched.append((vac.title or "", r_old.score, r_new.score, r_old.decision, r_new.decision))

    print(f"Сверено вакансий: {len(sample)}\n")
    print(f"Решения ДО : {dict(dec_old)}")
    print(f"Решения ПОСЛЕ: {dict(dec_new)}\n")

    print(f"Измерение skills: выросло у {skill_dim_up}, упало у {skill_dim_down}\n")

    print(f"Изменили балл: {len(changed)} из {len(sample)}")
    if changed:
        ups = [c for c in changed if c[0] > 0]
        downs = [c for c in changed if c[0] < 0]
        print(f"   вверх: {len(ups)}  (средний {sum(c[0] for c in ups)/len(ups):+.1f})" if ups else "   вверх: 0")
        print(f"   вниз : {len(downs)}  (средний {sum(c[0] for c in downs)/len(downs):+.1f})" if downs else "   вниз : 0")
        print("\n   Топ-12 подъёмов:")
        for d, t, o, n in sorted(changed, reverse=True)[:12]:
            print(f"     {o:>3} -> {n:<3} ({d:+3})  {t[:52]}")
        print("\n   Топ-12 падений:")
        for d, t, o, n in sorted(changed)[:12]:
            print(f"     {o:>3} -> {n:<3} ({d:+3})  {t[:52]}")

    print(f"\nСмена решения: {len(flips)}")
    for od, nd, o, n, t in flips[:30]:
        print(f"   {od:>6} -> {nd:<6}  {o:>3} -> {n:<3}   {t[:52]}")

    print("\nКонтрольные вакансии (просадка от правки кода):")
    seen = set()
    for t, o, n, od, nd in watched:
        if t in seen:
            continue
        seen.add(t)
        print(f"   {o:>3} -> {n:<3} ({n-o:+3})  {od:>6}->{nd:<6}  {t[:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
