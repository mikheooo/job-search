"""Мутации №64: повторное обнаружение анкеты откатывает её статус.

Что проверяется
---------------
Правка №64 узкая ровно в одном месте - в ветке `DO UPDATE` upsert'а
`db.save_hh_questionnaire()`. Дефект - `status = excluded.status`, безусловная
перезапись; предохранитель - записывать статус только тогда, когда полезная
нагрузка называет статус, ОТЛИЧНЫЙ от мнения по умолчанию обнаружения
(`NEEDS_HUMAN_REVIEW`).

Мутации ломают предохранитель шестью разными способами, и каждый обязан быть
пойман своим пином:

  * M0 «вернуть дефект» - перезапись снова безусловна;
  * M1 «шире дефекта» - статус не двигается вообще при конфликте;
  * M2 «литерал протух по регистру» - `needs_human_review` не совпадёт никогда,
    то есть CASE вырождается в безусловную перезапись;
  * M3 «сравнение с самим собой» - в `?` подставлен тот же статус, что и в
    `excluded.status`, условие всегда истинно;
  * M4 «сравнение перевёрнуто» - предохранитель срабатывает на НАСТОЯЩИХ
    статусах и пропускает мнение по умолчанию;
  * M5 «не тот операнд» - сравнивается сохранённый статус, а не пришедший;
  * M6 «литерал протух в другой член перечисления» - `SUBMITTED` вместо
    `NEEDS_HUMAN_REVIEW`; этот случай интересен тем, что пин на членство в
    перечислении его НЕ ловит (SUBMITTED - настоящий член), и ловят его только
    поведенческие пины.

M1, M3 и M6 вторая половина - это «предохранитель шире дефекта»: он обязан быть
пойман контролем `test_a_payload_that_advances_the_status_is_still_written`.
Предохранитель, который вообще не даёт статусу двигаться, защитой не является.

Осторожно с заполнителями
-------------------------
M0 и M1 держат `?` на месте нарочно. Первая редакция убирала его вместе с
CASE, а двенадцатый аргумент в кортеже параметров оставался, - sqlite отвечал
`Incorrect number of bindings supplied`, и падал каждый вызов писателя. Вердикт
«поймана» при этом был, но измерял он связывание, а не предохранитель: три
контроля краснели вместе с пинами. Проверка `_placeholder_delta` теперь валит
такую мутацию как ошибку построения, а не пропускает её как пойманную.

Как читается вердикт
--------------------
Вердикты берутся из ``--junitxml``, а не из stdout, и критерий - ``any()``:
вопрос в том, слеп ли набор пинов к ошибке, и одного сработавшего пина
достаточно. Пин, отсутствующий в отчёте, - ``UNKNOWN``, а не ``PASS``.
``FAIL`` важнее ``PASS``: упавший параметр не имеет права спрятаться за
прошедшим.

Соседи печатаются в доказательство, но вердикта не выносят: пин №63 на писателя
ответов и пин №60 «писатель - сеттер, а не слияние» обязаны оставаться
зелёными при любой из мутаций.

Файлы восстанавливаются байт-в-байт ИЗ ПАМЯТИ (не перечитыванием с диска - так
уже был потерян весь №63, см. отчёт, «Ошибка первая»), хеши печатаются; без
восстановления инструмент вердикта не даёт.

Запуск:
    .venv/Scripts/python.exe tools/ble001_questionnaire_rediscovery_mutation.py
    .venv/Scripts/python.exe tools/ble001_questionnaire_rediscovery_mutation.py M0 M5
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"
DB = REPO / "ai_assistant" / "db.py"

# --- пины №64 --------------------------------------------------------------
PIN_SUBMITTED = "test_a_rediscovery_does_not_rewind_a_submitted_questionnaire"
PIN_READY = "test_a_rediscovery_does_not_rewind_a_ready_to_submit_questionnaire"
PIN_BLOCKED = "test_a_rediscovery_does_not_flatten_a_blocked_questionnaire"
PIN_NEW = "test_a_rediscovery_still_creates_a_new_questionnaire_as_needs_review"
PIN_ANSWERS = "test_a_rediscovery_still_keeps_the_answers"
PIN_ONESHOT = "test_the_one_shot_invariant_still_fires_after_a_rediscovery"
PIN_ADVANCE = "test_a_payload_that_advances_the_status_is_still_written"
PIN_CLI = "test_the_live_cli_caller_does_not_rewind_the_status"
PIN_LITERAL = "test_the_discovery_status_literal_matches_the_enum"

# --- соседи, которых правка не имеет права двигать ------------------------
NB_Q63_WRITER = "test_an_unreadable_answers_column_is_not_laundered_into_an_empty_one"
NB_Q60_SETTER = "test_the_writer_is_a_setter_not_a_merger_so_the_argument_is_the_defect"
NB_Q63_READABLE = "test_a_readable_column_is_still_overwritten_by_an_empty_write"

SHORT = {
    PIN_SUBMITTED: "submitted",
    PIN_READY: "ready",
    PIN_BLOCKED: "blocked",
    PIN_NEW: "new_row",
    PIN_ANSWERS: "answers",
    PIN_ONESHOT: "one_shot",
    PIN_ADVANCE: "advance",
    PIN_CLI: "cli_path",
    PIN_LITERAL: "literal",
    NB_Q63_WRITER: "nb_q63_writer",
    NB_Q60_SETTER: "nb_q60_setter",
    NB_Q63_READABLE: "nb_q63_readable",
}

# --- якоря, дословно. Несовпадение якоря печатается как ошибка, а не как
# «мутация выжила»: протухший якорь и слепые пины выглядят одинаково. --------

CASE_GUARD = (
    "            status = CASE\n"
    "                WHEN excluded.status = ? THEN hh_questionnaires.status\n"
    "                ELSE excluded.status\n"
    "            END,\n"
)
# M0 и M1 держат `?` на месте нарочно. Первая редакция писала их как
# `status = excluded.status,` и `status = hh_questionnaires.status,` - то есть
# убирала заполнитель, оставляя двенадцатый аргумент в кортеже параметров. sqlite
# отвечал `Incorrect number of bindings supplied`, падал КАЖДЫЙ вызов писателя, и
# три контроля краснели не из-за ширины предохранителя, а из-за связывания. Это
# ровно тот случай, когда «мутация поймана» измеряет не то, что заявлено. Ниже
# `_placeholder_delta` не даёт этой ошибке повториться молча.
CASE_DEFECT = (
    "            status = CASE\n"
    "                WHEN excluded.status = ? THEN excluded.status\n"
    "                ELSE excluded.status\n"
    "            END,\n"
)
CASE_FROZEN = (
    "            status = CASE\n"
    "                WHEN excluded.status = ? THEN hh_questionnaires.status\n"
    "                ELSE hh_questionnaires.status\n"
    "            END,\n"
)
CASE_INVERTED = (
    "            status = CASE\n"
    "                WHEN excluded.status <> ? THEN hh_questionnaires.status\n"
    "                ELSE excluded.status\n"
    "            END,\n"
)
CASE_WRONG_OPERAND = (
    "            status = CASE\n"
    "                WHEN hh_questionnaires.status = ? THEN hh_questionnaires.status\n"
    "                ELSE excluded.status\n"
    "            END,\n"
)

LITERAL_DEF = '_DISCOVERY_STATUS = "NEEDS_HUMAN_REVIEW"\n'
LITERAL_LOWER = '_DISCOVERY_STATUS = "needs_human_review"\n'
LITERAL_SUBMITTED = '_DISCOVERY_STATUS = "SUBMITTED"\n'

PARAM_LITERAL = "        _DISCOVERY_STATUS,\n"
PARAM_SELF = '        data.get("status", "NEEDS_HUMAN_REVIEW"),\n'

REWIND_PINS = [PIN_SUBMITTED, PIN_READY, PIN_BLOCKED, PIN_ONESHOT, PIN_CLI]

# (имя, описание, старое, новое, пины)
MUTATIONS = [
    (
        "M0",
        "дефект возвращён: статус перезаписывается безусловно",
        CASE_GUARD,
        CASE_DEFECT,
        REWIND_PINS,
    ),
    (
        "M1",
        "предохранитель шире дефекта: при конфликте статус не двигается вообще",
        CASE_GUARD,
        CASE_FROZEN,
        [PIN_ADVANCE],
    ),
    (
        "M2",
        "литерал протух по регистру: CASE вырождается в безусловную перезапись",
        LITERAL_DEF,
        LITERAL_LOWER,
        REWIND_PINS + [PIN_LITERAL],
    ),
    (
        "M3",
        "сравнение с самим собой: в ? подставлен тот же статус, условие всегда истинно",
        PARAM_LITERAL,
        PARAM_SELF,
        [PIN_ADVANCE],
    ),
    (
        "M4",
        "сравнение перевёрнуто: предохранитель ловит настоящие статусы, а не мнение по умолчанию",
        CASE_GUARD,
        CASE_INVERTED,
        REWIND_PINS + [PIN_ADVANCE],
    ),
    (
        "M5",
        "не тот операнд: сравнивается сохранённый статус, а не пришедший",
        CASE_GUARD,
        CASE_WRONG_OPERAND,
        REWIND_PINS + [PIN_ADVANCE],
    ),
    (
        "M6",
        "литерал протух в другой член перечисления: SUBMITTED вместо NEEDS_HUMAN_REVIEW",
        LITERAL_DEF,
        LITERAL_SUBMITTED,
        REWIND_PINS + [PIN_ADVANCE, PIN_LITERAL],
    ),
]

# Пины, которые обязаны оставаться зелёными при КАЖДОЙ мутации: узость
# предохранителя и соседи из №63 и №60.
CONTROLS = [PIN_NEW, PIN_ANSWERS, NB_Q63_WRITER, NB_Q60_SETTER, NB_Q63_READABLE]

FILES = [DB]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pins(pins: list[str], xml: Path) -> dict[str, str]:
    """PASS / FAIL / UNKNOWN по каждому пину, из junit."""
    expr = " or ".join(pins)
    subprocess.run(
        [str(VENV_PY), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "-k", expr, f"--junitxml={xml}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    seen: dict[str, str] = {}
    if xml.exists():
        root = ET.parse(xml).getroot()
        for case in root.iter("testcase"):
            name = case.get("name") or ""
            bad = case.find("failure") is not None or case.find("error") is not None
            for pin in pins:
                # FAIL важнее PASS: параметризованный пин не имеет права отдать
                # свой вердикт прошедшему параметру.
                if name.startswith(pin) and (bad or pin not in seen):
                    seen[pin] = "FAIL" if bad else "PASS"
    return {pin: seen.get(pin, "UNKNOWN") for pin in pins}


def _placeholder_delta(old: str, new: str) -> str | None:
    """Заполнители SQL обязаны совпасть по числу.

    Мутация, меняющая число `?` и не трогающая кортеж параметров, роняет sqlite
    на связывании - и тогда «поймана» означает «все вызовы писателя упали», а не
    «предохранитель проверен». Это ошибка построения мутации, и она должна быть
    названа ошибкой, а не зачтена как попадание.
    """
    if old.count("?") == new.count("?"):
        return None
    return (f"changes the SQL placeholder count ({old.count('?')} -> "
            f"{new.count('?')}) while the parameter tuple is untouched: sqlite "
            f"would fail on binding, not on the guard")


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("-")]
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    if not selected:
        print(f"no mutation matches {wanted}; known: {[m[0] for m in MUTATIONS]}")
        return 2

    originals = {p: p.read_bytes() for p in FILES}
    before = {p: sha(p) for p in originals}
    for path, digest in before.items():
        print(f"{path.name} sha256 before: {digest}")
    print(f"{len(selected)} of {len(MUTATIONS)} mutations\n")

    xml = REPO / ".pytest_q64_mutation.xml"
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    survived = []
    ill_formed = []
    controls_broken = []
    try:
        for name, description, old, new, pins in selected:
            original = originals[DB]
            count = original.count(old.encode())
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times in "
                      f"{DB.name}, expected 1: {old.splitlines()[0][:60]!r}")
                survived.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                survived.append(name)
                continue
            bad_shape = _placeholder_delta(old, new)
            if bad_shape:
                print(f"{name:4s} !! malformed mutation: {bad_shape}")
                ill_formed.append(name)
                continue

            DB.write_bytes(mutated)
            all_pins = pins + CONTROLS
            verdicts = run_pins(all_pins, xml)
            fired = [p for p in pins if verdicts[p] == "FAIL"]
            unknown = [p for p in all_pins if verdicts[p] == "UNKNOWN"]
            broken = [p for p in CONTROLS if verdicts[p] != "PASS"]
            caught = bool(fired)
            print(f"{name:4s} {'caught' if caught else 'SURVIVED':9s} {description}")
            print("     evidence: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in pins))
            print("     controls: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in CONTROLS))
            if unknown:
                print(f"     !! never ran (UNKNOWN, not PASS): "
                      f"{', '.join(SHORT[p] for p in unknown)}")
            if not caught:
                survived.append(name)
            if broken:
                controls_broken.append(f"{name}: {', '.join(SHORT[p] for p in broken)}")
            DB.write_bytes(original)
    finally:
        for path, data in originals.items():
            path.write_bytes(data)

    print()
    ok = True
    for path, digest in before.items():
        after = sha(path)
        same = after == digest
        ok = ok and same
        print(f"{path.name} sha256 after : {after}")
        print(f"  restored byte-for-byte: {same}")
    if not ok:
        return 2

    print()
    if ill_formed:
        print(f"MALFORMED MUTATIONS (not verdicts): {', '.join(ill_formed)}")
    if controls_broken:
        print("CONTROL BREAKAGE (the guard is wider than the defect, or a "
              "neighbour moved):")
        for line in controls_broken:
            print(f"  {line}")
    if survived or ill_formed:
        if survived:
            print(f"RESULT: {len(survived)} mutation(s) survived: {', '.join(survived)}")
        if ill_formed:
            print(f"RESULT: {len(ill_formed)} mutation(s) malformed: "
                  f"{', '.join(ill_formed)}")
        return 1
    print(f"RESULT: all {len(selected)} mutations caught, controls intact, "
          f"tree restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
