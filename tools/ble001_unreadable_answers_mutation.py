"""Мутации №63: нечитаемая колонка ответов - улика и запись.

Что проверяется
---------------
Находка №63 состоит из двух половин, и мутации ломают каждую по отдельности:

  * ПИСАТЕЛЬ (`db.update_hh_questionnaire_answers`) не имеет права заменять
    нечитаемые непустые байты колонки пустым словарём - иначе строка делается
    неотличимой от никогда не заполнявшейся, а пометка №46 (`answers_error`)
    обнуляется;
  * ЧИТАТЕЛИ (`validate_human_answers`, гейт 5, `audit_questionnaire`) обязаны
    назвать колонку, а не судить о человеке («ответа нет»).

Кроме «вернуть дефект» есть мутации «предохранитель шире дефекта»: запретить
запись настоящих ответов, запретить любую пустую запись, отказать на ответы,
пришедшие от человека, завалить гейт при живых ответах приложения. Каждая из них
обязана быть поймана своим контролем - предохранитель, который отказывает всегда,
защитой не является.

Как читается вердикт
--------------------
Вердикты берутся из ``--junitxml``, а не из stdout, и критерий - ``any()``: вопрос
в том, слеп ли набор пинов к ошибке, и одного сработавшего пина достаточно. Пин,
отсутствующий в отчёте, - ``UNKNOWN``, а не ``PASS``. ``FAIL`` важнее ``PASS``:
упавший параметр не имеет права спрятаться за прошедшим.

Файлы восстанавливаются байт-в-байт, хеши печатаются; без восстановления
инструмент вердикта не даёт.

Запуск:
    .venv/Scripts/python.exe tools/ble001_unreadable_answers_mutation.py
    .venv/Scripts/python.exe tools/ble001_unreadable_answers_mutation.py M0 M5
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
QU = REPO / "ai_assistant" / "hh_questionnaire.py"
PO = REPO / "ai_assistant" / "hh_submit_policy.py"
AU = REPO / "ai_assistant" / "hh_questionnaire_audit.py"

# --- пины №63 -------------------------------------------------------------
PIN_WRITER = "test_an_unreadable_answers_column_is_not_laundered_into_an_empty_one"
PIN_WRITER_REAL = "test_real_answers_still_replace_an_unreadable_column"
PIN_WRITER_READABLE = "test_a_readable_column_is_still_overwritten_by_an_empty_write"
PIN_VALIDATE = "test_validate_human_answers_names_the_unreadable_answers_column"
PIN_VALIDATE_CTL = "test_validate_still_accepts_answers_the_human_supplied"
PIN_GATE = "test_gate_5_names_the_unreadable_answers_column"
PIN_GATE_CTL = "test_gate_5_still_uses_the_applications_answers_when_they_exist"
PIN_AUDIT = "test_the_audit_names_the_unreadable_answers_column"
PIN_RUNNER = "test_the_runner_path_does_not_erase_the_unreadable_column"
PIN_CLI = "test_the_cli_door_does_not_erase_the_unreadable_column"

# --- соседи, которых правка не имеет права двигать ------------------------
PIN_Q60_SETTER = "test_the_writer_is_a_setter_not_a_merger_so_the_argument_is_the_defect"
PIN_Q60_KEEP = "test_a_confirmed_submission_keeps_the_answers_the_questionnaire_was_submitted_with"
PIN_Q46_ANS = "test_unreadable_answers_still_fail_closed"

SHORT = {
    PIN_WRITER: "writer_erase",
    PIN_WRITER_REAL: "writer_real",
    PIN_WRITER_READABLE: "writer_readable",
    PIN_VALIDATE: "validate_names",
    PIN_VALIDATE_CTL: "validate_ctl",
    PIN_GATE: "gate_names",
    PIN_GATE_CTL: "gate_ctl",
    PIN_AUDIT: "audit_names",
    PIN_RUNNER: "runner_path",
    PIN_CLI: "cli_path",
    PIN_Q60_SETTER: "q60_setter",
    PIN_Q60_KEEP: "q60_keeps",
    PIN_Q46_ANS: "q46_answers",
}

# --- якоря, дословно. Несовпадение якоря печатается как ошибка, а не как
# «мутация выжила»: протухший якорь и слепые пины выглядят одинаково. ---------

GUARD = "    if not answers and _answers_json_is_unreadable(cur, qid):\n"
GUARD_OFF = "    if False and not answers and _answers_json_is_unreadable(cur, qid):\n"
GUARD_EMPTY_ONLY = "    if not answers:\n"
GUARD_ALWAYS = "    if True:\n"
STATUS_PARAMS = "                (new_status, now, qid),\n"
STATUS_NONE = "                (None, now, qid),\n"

VALIDATE_GUARD = "    if questionnaire.answers_error and not human_answers:\n"
VALIDATE_OFF = "    if False and questionnaire.answers_error and not human_answers:\n"
VALIDATE_WIDE = "    if questionnaire.answers_error:\n"

GATE_GUARD = '                if quest.answers_error and not app.get("answers"):\n'
GATE_OFF = '                if False and quest.answers_error and not app.get("answers"):\n'
GATE_WIDE = "                if quest.answers_error:\n"

AUDIT_GUARD = "    if quest.answers_error and not answers:\n"
AUDIT_OFF = "    if False and quest.answers_error and not answers:\n"

# (имя, файл, описание, старое, новое, пины)
MUTATIONS = [
    (
        "M0",
        DB,
        "писатель снова затирает нечитаемую колонку пустым словарём",
        GUARD,
        GUARD_OFF,
        [PIN_WRITER, PIN_RUNNER, PIN_CLI],
    ),
    (
        "M1",
        DB,
        "предохранитель шире дефекта: запрещена ЛЮБАЯ пустая запись",
        GUARD,
        GUARD_EMPTY_ONLY,
        [PIN_WRITER_READABLE, PIN_Q60_SETTER],
    ),
    (
        "M2",
        DB,
        "предохранитель шире дефекта: писатель отказывает всегда",
        GUARD,
        GUARD_ALWAYS,
        [PIN_WRITER_REAL, PIN_WRITER_READABLE, PIN_Q60_SETTER],
    ),
    (
        "M3",
        DB,
        "отказ от записи обнуляет статус - статус перестаёт обновляться",
        STATUS_PARAMS,
        STATUS_NONE,
        [PIN_WRITER],
    ),
    (
        "M4",
        QU,
        "инвариант №63 в валидаторе снят: запись снова говорит о человеке",
        VALIDATE_GUARD,
        VALIDATE_OFF,
        [PIN_VALIDATE, PIN_RUNNER],
    ),
    (
        "M5",
        QU,
        "валидатор шире дефекта: отказ и на ответы, пришедшие от человека",
        VALIDATE_GUARD,
        VALIDATE_WIDE,
        [PIN_VALIDATE_CTL, PIN_Q60_KEEP],
    ),
    (
        "M6",
        PO,
        "гейт 5 снова записывает missing_answer_for_* вместо колонки",
        GATE_GUARD,
        GATE_OFF,
        [PIN_GATE, PIN_Q46_ANS],
    ),
    (
        "M7",
        PO,
        "гейт 5 шире дефекта: валит анкету и когда ответы взяты у заявки",
        GATE_GUARD,
        GATE_WIDE,
        [PIN_GATE_CTL],
    ),
    (
        "M8",
        AU,
        "аудит снова отказывает пунктом на каждый вопрос, а не на колонку",
        AUDIT_GUARD,
        AUDIT_OFF,
        [PIN_AUDIT],
    ),
]

FILES = [DB, QU, PO, AU]


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

    xml = REPO / ".pytest_q63_mutation.xml"
    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    survived = []
    try:
        for name, target, description, old, new, pins in selected:
            original = originals[target]
            count = original.count(old.encode())
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times in "
                      f"{target.name}, expected 1: {old.splitlines()[0][:60]!r}")
                survived.append(name)
                continue
            mutated = original.replace(old.encode(), new.encode(), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                survived.append(name)
                continue

            target.write_bytes(mutated)
            verdicts = run_pins(pins, xml)
            fired = [p for p in pins if verdicts[p] == "FAIL"]
            unknown = [p for p in pins if verdicts[p] == "UNKNOWN"]
            caught = bool(fired)
            print(f"{name:4s} {'caught' if caught else 'SURVIVED':9s} {description}")
            print("     evidence: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in pins))
            if unknown:
                print(f"     !! never ran (UNKNOWN, not PASS): "
                      f"{', '.join(SHORT[p] for p in unknown)}")
            if not caught:
                survived.append(name)
            target.write_bytes(original)
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
    if survived:
        print(f"RESULT: {len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"RESULT: all {len(selected)} mutations caught, tree restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
