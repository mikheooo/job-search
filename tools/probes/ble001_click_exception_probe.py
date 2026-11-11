"""Проба №61: путь исключения при клике записывает «клика не было» и не взводит
одноразовый предохранитель.

Три реализации одного и того же клика живут в двух модулях:

  * `hh_submission.submit_application()` (~:418-434) - взводит одноразовый
       предохранитель ДО клика, а на исключении отдаёт `FAILED`
  * `hh_submission.execute_hh_submission()` (~:1400-1441) - на исключении пишет
       `AMBIGUOUS` и `submit_executed: "unknown"` плюс след в БД
  * `hh_controlled_submit.controlled_real_submit()` (:139-156) - взводит
       предохранитель ПОСЛЕ клика, а на исключении отдаёт `FAIL_CLOSED` и
       `click_count = 0`, `submit_count = 0`

Вопрос пробы: что этот третий путь записывает о клике, которого он не видел, и
взведён ли после него одноразовый предохранитель.

Почему исключение вообще возможно после клика. `_SUBMIT_CLICK_JS`
(hh_submission.py:78-85) выполняет `el.click()` строкой :83 и только потом
возвращает JSON строкой :84. Значит потеря ответа CDP (навигация, таймаут)
приходит как исключение ПОСЛЕ того, как кнопка уже нажата. Отличить «JS не
выполнялся» от «JS нажал и ответ потерялся» обработчик не может - и записывает
в обоих случаях одно и то же.

Замеры (состояние ДО правки №61, HEAD a2311b6):

  * S1 транспорт упал ДО клика: что записано и взведён ли предохранитель
  * S2 транспорт упал ПОСЛЕ клика (браузер щёлкнул): та же запись
  * S3 материальное: второй прямой вызов того же отзыва доходит до ВТОРОГО клика
  * S4 контроль: здоровый первый вызов взводит предохранитель, второй - BLOCKED
  * S5 контраст: копия-близнец `submit_application()` на том же исключении
       взводит предохранитель до клика, и второй вызов отбит
  * S6 соседняя ветка `FAILED`: JS отказался кликать и сказал об этом прямо, а
       запись всё равно считает клик (НЕ закрыто №61 - см. примечание ниже)

Что показывает проба ПОСЛЕ правки №61 (тот же файл, тот же стенд):

  * S1/S2 `verdict=SUBMISSION_UNKNOWN`, `click_count=1`, `submit_count=1`,
       предохранитель взведён
  * S3 второй вызов - `BLOCKED`, браузер второго вызова не кликает
  * S4 без изменений (здоровый путь правка не тронула)
  * S5 без изменений (копия-близнец чинится отдельно, если Миша решит)
  * S6 без изменений

Почему S6 не закрыт в №61. Запись `FAILED`/`click_count=1` при отказе самого JS
дословно совпадает в обеих копиях (`hh_controlled_submit.py:175-186` и
`hh_submission.py:438-445`). Починка одной копии развела бы их - а расхождение
копий и было находкой №44. Это отдельное решение, не часть №61.

Запуск:
    .venv/Scripts/python.exe tools/ble001_click_exception_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Килл-свитч отправки (находка #19) читается на вызове, а не при импорте.
os.environ["SUBMIT_ALLOWED"] = "true"

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-click-exc-")
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
    _submitted_reviews,
    submit_application,
)
from ai_assistant.prefill_orchestrate import GroupCheck, OrchestrationReport
from ai_assistant.prefill_plan import PrefillPlan

MEASURED = [
    "ai_assistant/hh_controlled_submit.py",
    "ai_assistant/hh_submission.py",
    "ai_assistant/hh_human_submission.py",
]

VAC = "hh:136591579"


class Dom:
    """Поддельный браузер: клик считает по-настоящему, транспорт можно сломать.

    `break_click_response`:
        None    - транспорт здоров
        "before" - evaluate падает, НЕ выполнив JS (клика не было)
        "after"  - evaluate падает ПОСЛЕ того, как JS выполнил el.click()

    `click_refusal`: JS отказывается кликать и говорит об этом прямо
        (`{"ok": false, "reason": ...}`) - это ветка FAILED, а не исключение.
    """

    URL = "https://hh.ru/applicant/vacancy_response?vacancyId=136591579"

    def __init__(self, *, break_click_response: str | None = None, markers=(),
                 click_refusal: str | None = None):
        self.clicks = 0
        self.click_js_runs = 0
        self.break_click_response = break_click_response
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
            if self.break_click_response == "before":
                raise RuntimeError("CDP evaluate failed before the JS ran")
            if self.click_refusal is not None:
                # JS сам отказался кликать - `_SUBMIT_CLICK_JS` возвращает
                # ok:false до el.click(), значит клика не было.
                return json.dumps({"ok": False, "reason": self.click_refusal})
            self.clicks += 1
            if self.break_click_response == "after":
                raise RuntimeError("CDP evaluate response lost after the JS clicked")
            return json.dumps({"ok": True})
        raise RuntimeError(f"Dom: unknown expression: {expression[:80]}")


def _form() -> ApplicationForm:
    return ApplicationForm(
        source="hh", vacancy_stable_id=VAC,
        application_type=ApplicationType.screening_questions,
        questions=[
            ApplicationQuestion(id="hh__ctrl_task_384589146", label="Опыт?",
                                normalized_type=QuestionType.RADIO, required=False,
                                options=["Менее 3 лет", "5-7 лет"],
                                source=QuestionSource.SCREENING),
        ])


def _pkg() -> ApplicationPackage:
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
    return pkg


def _plan() -> PrefillPlan:
    return PrefillPlan(vacancy_stable_id=VAC, status="VALID")


def _orch() -> OrchestrationReport:
    return OrchestrationReport(
        vacancy_stable_id=VAC,
        verdict="VERIFIED", planned_operations=1, executed_operations=1,
        verified_operations=1, failed_operations=0, skipped_operations=0,
        group_checks=[
            GroupCheck(group_name="task_384589146", input_type="radio",
                       expected_checked=["Менее 3 лет"],
                       actual_checked=["Менее 3 лет"], ok=True),
        ])


def _approved_setup():
    form, plan, orch, pkg = _form(), _plan(), _orch(), _pkg()
    gate = build_review_gate(pkg, plan, orch, {"controls": []}, form)
    assert gate.status == GateStatus.READY_FOR_HUMAN_REVIEW, gate.block_reasons
    store = HumanReviewStore()
    rid = store.save(gate)
    store.mark_waiting_for_human(rid)
    assert store.approve_review(rid, gate.fingerprint)["ok"] is True
    assert confirm_human_submission(store, rid, gate.fingerprint, VAC)["ok"] is True
    return gate, plan, orch, pkg, store, rid


def _record(rep) -> dict:
    return {
        "verdict": rep.verdict,
        "click_count": rep.click_count,
        "submit_count": rep.submit_count,
        "successful_submit": rep.successful_submit,
        "failed_submit": rep.failed_submit,
        "stop_reason": rep.stop_reason,
    }


def _show(label: str, rep) -> None:
    rec = _record(rep)
    print(f"  {label}: verdict={rec['verdict']!r} click_count={rec['click_count']} "
          f"submit_count={rec['submit_count']} successful={rec['successful_submit']} "
          f"failed={rec['failed_submit']}")
    print(f"    stop_reason: {rec['stop_reason']!r}")


def _counters(rec: dict) -> tuple:
    """Вердикт и счётчики без свободного текста причины.

    Сравнивать записи побайтово нельзя: текст исключения в `stop_reason` разный
    по построению стенда. Значимо другое - совпадают ли вердикт и счётчики.
    """
    return (rec["verdict"], rec["click_count"], rec["submit_count"],
            rec["successful_submit"], rec["failed_submit"])


def _sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main() -> int:
    print("Проба №61: путь исключения при клике - запись и предохранитель")
    print("=" * 78)
    print("Измеряемые исходники:")
    for rel in MEASURED:
        print(f"  {rel}: {_sha256(rel)[:16]}...")
    print()

    # ---------------- S1: транспорт упал ДО клика ----------------
    print("S1. evaluate падает ДО выполнения JS (клика не было)")
    clear_all_submission_state()
    gate, plan, orch, pkg, store, rid = _approved_setup()
    dom = Dom(break_click_response="before")
    rep1 = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom.evaluate)
    _show("запись", rep1)
    print(f"    браузер: click_js_runs={dom.click_js_runs} clicks={dom.clicks}")
    print(f"    предохранитель взведён: {rid in _submitted_reviews}")
    armed_before = rid in _submitted_reviews
    rec_before = _record(rep1)
    print()

    # ---------------- S2: транспорт упал ПОСЛЕ клика ----------------
    print("S2. evaluate падает ПОСЛЕ того, как JS выполнил el.click()")
    clear_all_submission_state()
    gate, plan, orch, pkg, store, rid2 = _approved_setup()
    dom2 = Dom(break_click_response="after")
    rep2 = controlled_real_submit(store, rid2, gate.fingerprint, pkg, plan, orch, dom2.evaluate)
    _show("запись", rep2)
    print(f"    браузер: click_js_runs={dom2.click_js_runs} clicks={dom2.clicks} "
          f"<-- кнопка нажата")
    print(f"    предохранитель взведён: {rid2 in _submitted_reviews}")
    armed_after = rid2 in _submitted_reviews
    rec_after = _record(rep2)
    print(f"    вердикт и счётчики совпадают с S1: "
          f"{_counters(rec_before) == _counters(rec_after)}")
    print()

    # ---------------- S3: второй прямой вызов доходит до второго клика ----
    print("S3. МАТЕРИАЛЬНОЕ: второй вызов того же отзыва после S1")
    dom3 = Dom(markers=["Вы откликнулись"])
    second_verdict = "(второй вызов не состоялся)"
    second_clicks = -1
    try:
        rep3 = controlled_real_submit(store, rid, gate.fingerprint, pkg, plan, orch, dom3.evaluate)
        _show("второй вызов", rep3)
        second_verdict = rep3.verdict
        second_clicks = dom3.clicks
        print(f"    браузер второго вызова: clicks={dom3.clicks} "
              f"(первый стенд кликал {dom.clicks} раз)")
        print(f"    ВЕРДИКТ: {'ВТОРОЙ КЛИК ВЫПОЛНЕН' if dom3.clicks else 'второго клика нет'}")
    except Exception as exc:  # noqa: BLE001 - проба обязана напечатать диагноз, а не упасть
        print(f"    второй вызов бросил: {type(exc).__name__}: {exc}")
    print()

    # ---------------- S4: контроль - здоровый первый вызов ----------------
    print("S4. Контроль: здоровый первый вызов взводит предохранитель")
    clear_all_submission_state()
    gate, plan, orch, pkg, store, rid4 = _approved_setup()
    dom4 = Dom(markers=["Вы откликнулись"])
    rep4 = controlled_real_submit(store, rid4, gate.fingerprint, pkg, plan, orch, dom4.evaluate)
    _show("первый вызов", rep4)
    print(f"    предохранитель взведён: {rid4 in _submitted_reviews}")
    dom4b = Dom(markers=["Вы откликнулись"])
    rep4b = controlled_real_submit(store, rid4, gate.fingerprint, pkg, plan, orch, dom4b.evaluate)
    _show("второй вызов", rep4b)
    print(f"    браузер второго вызова: clicks={dom4b.clicks}")
    print()

    # ---------------- S5: контраст - копия-близнец ----------------
    print("S5. Контраст: submit_application() на том же исключении")
    clear_all_submission_state()
    gate, plan, orch, pkg, store, rid5 = _approved_setup()
    dom5 = Dom(break_click_response="after")
    rep5 = submit_application(store, rid5, gate.fingerprint, pkg, plan, orch, dom5.evaluate)
    print(f"  статус={rep5.status.value!r} click_count={rep5.click_count} "
          f"submit_count={rep5.submit_count} failed={rep5.failed_submit}")
    print(f"    reason: {rep5.reason!r}")
    print(f"    браузер: clicks={dom5.clicks} <-- кнопка нажата")
    print(f"    предохранитель взведён: {rid5 in _submitted_reviews}")
    dom5b = Dom(markers=["Вы откликнулись"])
    rep5b = submit_application(store, rid5, gate.fingerprint, pkg, plan, orch, dom5b.evaluate)
    print(f"  второй вызов: статус={rep5b.status.value!r} reason={rep5b.reason!r}")
    print(f"    браузер второго вызова: clicks={dom5b.clicks}")
    print()

    # ---------------- S6: соседняя ветка FAILED ----------------
    print("S6. Соседняя ветка FAILED: JS отказался кликать и сказал об этом прямо")
    clear_all_submission_state()
    gate, plan, orch, pkg, store, rid6 = _approved_setup()
    dom6 = Dom(click_refusal="submit button not found")
    rep6 = controlled_real_submit(store, rid6, gate.fingerprint, pkg, plan, orch, dom6.evaluate)
    _show("запись", rep6)
    print(f"    браузер: click_js_runs={dom6.click_js_runs} clicks={dom6.clicks} "
          f"<-- кнопку НЕ нажимали, JS вернул ok:false")
    print("    (эта ветка дословно совпадает с hh_submission.py:438-445, "
          "поэтому №61 её не трогает)")
    print()

    print("=" * 78)
    print("Свод:")
    print(f"  S1 (упало до JS): verdict={rec_before['verdict']!r} "
          f"click_count={rec_before['click_count']} submit_count={rec_before['submit_count']} "
          f"предохранитель={'взведён' if armed_before else 'НЕ взведён'}")
    print(f"  S2 (упало после клика, кнопка нажата): "
          f"verdict={rec_after['verdict']!r} click_count={rec_after['click_count']} "
          f"submit_count={rec_after['submit_count']} "
          f"предохранитель={'взведён' if armed_after else 'НЕ взведён'}")
    print(f"  S1 и S2 неразличимы по вердикту и счётчикам: "
          f"{_counters(rec_before) == _counters(rec_after)}")
    print(f"  S3 второй вызов после потерянного ответа: {second_verdict!r}, "
          f"кликов у второго стенда: {second_clicks}")
    print(f"  S6 отказ JS: verdict={rep6.verdict!r} click_count={rep6.click_count} "
          f"при кликах браузера {dom6.clicks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
