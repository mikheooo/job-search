"""Проба №57: что читается из application_packages.package_json, которую не разобрали.

Четвёртая сестра семьи №33 (очередь) / №55 (верификации) / №56 (ревью) - но
на этот раз сестра-источник, а не потребитель. package_json - это то, из чего
считается фингерпринт отклика, откуда берутся ответы и сопроводительное письмо,
и то, что проверяют три ворот отправки.

Аудитор читает application_packages только на существование строки
(`application_integrity.py:202`, `get_application_package`); колонку
package_json он не читает нигде. Замер: `grep -n "package_json|application_packages"
ai_assistant/application_integrity.py` - пусто.

Вопрос пробы: что видит каждый потребитель, когда строка пакета есть, но её
содержимое не разбирается - и что он видит, когда она разбирается как JSON, но
не проходит модель.

Проба измеряет ЧЕТЫРЕ разных способа прочитать одну и ту же битую колонку:

  * application_review.compute_review_fingerprint   (:31-34)   - молча {}
  * application_review.approve_review               (:411-415) - молча pass
  * browser_executor.prepare_application_in_browser (:1986-1998) - молча pkg=None,
        и дальше, если ревью одобрено, READY_FOR_REVIEW
  * hh_submission.execute_hh_submission             (:1227)    - без try, сырой raise

Адреса в списке выше - состояние ДО правки №57 (HEAD 9ae1987). После правки они
сдвинулись: `compute_review_fingerprint` не тронут (:23), синк переехал в
`_mark_package_valid` (`application_review.py:350`), чтение пакета в браузере -
`:1993-2010`, голое чтение в отправке - `hh_submission.py:1245`.

Плюс отдельно: три ворот требуют `validation_status == "VALID"`, а четвёртый
читатель (browser_executor:2227) считает одобрение заменой VALID. Одобрение
пишет VALID молчаливым синком, который не всегда выполняется.

Запуск:
    .venv/Scripts/python.exe tools/ble001_package_read_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-package-read-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant.application_prep import APPLICATION_PREP_VERSION, ApplicationPackage
from ai_assistant.application_queue import QueueItem, save_queue_item
from ai_assistant.application_review import (
    REVIEW_VERSION,
    approve_review,
    compute_review_fingerprint,
    get_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.browser_executor import (
    MockBrowserAdapter,
    prepare_application_in_browser,
)
from ai_assistant.schema import Vacancy

SID = "hh:57"
# Отдельный признак «строки нет вовсе» - см. _write_package(). Отличать его от
# NULL в колонке обязательно: это два разных чтения и два разных поведения.
_ABSENT = object()

# Что проба готова поймать и записать вердиктом. Слепой `except Exception` здесь
# означал бы, что и неожиданное исключение превратится в аккуратную строку
# отчёта - то есть сюрприз станет невидимым. Проба обязана его показать, поэтому
# список узкий: всё, что читатели реально бросают на битой строке.
MEASURED_ERRORS = (ValueError, TypeError, RuntimeError, KeyError, AttributeError)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_package(status: str = "NEEDS_REVIEW") -> dict:
    """Пакет, который ApplicationPackage принимает целиком."""
    return {
        "vacancy_id": SID,
        "vacancy_stable_id": SID,
        "resume_adaptation_needed": False,
        "resume_summary": "Python-разработчик, 8 лет",
        "tailored_skills": ["python", "postgresql"],
        "relevant_experience": ["платежи", "интеграции"],
        "cover_letter": "Здравствуйте! Готов обсудить вакансию.",
        "application_strategy": "apply",
        "warnings": [],
        "generator_version": APPLICATION_PREP_VERSION,
        "adaptation": {
            "target_title": "Старший разработчик Python",
            "professional_summary": "summary",
            "prioritized_skills": ["python"],
            "relevant_experience_points": ["exp"],
        },
        "application_type": "unknown",
        "form": None,
        "answers": [],
        "validation_status": status,
        "review_reasons": [],
    }


def _valid_review(status: str = "APPROVED") -> dict:
    return {
        "vacancy_stable_id": SID,
        "company": "TestCo",
        "title": "Старший разработчик Python",
        "source": "hh",
        "vacancy_url": "https://hh.ru/vacancy/57",
        "status": status,
        "cover_letter": "Здравствуйте! Готов обсудить вакансию.",
        "form_fingerprint": "a" * 64,
        "review_id": "rev_57",
        "created_at": _now(),
        "updated_at": _now(),
        "review_version": REVIEW_VERSION,
    }


def _shapes() -> list[tuple[str, str, str | None]]:
    """(id, что это за форма, что реально лежит в колонке package_json)."""
    valid = _valid_package()
    return [
        ("S1-valid", "нормальный пакет, NEEDS_REVIEW", json.dumps(valid)),
        (
            "S2-extra-key",
            "лишнее поле (ApplicationPackage: extra=forbid)",
            json.dumps({**valid, "screenshot_sha256": "deadbeef"}),
        ),
        (
            "S3-dropped-required",
            "нет обязательного поля cover_letter",
            json.dumps({k: v for k, v in valid.items() if k != "cover_letter"}),
        ),
        ("S4-empty-object", "пустой объект", "{}"),
        ("S5-null", "SQL NULL в колонке", None),
        ("S6-empty-string", "пустая строка", ""),
        ("S7-broken-json", "обрезанный JSON", '{"validation_status": "VALID"'),
        (
            "S8-wrong-type",
            "tailored_skills строкой, а не списком",
            json.dumps({**valid, "tailored_skills": "python"}),
        ),
        ("S9-json-list", "валидный JSON, но не объект", "[]"),
    ]


def _setup() -> None:
    db.init_db()
    vac = Vacancy(
        source="hh",
        source_job_id="57",
        title="Старший разработчик Python",
        company="TestCo",
        description="Python, PostgreSQL.",
        job_url="https://hh.ru/vacancy/57",
        location="Remote",
    )
    db.save_vacancy(vac)
    save_queue_item(QueueItem(
        canonical_id="canonical_57",
        representative_vacancy_stable_id=SID,
        vacancy_stable_id=SID,
        priority_score=90,
        match_score=90,
        deep_score=85,
        company="TestCo",
        title="Старший разработчик Python",
        source="hh",
        vacancy_url="https://hh.ru/vacancy/57",
        reasons=["high match"],
        warnings=[],
        rank=1,
        components={},
        application_strategy="apply",
    ))
    set_application_status(
        SID,
        ApplicationStatus.READY_TO_APPLY,
        company="TestCo",
        title="Старший разработчик Python",
        source="hh",
        vacancy_url="https://hh.ru/vacancy/57",
        match_score=90,
        deep_score=85,
    )


def _write_package(raw: str | None) -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_packages")
        if raw is _ABSENT:
            # Строки нет вовсе. Это НЕ то же самое, что колонка NULL, и разница
            # измерена: при отсутствии строки браузерная подготовка падает
            # `ValueError: Application package not found` (browser_executor:1983),
            # а строка с NULL раньше молча читалась как `{}`. Первая редакция
            # пробы подписывала NULL-строку как «нет строки» - подпись врала.
            conn.commit()
            return
        conn.execute(
            "INSERT INTO application_packages "
            "(vacancy_stable_id, generator_version, package_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (SID, APPLICATION_PREP_VERSION, raw, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _write_review(raw: str | None) -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_reviews")
        if raw is not None:
            conn.execute(
                "INSERT INTO application_reviews "
                "(vacancy_stable_id, review_json, status, note, created_at, updated_at, "
                "review_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (SID, raw, "APPROVED", None, _now(), _now(), REVIEW_VERSION),
            )
        conn.commit()
    finally:
        conn.close()


def _clear_reviews() -> None:
    _write_review(None)


def _package_status() -> str:
    row = db.get_application_package(SID)
    if not row:
        return "<нет строки>"
    raw = row[2]
    if raw is None:
        return "<SQL NULL>"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "<JSON не разобран>"
    if not isinstance(data, dict):
        return f"<{type(data).__name__}, не объект>"
    return str(data.get("validation_status", "<нет ключа validation_status>"))


def _model_verdict(raw: str | None) -> str:
    """Читает ли колонку модель ApplicationPackage (её required-поля).

    Колонка этой моделью НЕ читается нигде: все потребители делают
    json.loads() и .get(). Колонка оставлена в таблице, чтобы было видно,
    почему определение «читаемости» взято от читателей, а не от модели:
    строки вида {"cover_letter": ..., "validation_status": "VALID"} модель
    отвергает, а весь проект их читает.
    """
    try:
        ApplicationPackage.model_validate_json(raw or "{}")
    except MEASURED_ERRORS as exc:
        return f"нет ({type(exc).__name__})"
    return "да"


def _unreadable_verdict() -> str:
    """Итоговое определение: что скажет package_record_is_unreadable()."""
    from ai_assistant.application_prep import package_record_is_unreadable

    return "НЕЧИТАЕМА" if package_record_is_unreadable(SID) else "читаема"


def _load_verdict(raw: str | None) -> str:
    try:
        data = json.loads(raw) if raw else None
    except MEASURED_ERRORS as exc:
        return f"raise {type(exc).__name__}"
    if data is None:
        return "None"
    return type(data).__name__


# --------------------------------------------------------------------------
# Секция 3: настоящий прогон prepare_application_in_browser
# --------------------------------------------------------------------------
def _browser_status(
    package_raw: str | None,
    review_status: str | None,
    fields: list[str] | None = None,
) -> tuple[str, str]:
    """(статус браузерной сессии, предупреждения). Гоняет настоящую функцию.

    `fields` - что «нашёл» мок на странице. Это рычаг: если среди полей нет
    resume/cover_letter, то `fields_skipped` остаётся пустым, и ветку
    READY_FOR_REVIEW решает ровно выражение is_valid (browser_executor:2227).
    Иначе готовность гасит пропущенное поле, и выражение не проверяется.
    """
    _write_package(package_raw)
    _clear_reviews()
    if review_status is not None:
        _write_review(json.dumps(_valid_review(review_status)))
    # Сессия мешает идемпотентности между прогонами - гасим её.
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM browser_preparations")
        conn.commit()
    finally:
        conn.close()
    mock = MockBrowserAdapter(simulate={
        "page_title": "Test Vacancy",
        "final_url": "https://hh.ru/vacancy/57",
        "fields": fields if fields is not None else [
            "name", "email", "resume", "cover_letter"],
        "apply_button": True,
    })
    try:
        res = prepare_application_in_browser(SID, adapter=mock)
    except MEASURED_ERRORS as exc:
        return (f"raise {type(exc).__name__}: {exc}", "")
    status = res.status.value if hasattr(res.status, "value") else str(res.status)
    # `skipped` печатается ПЕРВЫМ и не обрезается: секция 3a обязана показывать
    # не только итог, но и то, что его погасило. Замер 2026-09-20: с resume и
    # cover_letter на странице нечитаемый пакет давал FORM_DETECTED не из-за
    # пакета, а потому что пропущенные поля гасили готовность раньше, чем
    # вопрос о пакете вообще задавался. Без этого столбца «гипотеза
    # опровергнута» неотличимо от «условие не выполнялось».
    skipped = list(res.fields_skipped or [])
    return (status, f"skipped={skipped} | " + " | ".join(res.warnings or []))


# --------------------------------------------------------------------------
# Секция 4: аудитор
# --------------------------------------------------------------------------
def _audit_codes() -> list[str]:
    from ai_assistant.application_integrity import run_integrity_audit

    return sorted(
        i.code for i in run_integrity_audit(scope="full").issues
        if i.vacancy_stable_id == SID
    )


def _write_verification(raw: str | None) -> None:
    """Соседняя таблица, у которой проверка ЕСТЬ - чтобы асимметрия была видна
    на одном экране. Мерить надо по одной таблице за раз: аудитор не подписывает,
    из какой таблицы пришла находка."""
    from ai_assistant.submission_verifier import VERIFICATION_VERSION

    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM submission_verifications")
        if raw is not None:
            conn.execute(
                "INSERT INTO submission_verifications "
                "(vacancy_stable_id, submission_id, verification_version, "
                "verification_status, verification_json, verified_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (SID, "sub_57", VERIFICATION_VERSION, "VERIFIED", raw,
                 _now(), _now(), _now()),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Секция 5: расхождение читателей validation_status
# --------------------------------------------------------------------------
class _StubReviewStore:
    def __init__(self, entry):
        self._entry = entry

    def get(self, review_id):
        return self._entry


def _preflight(package_status: str):
    from ai_assistant import hh_submission as hs
    from ai_assistant.hh_submission import clear_submitted_reviews, preflight_submission

    clear_submitted_reviews()
    url = "https://hh.ru/applicant/vacancy_response?vacancyId=57"

    def evaluate(expression: str) -> str:
        if expression == hs._URL_JS:
            return json.dumps({"url": url})
        if expression == hs._SUBMIT_BTN_JS:
            return json.dumps({"found": True, "disabled": False, "tag": "BUTTON"})
        raise RuntimeError(f"unexpected expression: {expression[:60]}")

    fingerprint = "f" * 64
    entry = {"state": "HUMAN_APPROVED", "fingerprint": fingerprint,
             "review_id": "rid-1", "gate": None}
    package = SimpleNamespace(vacancy_stable_id=SID, validation_status=package_status)
    plan = SimpleNamespace(status="VALID", unresolved=[])
    orch = SimpleNamespace(verdict="VERIFIED", failed_operations=0,
                           skipped_operations=0, errors=[])
    try:
        rep = preflight_submission(
            _StubReviewStore(entry), "rid-1", fingerprint, package, plan, orch, evaluate)
    except MEASURED_ERRORS as exc:
        return f"raise {type(exc).__name__}"
    return f"{rep.status.value}: {rep.reason}"


def _auto_apply(package_status: str) -> str:
    """Вердикт run_auto_apply на этой validation_status.

    Для NEEDS_REVIEW и INVALID гейт пакета срабатывает раньше детерминированного
    плана, и вердикт получен целиком. Для VALID гейт пропускает дальше, в
    build_prefill_plan, которому нужен настоящий пакет с формой, - поэтому эта
    строка читается как «не прогнано», а не как «прошло». Гейт под вопросом
    лежит ДО этого места, так что расхождение измерено и без неё.
    """
    from ai_assistant.auto_apply_modes import run_auto_apply

    pkg = SimpleNamespace(
        vacancy_stable_id=SID,
        validation_status=package_status,
        cover_letter="Здравствуйте!",
        answers=[],
        form=None,
        review_reasons=[],
        application_type="unknown",
    )
    form = SimpleNamespace(questions=[], extraction_meta={"url": "https://hh.ru/vacancy/57"})
    try:
        rep = run_auto_apply(pkg, evaluate_fn=lambda e: "{}", snapshot={}, form=form)
    except MEASURED_ERRORS as exc:
        return f"не прогнано до гейта: {type(exc).__name__}: {exc}"
    return f"{rep.verdict}: {rep.stop_reason}"


def main() -> int:
    print("=" * 108)
    print("Проба №57: application_packages.package_json, которую не разобрали")
    print("=" * 108)
    _setup()
    print(f"вакансия: {SID}   tracking: READY_TO_APPLY   БД: {config.DB_FILE}")
    print()

    empty_fp = compute_review_fingerprint(SID, {})
    valid_fp = compute_review_fingerprint(SID, _valid_package())

    # ---- Секция 1: форма колонки vs. как её видят читатели ----
    print("1. ОДНА КОЛОНКА, ЧЕТЫРЕ ЧТЕНИЯ")
    header = (
        f"{'форма':<20} {'json.loads':<16} {'модель':<14} {'итог':<10} "
        f"{'fp == fp({})':<13} {'fp == fp(пакет)':<16}"
    )
    print(header)
    print("-" * len(header))
    for shape_id, _desc, raw in _shapes():
        _write_package(raw)
        row = db.get_application_package(SID)
        fp = compute_review_fingerprint(SID, row)
        print(
            f"{shape_id:<20} {_load_verdict(raw):<16} {_model_verdict(raw):<14} "
            f"{_unreadable_verdict():<10} {fp == empty_fp!s:<13} "
            f"{fp == valid_fp!s:<16}"
        )

    # ---- Секция 2: одобрение на битом пакете ----
    print()
    print("2. approve_review НА БИТОМ ПАКЕТЕ (ревью PENDING_REVIEW, пакет не разбирается)")
    print("-" * 108)
    _write_package('{"validation_status": "VALID"')
    _write_review(json.dumps(_valid_review("PENDING_REVIEW")))
    try:
        approve_review(SID)
        raised = "нет"
    except MEASURED_ERRORS as exc:
        raised = f"{type(exc).__name__}: {exc}"
    rev = get_application_review(SID)
    print(f"   исключение:              {raised}")
    print(f"   статус ревью после:      {rev.status.value if rev else None}")
    print(f"   фингерпринт ревью:       {(rev.form_fingerprint or '')[:16]}...")
    print(f"   фингерпринт пустого:     {empty_fp[:16]}...")
    print(f"   совпал с пустым:         {rev is not None and rev.form_fingerprint == empty_fp}")
    print(f"   validation_status:       {_package_status()}")

    # ---- Секция 3: настоящий прогон браузерной подготовки ----
    print()
    print("3. prepare_application_in_browser (настоящий прогон, MockBrowserAdapter)")
    print("-" * 108)
    print("3a. на странице есть resume и cover_letter (их заполнение зависит от пакета)")
    print(f"{'пакет':<18} {'ревью':<10} {'статус':<18} предупреждения")
    for label, raw, rev_status in [
        ("валидный/VALID", json.dumps(_valid_package("VALID")), None),
        ("валидный/NEEDS", json.dumps(_valid_package("NEEDS_REVIEW")), "APPROVED"),
        ("валидный/NEEDS", json.dumps(_valid_package("NEEDS_REVIEW")), None),
        ("битый JSON", '{"validation_status": "VALID"', "APPROVED"),
        ("битый JSON", '{"validation_status": "VALID"', None),
        ("пустой объект", "{}", "APPROVED"),
    ]:
        status, warns = _browser_status(raw, rev_status)
        rev_s = rev_status or "<нет>"
        print(f"{label:<18} {rev_s:<10} {status:<18} {warns[:64]}")

    print()
    print("3b. контроль: на странице только name и email, пропускать нечего")
    print("    (тогда ветку READY_FOR_REVIEW решает ровно выражение is_valid)")
    print(f"{'пакет':<18} {'ревью':<10} {'статус':<18} предупреждения")
    for label, raw, rev_status in [
        ("валидный/VALID", json.dumps(_valid_package("VALID")), None),
        ("валидный/NEEDS", json.dumps(_valid_package("NEEDS_REVIEW")), "APPROVED"),
        ("валидный/NEEDS", json.dumps(_valid_package("NEEDS_REVIEW")), None),
        ("битый JSON", '{"validation_status": "VALID"', "APPROVED"),
        ("битый JSON", '{"validation_status": "VALID"', None),
        ("пустой объект", "{}", "APPROVED"),
        ("NULL-колонка", None, "APPROVED"),
        ("нет строки", _ABSENT, "APPROVED"),
    ]:
        status, warns = _browser_status(raw, rev_status, fields=["name", "email"])
        rev_s = rev_status or "<нет>"
        print(f"{label:<18} {rev_s:<10} {status:<18} {warns[:64]}")

    # ---- Секция 4: аудитор ----
    print()
    print("4. АУДИТОР: что он говорит про битую колонку")
    print("-" * 108)
    _clear_reviews()
    _write_verification(None)
    _write_package('{"validation_status": "VALID"')
    print(f"   пакет битый, верификаций нет:        {_audit_codes() or '<тихо>'}")
    _write_package(json.dumps(_valid_package("VALID")))
    _write_verification('{"verification_status": "VERIFIED"')
    print(f"   пакет целый, верификация битая:      {_audit_codes() or '<тихо>'}")
    _write_verification(None)
    _write_package(json.dumps({**_valid_package(), "screenshot_sha256": "x"}))
    print(f"   пакет с лишним ключом (extra=forbid): {_audit_codes() or '<тихо>'}")
    _write_package(json.dumps(_valid_package("VALID")))

    # ---- Секция 5: расхождение читателей validation_status ----
    print()
    print("5. validation_status: ТРИ ВОРОТ ТРЕБУЮТ VALID, ЧЕТВЁРТЫЙ ЧИТАТЕЛЬ ПРОЩАЕТ")
    print("-" * 108)
    for status in ("VALID", "NEEDS_REVIEW", "INVALID"):
        print(f"   validation_status={status}")
        print(f"      preflight_submission: {_preflight(status)}")
        print(f"      run_auto_apply:       {_auto_apply(status)}")
    print("      browser_executor:2227: is_valid = (validation_status == 'VALID') or (is_approved and validation_status != 'INVALID')")
    print("                            -> при APPROVED и NEEDS_REVIEW даёт True (замер в секции 3)")

    # ---- Секция 6: молчаливый синк не выполняется на повторном одобрении ----
    print()
    print("6. ПОВТОРНОЕ ОДОБРЕНИЕ: СИНК VALID НЕ ВЫПОЛНЯЕТСЯ")
    print("-" * 108)
    _write_package(json.dumps(_valid_package("NEEDS_REVIEW")))
    _write_review(json.dumps(_valid_review("APPROVED")))
    print(f"   до повторного одобрения:  {_package_status()}")
    try:
        approve_review(SID)
        print("   approve_review:           без исключения (идемпотентная ветка)")
    except MEASURED_ERRORS as exc:
        print(f"   approve_review:           {type(exc).__name__}: {exc}")
    print(f"   после повторного:         {_package_status()}")
    print(f"   preflight на этом:        {_preflight(_package_status())}")

    print()
    print("=" * 108)
    print("ЧЕГО ПРОБА НЕ МЕРИЛА")
    print("=" * 108)
    print("   * hh_submission.py:1245 и hh_submit_policy.py:206 - не защищённый")
    print("     json.loads. Это чтение исходника, не прогон: обе точки лежат за")
    print("     живой браузерной сессией, которой у пробы нет. (До правки №57")
    print("     адреса были :1227 и :195.)")
    print("   * application_review_gate.py:148 - то же условие != \"VALID\", что у")
    print("     preflight. Чтение исходника, чтобы не строить третий стенд.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
