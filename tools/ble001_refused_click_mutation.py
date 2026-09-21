"""Мутации №62: отказ `_SUBMIT_CLICK_JS` записан как клик, которого не было.

Что проверяется
---------------
Находка №62 - про две копии обработчика клика (`hh_submission.submit_application`
и `hh_controlled_submit.controlled_real_submit`) и про сам JS. Отказ JS означает,
что `el.click()` не выполнялся, и это надо не угадывать по тексту причины, а
читать по маркеру `refused: true`, который №34 завёл в соседнем скрипте
`execute_hh_submission`. Мутации ломают каждую половину этой правки по
отдельности - JS, первую копию, вторую копию - и встречную (ответ без маркера
обязан остаться попыткой).

Как читается вердикт
--------------------
Вердикты берутся из ``--junitxml``, а не из stdout, и критерий - ``any()``:
вопрос в том, слеп ли набор пинов к ошибке, и одного сработавшего пина
достаточно. Пин, отсутствующий в отчёте, - ``UNKNOWN``, а не ``PASS``: «не
упомянут» и «прошёл» - разные факты. Пин параметризован, поэтому ``FAIL``
важнее ``PASS``: иначе упавший параметр прячется за прошедшим (это измерено
на мутаторе №54, где ``M5`` из-за такого агрегата был записан выжившим).

Файлы восстанавливаются байт-в-байт, хеши печатаются; без восстановления
инструмент вердикта не даёт.

Запуск:
    .venv/Scripts/python.exe tools/ble001_refused_click_mutation.py
    .venv/Scripts/python.exe tools/ble001_refused_click_mutation.py M0 M3
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
HS = REPO / "ai_assistant" / "hh_submission.py"
CS = REPO / "ai_assistant" / "hh_controlled_submit.py"

PIN_JS = "test_the_submit_click_script_says_it_refused_before_it_clicks"
PIN_C1 = "test_a_refused_click_records_no_click_in_the_human_confirmed_path"
PIN_C2 = "test_a_refused_click_records_no_click_in_the_controlled_path"
PIN_CTRL = "test_a_click_answer_without_the_refusal_marker_still_counts_as_an_attempt"
PIN_HEALTHY = "test_a_confirmed_click_still_reports_submitted_and_still_blocks_a_second_attempt"

SHORT = {
    PIN_JS: "js_source",
    PIN_C1: "c1_refusal",
    PIN_C2: "c2_refusal",
    PIN_CTRL: "ctrl_markerless",
    PIN_HEALTHY: "ctrl_healthy",
}

# --- якоря, дословно. Несовпадение якоря инструмент печатает как ошибку, а не
# как «мутация выжила»: протухший якорь и слепые пины выглядят одинаково. -----

# Обе копии читают маркер одинаково и с одинаковым отступом, поэтому якорь один.
MARKER_READ = (
    '    is_pre_click = bool(res.get("refused"))\n'
    "    report.click_count = 0 if is_pre_click else 1\n"
    "    report.submit_count = 0 if is_pre_click else 1\n"
)
PREFIX_READ = (
    "    report.click_count = 1\n"
    "    report.submit_count = 1\n"
)
INVERTED_READ = (
    '    is_pre_click = not bool(res.get("refused"))\n'
    "    report.click_count = 0 if is_pre_click else 1\n"
    "    report.submit_count = 0 if is_pre_click else 1\n"
)
SUBSTRING_READ = (
    '    is_pre_click = "disabled" in (res.get("reason") or "")\n'
    "    report.click_count = 0 if is_pre_click else 1\n"
    "    report.submit_count = 0 if is_pre_click else 1\n"
)

JS_REFUSALS = (
    "    if (!el) return JSON.stringify({ok: false, refused: true,"
    " reason: 'submit button not found'});\n"
    "    if (el.disabled) return JSON.stringify({ok: false, refused: true,"
    " reason: 'submit button is disabled'});\n"
)
JS_WITHOUT_MARKER = (
    "    if (!el) return JSON.stringify({ok: false,"
    " reason: 'submit button not found'});\n"
    "    if (el.disabled) return JSON.stringify({ok: false,"
    " reason: 'submit button is disabled'});\n"
)
JS_SUCCESS = "    return JSON.stringify({ok: true});\n"
JS_SUCCESS_MARKED = "    return JSON.stringify({ok: true, refused: true});\n"

# (имя, файл, описание, старое, новое, пины)
MUTATIONS = [
    (
        "M0",
        CS,
        "вторая копия снова считает клик при отказе (досекционная строка)",
        MARKER_READ,
        PREFIX_READ,
        [PIN_C2, PIN_CTRL],
    ),
    (
        "M1",
        HS,
        "первая копия снова считает клик при отказе (досекционная строка)",
        MARKER_READ,
        PREFIX_READ,
        [PIN_C1, PIN_CTRL],
    ),
    (
        "M2",
        HS,
        "JS отказывается кликать, но маркера не ставит (досекционный JS)",
        JS_REFUSALS,
        JS_WITHOUT_MARKER,
        [PIN_JS, PIN_C1, PIN_C2],
    ),
    (
        "M3",
        CS,
        "маркер читается наоборот: отказ считается кликом, а клик - отказом",
        MARKER_READ,
        INVERTED_READ,
        [PIN_C2, PIN_CTRL],
    ),
    (
        "M4",
        HS,
        "частичная правка: отказ опознаётся по слову, а не по маркеру",
        MARKER_READ,
        SUBSTRING_READ,
        [PIN_C1, PIN_CTRL],
    ),
    (
        "M5",
        HS,
        "маркер ставится и на успешный клик - отказ перестаёт быть отказом",
        JS_SUCCESS,
        JS_SUCCESS_MARKED,
        [PIN_JS, PIN_HEALTHY],
    ),
]

FILES = [HS, CS]


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
                # FAIL важнее PASS: пин параметризован двумя причинами отказа, и
                # прошедший параметр не имеет права перекрыть упавший.
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

    xml = REPO / ".pytest_q62_mutation.xml"
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
    sys.exit(main(sys.argv[1:]))
