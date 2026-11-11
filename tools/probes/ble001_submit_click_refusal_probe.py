"""Проба №62: что `_SUBMIT_CLICK_JS` отдаёт, когда отказывается кликать, и что
записывают об этом две копии обработчика.

Зачем отдельная проба, если есть №61
------------------------------------
Проба №61 мерит ту же ветку (`FAILED`), но её стенд подсовывает синтетический
payload `{"ok": false, "reason": "..."}` - **без** маркера отказа. Пока маркера
не было ни в стенде, ни в JS, это совпадало. После №62 маркер есть в JS, и
стенд №61 обязан его повторять, иначе проба покажет «ничего не изменилось» и
разойдётся с кодом. Чтобы не переписывать payload по памяти, он здесь
**измеряется**: настоящий `_SUBMIT_CLICK_JS` берётся из модуля и исполняется на
node в трёх формах DOM.

Почему payload вообще надо измерять
-----------------------------------
`_SUBMIT_CLICK_JS` - строка Python, которую Python никогда не исполняет, а все
тесты подменяют `evaluate_fn`. Значит pytest может пинить только текст. Что JS
делает на самом деле, видно только на движке (тот же приём, что в №34,
`tools/ble001_js_guard_harness.py`).

Вопросы пробы
-------------
  Q1 форма "missing"  - кнопки нет:  что отдаёт JS, нажал ли он что-нибудь
  Q2 форма "disabled" - кнопка выключена: то же
  Q3 форма "enabled"  - кнопка есть и активна (контроль): клик обязан быть
  Q4 что записывают обе копии обработчика, получив ИЗМЕРЕННЫЙ payload отказа
  Q5 контроль: синтетический payload без маркера - запись не меняется

Запуск:
    .venv/Scripts/python.exe tools/ble001_submit_click_refusal_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный, JS
уезжает в node через временный каталог. Node обязателен; без него проба
завершается ненулевым кодом, а не молчаливым успехом.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Килл-свитч отправки (находка #19) читается на вызове, а не при импорте.
os.environ["SUBMIT_ALLOWED"] = "true"

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-click-refusal-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.application_review_gate import (
    GateStatus,
    HumanReviewStore,
    build_review_gate,
)
from ai_assistant.hh_controlled_submit import controlled_real_submit
from ai_assistant.hh_extractor import (
    ApplicationAnswer,
    ApplicationForm,
    ApplicationQuestion,
    ApplicationType,
    QuestionSource,
    QuestionType,
)
from ai_assistant.hh_human_submission import (
    clear_all_submission_state,
    confirm_human_submission,
)
from ai_assistant.hh_submission import (
    _SUBMIT_CLICK_JS,
    _submitted_reviews,
    submit_application,
)
from ai_assistant.prefill_orchestrate import GroupCheck, OrchestrationReport
from ai_assistant.prefill_plan import PrefillPlan

MEASURED = [
    "ai_assistant/hh_submission.py",
    "ai_assistant/hh_controlled_submit.py",
]

VAC = "hh:136591579"

# Стенд для node: одна кнопка, форму задаёт argv. `click()` считает нажатия -
# «JS вернул ok:false» и «клика не было» обязаны быть одним и тем же фактом, и
# проверить это можно только здесь.
NODE_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const shape = process.argv[3];

let clicked = 0;
const el = shape === 'missing' ? null : {
  tagName: 'BUTTON',
  disabled: shape === 'disabled',
  innerText: 'Откликнуться',
  className: 'magritte-button_mode-primary',
  offsetWidth: 100,
  getAttribute: () => 'vacancy-response-submit-popup',
  getClientRects: () => [{}],
  click() { clicked += 1; },
};

global.document = { querySelector: () => el };

let raw;
try {
  raw = eval(src);
} catch (err) {
  console.log(JSON.stringify({ shape, threw: String(err), clicked }));
  process.exit(0);
}
console.log(JSON.stringify({ shape, payload: JSON.parse(raw), clicked }));
"""


def _node() -> str:
    for name in (os.environ.get("NODE_BIN"), "node", "node.exe"):
        if name and shutil.which(name):
            return shutil.which(name)
    raise SystemExit("node не найден: проба меряет поведение JS и без движка "
                     "не имеет права отчитываться об успехе")


def _run_js(shape: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="ble001-click-js-") as work:
        js_path = os.path.join(work, "submit_click.js")
        harness_path = os.path.join(work, "harness.js")
        with open(js_path, "w", encoding="utf-8") as fh:
            fh.write(_SUBMIT_CLICK_JS)
        with open(harness_path, "w", encoding="utf-8") as fh:
            fh.write(NODE_HARNESS)
        proc = subprocess.run(
            [_node(), harness_path, js_path, shape],
            capture_output=True, text=True, encoding="utf-8", check=False,
            cwd=work)
    if proc.returncode != 0:
        raise SystemExit(f"node упал на форме {shape!r}: {proc.stderr.strip()}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class Dom:
    """Поддельный браузер, отдающий ИЗМЕРЕННЫЙ payload отказа.

    `click_refusal` - готовый dict, который вернёт evaluate на клик-скрипт.
    Ничего не додумывает: проба обязана кормить обработчик тем же, что отдаёт
    настоящий JS.
    """

    URL = f"https://hh.ru/applicant/vacancy_response?vacancyId={VAC.split(':')[1]}"

    def __init__(self, *, click_refusal: dict | None = None, markers=()):
        self.clicks = 0
        self.click_js_runs = 0
        self.click_refusal = click_refusal
        self.markers = list(markers)

    def evaluate(self, expression: str) -> str:
        if expression == "JSON.stringify({url: location.href})":
            return json.dumps({"url": self.URL})
        if "markers" in expression and "body" in expression:
            return json.dumps({"found": self.markers, "url": self.URL})
        if '"vacancy-response-submit-popup"' in expression and "el.click()" not in expression:
            return json.dumps({
                "found": True, "tag": "BUTTON", "type": "submit",
                "text": "Откликнуться",
                "dataQa": "vacancy-response-submit-popup",
                "disabled": False, "visible": True,
                "cls": "magritte-button_mode-primary"})
        if "el.click()" in expression:
            self.click_js_runs += 1
            if self.click_refusal is not None:
                return json.dumps(self.click_refusal)
            self.clicks += 1
            return json.dumps({"ok": True})
        raise RuntimeError(f"unknown expression: {expression[:80]}")


def _approved_setup():
    clear_all_submission_state()
    form = ApplicationForm(
        source="hh", vacancy_stable_id=VAC,
        application_type=ApplicationType.screening_questions,
        questions=[
            ApplicationQuestion(id="hh__ctrl_task_384589146", label="Опыт?",
                                normalized_type=QuestionType.RADIO, required=False,
                                options=["Менее 3 лет", "5-7 лет"],
                                source=QuestionSource.SCREENING),
        ])
    pkg = ApplicationPackage(
        vacancy_id=VAC, vacancy_stable_id=VAC,
        resume_adaptation_needed=False, resume_summary="s",
        tailored_skills=["python"], relevant_experience=["e"],
        cover_letter="Hello " + " ".join(["word"] * 130), application_strategy="st",
        warnings=[], generator_version="v1",
        adaptation=ResumeAdaptation(target_title="t", professional_summary="p",
                                    prioritized_skills=["python"],
                                    relevant_experience_points=["e"]))
    pkg.validation_status = "VALID"
    pkg.answers = [
        ApplicationAnswer(question_id="hh__ctrl_task_384589146",
                          answer="Менее 3 лет", answer_type=QuestionType.RADIO,
                          confidence=1.0, requires_review=False, reason="c"),
    ]
    plan = PrefillPlan(vacancy_stable_id=VAC, status="VALID")
    orch = OrchestrationReport(
        vacancy_stable_id=VAC,
        verdict="VERIFIED", planned_operations=1, executed_operations=1,
        verified_operations=1, failed_operations=0, skipped_operations=0,
        group_checks=[
            GroupCheck(group_name="task_384589146", input_type="radio",
                       expected_checked=["Менее 3 лет"],
                       actual_checked=["Менее 3 лет"], ok=True),
        ])
    gate = build_review_gate(pkg, plan, orch, {"controls": []}, form)
    assert gate.status == GateStatus.READY_FOR_HUMAN_REVIEW, gate.block_reasons
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    assert store.approve_review(rid, gate.fingerprint)["ok"] is True
    assert confirm_human_submission(store, rid, gate.fingerprint, VAC)["ok"] is True
    return gate, plan, orch, pkg, store, rid


def _show(label: str, rep) -> None:
    """Печатает запись, не зная заранее её тип: у `SubmissionReport` вердикт лежит
    в `status`, у `ControlledSubmitReport` - в `verdict`."""
    verdict = getattr(rep, "verdict", None) or getattr(rep, "status", None)
    verdict = getattr(verdict, "value", verdict)
    reason = getattr(rep, "stop_reason", None) or getattr(rep, "reason", "")
    print(f"    {label}: вердикт={verdict!r} click_count={rep.click_count} "
          f"submit_count={rep.submit_count} failed={rep.failed_submit} "
          f"successful={rep.successful_submit}")
    print(f"      reason: {reason!r}")


def _hashes() -> dict[str, str]:
    out = {}
    for rel in MEASURED:
        with open(os.path.join(REPO, rel), "rb") as fh:
            out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def main() -> int:
    print("=" * 78)
    print("№62. Что `_SUBMIT_CLICK_JS` отдаёт, когда отказывается кликать")
    print("=" * 78)
    print("Источник JS:", "ai_assistant/hh_submission.py:_SUBMIT_CLICK_JS")
    for rel, digest in _hashes().items():
        print(f"  sha256 {rel}: {digest[:16]}...")
    print()

    print("Q1-Q3. Настоящий JS на node, три формы DOM")
    measured: dict[str, dict] = {}
    for shape in ("missing", "disabled", "enabled"):
        got = _run_js(shape)
        measured[shape] = got
        payload = got.get("payload")
        print(f"  {shape:<9} payload={json.dumps(payload, ensure_ascii=False)} "
              f"click() вызван: {got['clicked']} раз")
    print()

    refusal_payload = measured["missing"].get("payload") or {}
    marker = "refused" if refusal_payload.get("refused") is True else None
    print(f"  маркер отказа в payload: {marker!r}")
    print(f"  отказ совпадает с отсутствием клика: "
          f"{measured['missing']['clicked'] == 0 and measured['disabled']['clicked'] == 0}")
    print(f"  контроль (enabled): клик есть: {measured['enabled']['clicked'] == 1}")
    print()

    print("Q4. Запись обеих копий на ИЗМЕРЕННОМ payload отказа")
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = Dom(click_refusal=refusal_payload)
    rep = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch,
                                 dom.evaluate)
    _show("controlled_real_submit", rep)
    print(f"      браузер: click_js_runs={dom.click_js_runs} clicks={dom.clicks}")
    if rep.click_count and dom.clicks == 0:
        print("      <-- клика не было, а запись считает клик")

    gate, plan, orch, pkg, store, rid = _approved_setup()
    _submitted_reviews.discard(rid)
    dom1 = Dom(click_refusal=refusal_payload)
    rep1 = submit_application(store, rid, gate.fingerprint, pkg, plan, orch,
                              dom1.evaluate)
    _show("submit_application   ", rep1)
    print(f"      браузер: click_js_runs={dom1.click_js_runs} clicks={dom1.clicks}")
    print()

    print("Q5. Контроль: синтетический payload БЕЗ маркера")
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom2 = Dom(click_refusal={"ok": False, "reason": "click intercepted"})
    rep2 = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch,
                                  dom2.evaluate)
    _show("controlled_real_submit", rep2)
    print("      (это форма легаси-пинов test_stage20i/j: попытка без маркера)")
    print()

    print("=" * 78)
    print("Свод:")
    print(f"  отказ JS (missing)   : ok={refusal_payload.get('ok')!r} "
          f"refused={refusal_payload.get('refused')!r} "
          f"кликов у JS={measured['missing']['clicked']}")
    print(f"  отказ JS (disabled)  : ok={measured['disabled']['payload'].get('ok')!r} "
          f"refused={measured['disabled']['payload'].get('refused')!r} "
          f"кликов у JS={measured['disabled']['clicked']}")
    print(f"  контроль (enabled)   : ok={measured['enabled']['payload'].get('ok')!r} "
          f"кликов у JS={measured['enabled']['clicked']}")
    print(f"  запись controlled    : click_count={rep.click_count} "
          f"submit_count={rep.submit_count} при кликах {dom.clicks}")
    print(f"  запись submit_app    : click_count={rep1.click_count} "
          f"submit_count={rep1.submit_count} при кликах {dom1.clicks}")
    print(f"  запись без маркера   : click_count={rep2.click_count} "
          f"submit_count={rep2.submit_count} при кликах {dom2.clicks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
