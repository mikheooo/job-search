"""Проба №58: что записывает create_application_review() из нечитаемого пакета.

Пятое чтение той же колонки `application_packages.package_json` - и первое, которое
ПИШЕТ. №57 закрыла четыре чтения пакета; это чтение строит из пакета саму запись
ревью и кладёт в неё `form_fingerprint` как удостоверение того, что человек
одобряет. Вопрос пробы: что попадает в `application_reviews`, когда строка пакета
есть, но её содержимое не разбирается, - и что об этой записи говорит собственный
инструмент оператора.

Замеры (состояние ДО правки №58, HEAD 2df4602):

  * `_get_required_data` молча `pkg_json = {}`       (application_review.py:296-301)
  * review записывается с `form_fingerprint == fp({})` и пустым письмом (:345)
  * `cli review_list` печатает `FP=YES`, а `--missing-fingerprint` эту строку прячет
  * гейт 3 (`_check_fingerprint_gate`) при обоих `fp({})` объявляет совпадение

Отдельно измерено и НЕ является дефектом: `fp({})` достижим законной строкой.
Канонический payload - только `{answers, cover_letter, vacancy_stable_id}`, поэтому
читаемый пакет без письма и без ответов хешируется в то же значение. Значит
«фингерпринт пустой» - не признак порчи, и защита обязана судить СТРОКУ, а не
значение (`application_prep.package_record_is_unreadable`).

Запуск:
    .venv/Scripts/python.exe tools/ble001_review_fingerprint_probe.py

Ничего не пишет в рабочие файлы: config.DB_FILE подменяется на временный.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ai_assistant import config

TMP = tempfile.mkdtemp(prefix="ble001-review-fp-")
config.DB_FILE = os.path.join(TMP, "state.db")

from ai_assistant import db
from ai_assistant.application_prep import APPLICATION_PREP_VERSION
from ai_assistant.application_queue import QueueItem, save_queue_item
from ai_assistant.application_review import (
    REVIEW_VERSION,
    ApplicationReview,
    ReviewStatus,
    compute_review_fingerprint,
    create_application_review,
    get_application_review,
    list_application_reviews,
    save_application_review,
)
from ai_assistant.application_tracking import ApplicationStatus, set_application_status
from ai_assistant.browser_executor import BrowserApplicationSession, BrowserStatus
from ai_assistant.browser_executor import save_browser_session as save_browser
from ai_assistant.hh_submission import HHSubmissionGates
from ai_assistant.schema import Vacancy

SID = "hh:58"

# Что проба готова поймать и записать вердиктом. Слепой `except Exception` сделал
# бы невидимым неожиданное исключение - то есть сюрприз превратился бы в аккуратную
# строку отчёта. Список узкий: всё, что читатели реально бросают на битой строке.
MEASURED_ERRORS = (ValueError, TypeError, RuntimeError, KeyError, AttributeError)

CREATED = "CREATE-OK"
REFUSED = "REFUSED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _package(status: str = "NEEDS_REVIEW") -> dict:
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


def _shapes() -> list[tuple[str, str, str | None]]:
    """(id, что это за форма, что реально лежит в колонке package_json)."""
    valid = _package()
    return [
        ("S1-valid", "нормальный пакет", json.dumps(valid)),
        (
            "S2-extra-key",
            "лишнее поле (для читателей безразлично)",
            json.dumps({**valid, "screenshot_sha256": "deadbeef"}),
        ),
        (
            "S3-dropped-required",
            "нет cover_letter - читаемо, но fp == fp({})",
            json.dumps({k: v for k, v in valid.items() if k != "cover_letter"}),
        ),
        ("S4-empty-object", "пустой объект", "{}"),
        ("S5-null", "SQL NULL в колонке", None),
        ("S6-empty-string", "пустая строка", ""),
        ("S7-broken-json", "обрезанный JSON", '{"validation_status": "VALID"'),
        ("S8-json-list", "валидный JSON, но не список", "[]"),
        ("S9-absent", "строки нет вовсе", "absent"),
    ]


def _setup() -> None:
    db.init_db()
    db.save_vacancy(Vacancy(
        source="hh", source_job_id="58", title="Старший разработчик Python",
        company="TestCo", description="Python, PostgreSQL.",
        job_url="https://hh.ru/vacancy/58", location="Remote",
    ))
    save_queue_item(QueueItem(
        canonical_id="canonical_58",
        representative_vacancy_stable_id=SID,
        vacancy_stable_id=SID,
        priority_score=90, match_score=90, deep_score=85,
        company="TestCo", title="Старший разработчик Python", source="hh",
        vacancy_url="https://hh.ru/vacancy/58",
        reasons=["high match"], warnings=[], rank=1, components={},
        application_strategy="apply",
    ))
    set_application_status(
        SID, ApplicationStatus.READY_TO_APPLY, company="TestCo",
        title="Старший разработчик Python", source="hh",
        vacancy_url="https://hh.ru/vacancy/58", match_score=90, deep_score=85,
    )
    save_browser(BrowserApplicationSession(
        vacancy_stable_id=SID, url="https://hh.ru/vacancy/58",
        status=BrowserStatus.READY_FOR_REVIEW, created_at=_now(), updated_at=_now(),
        form_detected=True,
    ))


def _write_package(raw: str | None) -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_packages")
        if raw == "absent":
            # Строки нет вовсе - это НЕ то же самое, что NULL в колонке.
            conn.commit()
            return
        conn.execute(
            "INSERT INTO application_packages"
            " (vacancy_stable_id, generator_version, package_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (SID, APPLICATION_PREP_VERSION, raw, "2026-09-20T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_reviews() -> None:
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute("DELETE FROM application_reviews")
        conn.commit()
    finally:
        conn.close()


def _write_review_row(fp: str | None, letter: str | None) -> None:
    """Записать ревью так, как его записала бы до-правочная труба."""
    save_application_review(ApplicationReview(
        vacancy_stable_id=SID, company="TestCo",
        title="Старший разработчик Python", source="hh",
        vacancy_url="https://hh.ru/vacancy/58",
        status=ReviewStatus.PENDING_REVIEW,
        cover_letter=letter, form_fingerprint=fp or "",
        created_at="2026-09-20T00:00:00", updated_at="2026-09-20T00:00:00",
        review_version=REVIEW_VERSION,
    ))


def _stored():
    return get_application_review(SID)


def _try_create():
    """(вердикт, текст исключения, запись после попытки)."""
    try:
        rev = create_application_review(SID)
    except MEASURED_ERRORS as exc:
        return REFUSED, f"{exc.__class__.__name__}: {exc}", _stored()
    return CREATED, "", rev


def section_1_create() -> None:
    print("=" * 78)
    print("1. create_application_review() на каждой форме строки")
    print("=" * 78)
    print(f"{'форма':18} | {'итог':9} | {'fp == fp({})':12} | письмо | что записано")
    print("-" * 78)
    empty = compute_review_fingerprint(SID, {})
    for name, _desc, raw in _shapes():
        _clear_reviews()
        _write_package(raw)
        verdict, message, rev = _try_create()
        stored = rev.form_fingerprint if rev is not None else None
        same = "ДА" if stored == empty else ("нет" if stored else "-")
        letter = "-"
        if rev is not None:
            letter = "пусто" if not rev.cover_letter else f"{len(rev.cover_letter)} симв."
        note = message or ("запись есть" if rev is not None else "записи нет")
        print(f"{name:18} | {verdict:9} | {same:12} | {letter:6} | {note[:34]}")
    print()
    print("Измерено на HEAD 2df4602 (см. .p58_probe_before.log): S4, S5, S6, S7")
    print("давали CREATE-OK с fp == fp({}) и пустым письмом; S8 падал")
    print("AttributeError ('list' object has no attribute 'get') - то есть не")
    print("отказом, а трейсбеком; S9 отказывал ValueError 'Application package")
    print("not found' и до правки - это другое чтение, не порча.")
    print()


def section_2_cli() -> None:
    print("=" * 78)
    print("2. Детектор оператора: cli review_list по такой записи")
    print("=" * 78)
    from ai_assistant import cli

    _write_package('{"validation_status": "VALID"')
    _write_review_row(compute_review_fingerprint(SID, {}), None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.review_list()
    lines = [ln for ln in buf.getvalue().splitlines() if SID.split(":")[0] in ln or "|" in ln]
    print("review_list() печатает:")
    for ln in lines[-4:]:
        print("   " + ln)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.review_list(missing_fingerprint=True)
    kept = [ln for ln in buf.getvalue().splitlines() if "TestCo" in ln]
    print()
    print(f"review_list(missing_fingerprint=True) показывает строк с TestCo: {len(kept)}")
    print("Замер (одинаковый до и после правки №58): FP=YES в первом выводе и НОЛЬ")
    print("строк во втором - то есть фильтр, существующий ради поиска протухшего")
    print("фингерпринта, прячет ровно ту строку, ради которой он есть.")
    print()
    print("Правка №58 это НЕ закрывает: она не даёт записать такую строку заново,")
    print("а записанная раньше читается так же. Чинится в cli.review_list - и там")
    print("же лежит долг по ссылкам отчёта (чужая незакоммиченная правка cli.py).")
    print()


def section_3_gate3() -> None:
    print("=" * 78)
    print("3. Гейт 3: сверка отпечатка страницы с отпечатком ревью")
    print("=" * 78)
    _clear_reviews()
    empty = compute_review_fingerprint(SID, {})
    for name, _desc, raw in _shapes():
        if raw == "absent":
            # У страницы нет строки-пакета, а не битая строка: подставлять сюда
            # маркер отсутствия нельзя, он сам не разберётся как JSON и даст
            # ровно тот же fp({}) - то есть артефакт замера, а не находку.
            print(f"   {name:18} - пропущено: это отсутствие строки, не порча")
            continue
        _write_package(raw)
        res = HHSubmissionGates._check_fingerprint_gate(
            SID, expected_fp=empty, form_snapshot={"package": raw},
        )
        verdict = "PASS (None)" if res is None else f"FAIL: {res.failed_gate}"
        print(f"   {name:18} expected=fp({{}}) -> {verdict}")
    print()
    print("Обе стороны деградировали в одну константу, и гейт объявил совпадение.")
    print("В бою до этого места не дойти, если ревью создавалось после #57:")
    print("одобрение нечитаемого пакета отказывает. Достижимо для строк,")
    print("одобренных ДО правки.")
    print()


def section_4_letterless() -> None:
    print("=" * 78)
    print("4. Контроль: fp({}) достижим законной строкой")
    print("=" * 78)
    empty = compute_review_fingerprint(SID, {})
    _clear_reviews()
    _write_package(json.dumps({"vacancy_stable_id": SID}))
    verdict, message, rev = _try_create()
    print(f"   читаемый пакет без письма и ответов: {verdict} {message}")
    if rev is not None:
        print(f"   cover_letter = {rev.cover_letter!r}")
        print(f"   form_fingerprint == fp({{}}) -> {rev.form_fingerprint == empty}")
    print()
    print("Значит «отпечаток пустой» - не признак порчи, и детектор, построенный")
    print("на этом сравнении, флагует каждую заявку без письма. Судят СТРОКУ.")
    print()


def section_5_review_list_skips() -> None:
    print("=" * 78)
    print("5. Соседнее чтение: битая запись ревью в списке")
    print("=" * 78)
    _clear_reviews()
    _write_package(json.dumps(_package()))
    conn = sqlite3.connect(config.DB_FILE)
    try:
        conn.execute(
            "INSERT INTO application_reviews"
            " (vacancy_stable_id, review_json, status, note, created_at, updated_at,"
            "  review_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (SID, '{"broken": ', "PENDING_REVIEW", None,
             "2026-09-20T00:00:00", "2026-09-20T00:00:00", REVIEW_VERSION),
        )
        conn.commit()
    finally:
        conn.close()

    listed = list_application_reviews()
    from ai_assistant.application_integrity import run_integrity_audit

    report = run_integrity_audit(scope="full")
    codes = {i.code for i in report.issues if i.vacancy_stable_id == SID}
    print(f"   list_application_reviews() -> {len(listed)} строк (битая пропала молча)")
    print(f"   аудит называет: {sorted(codes)}")
    print()
    print("Пропажа из списка закрыта тем, что строка НАЗВАНА аудитом (#56), -")
    print("это защита, а не дыра. Записано, чтобы разница была видна.")
    print()


def main() -> int:
    _setup()
    section_1_create()
    section_2_cli()
    section_3_gate3()
    section_4_letterless()
    section_5_review_list_skips()
    print("=" * 78)
    print("Итог: форма строки решает, попадёт ли в application_reviews запись,")
    print("удостоверяющая пустой пакет. Проба печатает ОДНО состояние кода за")
    print("раз: два её запуска (файл из HEAD и файл после правки) и есть замер.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
