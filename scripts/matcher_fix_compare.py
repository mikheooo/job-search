"""До/после правки matcher — изолированно, на одних и тех же данных.

Read-only. Ничего не пишет в state.db.

Гоняет ДВА матчера на одном и том же наборе вакансий и одном профиле:
  * ai_assistant.matcher            — с правкой (demanded/matched разделены)
  * версия из git-индекса           — то, что было до правки

Версия «до» достаётся из git-индекса (`git show :ai_assistant/matcher.py`),
кладётся рядом временным модулем внутри пакета (иначе не работают относительные
импорты) и удаляется в конце. Так сравнивается ровно правка, а не дрейф базы.

ВНИМАНИЕ: версия «до» берётся из ИНДЕКСА, поэтому скрипт имеет смысл только до
того, как правка закоммичена. После коммита он сравнивал бы код с самим собой.

Запуск: .venv/Scripts/python.exe scripts/matcher_fix_compare.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from collections import Counter

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant import db  # noqa: E402
from ai_assistant.candidate_profile import load_candidate_profile  # noqa: E402
from ai_assistant.cli import _row_to_vacancy  # noqa: E402
from ai_assistant import matcher as new_matcher  # noqa: E402

LIMIT = 450
TMP_MODULE = PROJECT_ROOT / "ai_assistant" / "_matcher_prev_tmp.py"


def _write_prev_version() -> None:
    """Materialize the pre-fix matcher from the git index as a package member."""
    src = subprocess.run(
        ["git", "show", ":ai_assistant/matcher.py"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    if "demanded_core" in src:
        raise SystemExit(
            "Индексная версия уже содержит правку (demanded_core) — сравнивать нечего. "
            "Скрипт рассчитан на запуск ДО коммита правки."
        )
    TMP_MODULE.write_text(src, encoding="utf-8")


def main() -> int:
    _write_prev_version()
    try:
        from ai_assistant import _matcher_prev_tmp as old_matcher
        return _run(new_matcher, old_matcher)
    finally:
        TMP_MODULE.unlink(missing_ok=True)


def _run(new_mod, old_mod) -> int:
    db.init_db()
    rows = [r for r in db.list_vacancies(limit=50000) if r]
    # `list_vacancies` yields tuples, not dicts, so the stored decision is read
    # with its own query rather than off the row.
    import sqlite3
    from ai_assistant import config
    conn = sqlite3.connect(config.DB_FILE)
    relevant = {
        r[0] for r in conn.execute(
            "SELECT stable_id FROM vacancies WHERE match_decision IN ('APPLY','REVIEW')"
        ).fetchall()
    }
    conn.close()

    # Two matchers over the full 3.4k base does not fit in one command window,
    # so the sample is capped. Stratified: every APPLY/REVIEW row is kept (that
    # is the population the decision actually matters for) and the rest is
    # sampled with a stride so the tail is represented too.
    pairs = [(_row_to_vacancy(r), r) for r in rows]
    pairs = [(v, r) for v, r in pairs if v is not None]
    keep = [(v, r) for v, r in pairs if v.stable_id() in relevant]
    rest = [(v, r) for v, r in pairs if v.stable_id() not in relevant]
    stride = max(1, len(rest) // max(1, LIMIT - len(keep)))
    sample = keep + rest[::stride]
    print(f"Выборка: {len(keep)} APPLY/REVIEW + {len(sample) - len(keep)} прочих = {len(sample)}")
    profile = load_candidate_profile()

    m_new = new_mod.JobMatcher(profile)
    m_old = old_mod.JobMatcher(profile)

    changed: list[tuple[int, str, int, int, str]] = []
    flips: list[tuple[str, str, int, int, str]] = []
    bogus_removed = 0
    bogus_kept = 0
    new_gaps = 0
    checked = 0
    dec_old = Counter()
    dec_new = Counter()

    for vac, _row in sample:
        checked += 1
        r_old = m_old.match(vac)
        r_new = m_new.match(vac)
        dec_old[r_old.decision] += 1
        dec_new[r_new.decision] += 1

        if r_old.score != r_new.score:
            changed.append((r_new.score - r_old.score, vac.title or "", r_old.score, r_new.score,
                            str(r_new.dimensions.get("skills"))))
        if r_old.decision != r_new.decision:
            flips.append((r_old.decision, r_new.decision, r_old.score, r_new.score, vac.title or ""))

        had_old = any("Missing skills" in g for g in r_old.gaps)
        has_new = any("Missing skills" in g for g in r_new.gaps)
        if had_old and not has_new:
            bogus_removed += 1
        elif had_old and has_new:
            bogus_kept += 1
        elif has_new and not had_old:
            new_gaps += 1

    print(f"Сверено вакансий: {checked}\n")
    print(f"Решения ДО : {dict(dec_old)}")
    print(f"Решения ПОСЛЕ: {dict(dec_new)}\n")

    print(f"Изменили балл: {len(changed)} из {checked}")
    if changed:
        ups = [c for c in changed if c[0] > 0]
        downs = [c for c in changed if c[0] < 0]
        print(f"   вверх: {len(ups)}  (средний {sum(c[0] for c in ups)/len(ups):+.1f})" if ups else "   вверх: 0")
        print(f"   вниз : {len(downs)}  (средний {sum(c[0] for c in downs)/len(downs):+.1f})" if downs else "   вниз : 0")
        print("\n   Топ-12 падений:")
        for d, t, o, n, sk in sorted(changed)[:12]:
            print(f"     {o:>3} -> {n:<3} ({d:+3})  {t[:52]}")
        print("\n   Топ-12 подъёмов:")
        for d, t, o, n, sk in sorted(changed, reverse=True)[:12]:
            print(f"     {o:>3} -> {n:<3} ({d:+3})  {t[:52]}")

    print(f"\nСмена решения: {len(flips)}")
    for od, nd, o, n, t in flips[:30]:
        print(f"   {od:>6} -> {nd:<6}  {o:>3} -> {n:<3}   {t[:52]}")

    print("\nСтроки 'Missing skills:' в gaps")
    print(f"   исчезло (фальшивые сняты) : {bogus_removed}")
    print(f"   осталось                   : {bogus_kept}")
    print(f"   появилось новых            : {new_gaps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
