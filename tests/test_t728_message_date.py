"""T7.28 — дата сообщения в карточке переписки HH.

Зачем тест написан до правки. Замер T7.28 показал: `sent_at` пуст в 37 из 38
событий (97 %), и дата лежит в `rawLines[1]` карточки, где её **выфильтровывают**
как мусор. Фикс выглядит настолько очевидным («прочитать строку 1»), что его
легко сделать неправильно сразу в трёх местах:

1. **Подставить сырую строку в fingerprint.** Это осиротило бы все 10 уже
   обработанных сообщений (проверено: сохранённые fingerprint'ы совпадают с
   вычисленными при `sent_at=None` — 10 из 10). И сломало бы дедуп даже на новых:
   «вчера» на следующий день читается как «позавчера», fingerprint меняется.
2. **Не отличить дату от работодателя.** `rawLines[2]` — работодатель, но при
   другом DOM он окажется на месте даты. Парсер обязан **отказаться** (None),
   а не съесть похожую строку.
3. **Зашитая пауза вместо ожидания отрисовки.** Список гидрируется после
   `complete` (T7.26.2: 20 → 50 за ~3 с).

Поэтому здесь проверяются свойства, а не реализация: имена двух форматов,
отказ на неоднозначном вводе, и — главное — **неизменность fingerprint**.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Реальные карточки: замер 2026-09-30, tools/.t728/card_lines_20260930.txt
# Хранится как (conversation_id, rawLines) — ровно то, что отдаёт страница.
# Взято 12 из 12, без отбора: если отобрать «удобные», тест перестанет ловить
# именно те формы, которые ломали чтение.
# ---------------------------------------------------------------------------
LIVE_CARDS: list[tuple[str, list[str]]] = [
    ("5665125042", [
        "Специалист технической поддержки / IT-ассистент (IT Support Specialist Bitrix24 / GetCourse)",
        "вчера", "Кадровое агентство Dream Team", "Отказ", "1"]),
    ("5662692999", [
        "Технический специалист CRM-отдела",
        "вчера", "Академия Йоги", "Отказ", "1"]),
    ("5662603940", [
        "Инженер технической поддержки",
        "пн", "EvaTeam", "Отказ", "1"]),
    ("5664349328", [
        "Битриксолог (Middle, Bitrix24, Удаленно)",
        "пн", "Каналсервис", "Отклик на\xa0вакансию"]),
    ("5664348392", [
        "Администратор Bitrix (Битрикс24)",
        "пн", "АФЛТ-Системс", "Отклик на\xa0вакансию"]),
    ("5662695512", [
        "Помощник администратора Bitrix24 / Специалист поддержки CRM",
        "вс", "Юридическая Компания Опора", "Отказ"]),
    ("5566499616", [
        "Инженер интеграций / n8n-разработчик (автоматизация бухгалтерии + ИИ)",
        "ср", "ТопБухгалтер", "Отказ"]),
    ("5643197064", [
        "Чат с компанией",
        "20.09", "Скайтек",
        "Михаил, здравствуйте! Если вдруг в хх пришел отказ - не волнуйтесь, это пакетн"]),
    ("5566493500", [
        "AI Automation Engineer (Инженер по AI-автоматизации)",
        "20.09", "Скайтек", "Отказ"]),
    ("5566479975", [
        "AI Engineer",
        "15.09", "Банк ПСБ, Бизнес и инфраструктура",
        "Михаил Кириллович, здравствуйте! Рассмотрим ваше резюме. Если навыки и\xa0опыт по"]),
    ("5586277073", [
        "Senior Python Developer",
        "15.09", "Филиал компании Листа Ренталс Лимитед", "Отказ"]),
    ("5578155610", [
        "Computer Vision engineer",
        "12.09", "ОнТаргет ЛАБС", "Отказ"]),
]

#: Все 12 живых дат — строка `rawLines[1]` каждой карточки, без исключений.
LIVE_DATE_LINES = [lines[1] for _, lines in LIVE_CARDS]


def _import_parser():
    """Парсер даты — из продакшн-модуля, а не из теста."""
    from ai_assistant.hh_message_reply import parse_card_date
    return parse_card_date


# ---------------------------------------------------------------------------
# 1. Формат `ДД.ММ` — абсолютная дата, самый ценный случай
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected_month_day",
    [
        ("20.09", (9, 20)),
        ("15.09", (9, 15)),
        ("12.09", (9, 12)),
        ("01.10", (10, 1)),
        ("31.12", (12, 31)),
        ("5.09", (9, 5)),    # без ведущего нуля — встречается на HH
    ],
)
def test_absolute_day_month_is_parsed(raw, expected_month_day):
    """`ДД.ММ` — дата, а не текст. Год берётся текущий, месяц и день — из строки."""
    parse_card_date = _import_parser()
    got = parse_card_date(raw, today="2026-09-30")
    assert got is not None, f"{raw!r} не разобрана"
    dt = got if hasattr(got, "month") else __import__("datetime").datetime.fromisoformat(got)
    assert (dt.month, dt.day) == expected_month_day, f"{raw!r} -> {got}"


def test_absolute_date_in_december_reads_as_this_year_not_next():
    """Граница года: 31.12, прочитанное 30 сентября, — это ещё не наступивший
    декабрь ТЕКУЩЕГО года, а не прошлогодний. Ошибка здесь тихо сдвинула бы
    дату на год и сделала бы свежее сообщение старым."""
    parse_card_date = _import_parser()
    got = parse_card_date("31.12", today="2026-09-30")
    assert got is not None
    dt = got if hasattr(got, "month") else __import__("datetime").datetime.fromisoformat(got)
    assert dt.year == 2026, f"31.12 при today=2026-09-30 дало год {dt.year}"
    assert (dt.month, dt.day) == (12, 31)


# ---------------------------------------------------------------------------
# 2. Относительный формат: день недели и «вчера»
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["вчера"])
def test_yesterday_resolves_to_the_previous_day(raw):
    """«вчера» — это ровно `today - 1`, без угадывания часа."""
    parse_card_date = _import_parser()
    got = parse_card_date(raw, today="2026-09-30")  # среда
    assert got is not None, f"{raw!r} не разобрана"
    dt = got if hasattr(got, "month") else __import__("datetime").datetime.fromisoformat(got)
    assert (dt.year, dt.month, dt.day) == (2026, 9, 29), f"{raw!r} -> {got}"


@pytest.mark.parametrize(
    "raw,expect_weekday",
    [
        ("пн", 0), ("вт", 1), ("ср", 2), ("чт", 3), ("пт", 4), ("сб", 5), ("вс", 6),
    ],
)
def test_weekday_abbreviation_resolves_to_the_past(raw, expect_weekday):
    """`пн`..`вс` — это БЛИЖАЙШИЙ ПРОШЛЫЙ такой день, а не следующий.

    Ключевое свойство: результат никогда не в будущем. Если взять ближайший
    вперёд, сообщение от понедельника, прочитанное во вторник, получит дату
    следующего понедельника — в будущем, и станет «свежее, чем есть»."""
    parse_card_date = _import_parser()
    today = "2026-09-30"  # среда
    got = parse_card_date(raw, today=today)
    assert got is not None, f"{raw!r} не разобрана"
    dt = got if hasattr(got, "month") else __import__("datetime").datetime.fromisoformat(got)
    assert dt.weekday() == expect_weekday, f"{raw!r} -> weekday {dt.weekday()}"
    assert dt.date().isoformat() <= today, f"{raw!r} -> {dt.date()} в будущем (today={today})"


def test_today_weekday_means_today_not_a_week_ago():
    """Сокращение текущего дня недели — это СЕГОДНЯ, а не семь дней назад.

    «ср», прочитанное в среду, обязано дать среду, а не прошлую: брать
    «строго меньше today» здесь — самая естественная ошибка."""
    parse_card_date = _import_parser()
    got = parse_card_date("ср", today="2026-09-30")  # среда
    assert got is not None
    dt = got if hasattr(got, "month") else __import__("datetime").datetime.fromisoformat(got)
    assert dt.date().isoformat() == "2026-09-30", f"'ср' в среду -> {dt.date()}"


# ---------------------------------------------------------------------------
# 3. Отказ: парсер НЕ УГАДЫВАЕТ
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "not_a_date",
    [
        "Кадровое агентство Dream Team",   # работодатель
        "Скайтек",                          # работодатель
        "Отказ",                            # текст сообщения
        "Отклик на\xa0вакансию",            # наш собственный отклик
        "1",                                # счётчик непрочитанного
        "",                                 # пусто
        "Михаил, здравствуйте! Если вдруг",  # сниппет письма
    ],
)
def test_non_dates_are_refused(not_a_date):
    """Возврат `None` — обязательное поведение, а не побочный эффект.

    Именно здесь рождался дефект C отчёта: строка, не похожая на дату, молча
    становилась `employer`. Парсер даты обязан ошибаться в сторону отказа."""
    parse_card_date = _import_parser()
    assert parse_card_date(not_a_date, today="2026-09-30") is None, (
        f"{not_a_date!r} принято за дату — это вернёт дефект 'дата как работодатель'"
    )


def test_ambiguous_date_like_string_is_refused():
    """Похоже на дату, но не `ДД.ММ`: 99.99 существует как строка и не существует
    как день. Парсер обязан отказаться, а не построить 99-е число."""
    parse_card_date = _import_parser()
    assert parse_card_date("99.99", today="2026-09-30") is None
    assert parse_card_date("31.02", today="2026-09-30") is None, "31 февраля не бывает"


# ---------------------------------------------------------------------------
# 4. ГЛАВНОЕ: разобранная дата НЕ меняет идентичность сообщения
#
# Здесь я сначала сформулировал тест неправильно и это стоило правки
# продакшн-кода в неверную сторону. Разбор ошибки — ниже, он важнее самого теста.
#
# Первая версия требовала: `fp(sent_at=None) == fp(sent_at="вчера")`. Тест
# проходил после того, как я убрал `sent_at` из payload. Но проверка всех 38
# строк `state.db` показала, что ключ — `cid+sender+sent_at+text` (38 из 38), а
# `sent_at` там пустая строка. То есть поле УЖЕ в ключе, и его удаление
# осиротило бы всю историю. Тест «прошёл», будучи неверным: он закреплял
# удобную гипотезу, а не факт.
#
# Правильное свойство: **дата отображения не становится идентичностью**. Его
# обеспечивает вызывающая сторона — сниппет-ветка передаёт `sent_at=""`.
# ---------------------------------------------------------------------------
def test_an_empty_sent_at_reproduces_the_historical_key():
    """Ключ с пустым `sent_at` — тот же, что писали прошлые прогоны.

    Это и есть защита истории: пока сниппет-ветка передаёт пустую строку,
    ни одна из 38 существующих записей не осиротеет."""
    from ai_assistant.hh_message_watcher import compute_message_fingerprint as fp

    # Реальная строка state.db: neg_136613588 (JT marketing), sent_at = None
    stored = "hh_mfp_8ab21607aefb87568ff93f91"
    got = fp(conversation_id="neg_136613588", sender="employer",
             sent_at=None, text="Статус: Просмотрен")
    assert got == stored, f"исторический ключ не воспроизводится: {got} vs {stored}"


def test_none_and_empty_string_give_the_same_fingerprint():
    """`None` и `""` обязаны сходиться: прогоны писали и то, и другое."""
    from ai_assistant.hh_message_watcher import compute_message_fingerprint as fp

    a = fp(conversation_id="c1", sender="employer", sent_at=None, text="Отказ")
    b = fp(conversation_id="c1", sender="employer", sent_at="", text="Отказ")
    assert a == b, "None и '' дали разные ключи — часть истории станет невидимой"


def test_a_display_date_does_not_change_identity_when_it_is_not_the_message_time():
    """Относительная дата карточки нестабильна: «вчера» завтра читается как
    «позавчера». Если бы она попадала в ключ, дедупликация ломалась бы сама,
    без всякого фикса — достаточно пережить полночь.

    Поэтому идентичность строится на (переписка, автор, текст), а дата карточки
    живёт отдельным полем. Здесь закреплено, что сниппет-ветка не передаёт её
    как время сообщения."""
    from ai_assistant.hh_message_watcher import compute_message_fingerprint as fp

    cid, text = "5665125042", "Отказ"
    # Так пишет сниппет-ветка: дата карточки известна, но это НЕ время сообщения.
    snippet_key = fp(conversation_id=cid, sender="employer", sent_at="", text=text)
    # Так было бы, если бы дату взяли как время сообщения.
    naive_key = fp(conversation_id=cid, sender="employer", sent_at="вчера", text=text)
    assert snippet_key != naive_key, (
        "если ключи совпали — тест перестал различать два разных решения"
    )
    # И это ровно тот ключ, который узнают прошлые прогоны.
    assert snippet_key == fp(conversation_id=cid, sender="employer",
                             sent_at=None, text=text)


# ---------------------------------------------------------------------------
# 5. Живые данные: парсер справляется со ВСЕМИ 12 карточками
# ---------------------------------------------------------------------------
def test_every_live_card_date_is_parsed():
    """Ни одна из 12 реальных дат не должна остаться неразобранной.

    Формулировка «все 12» важна: 11 из 12 — это 92 %, и именно так выглядит
    неполный фикс. Пропущенная форма = пропущенное сообщение."""
    parse_card_date = _import_parser()
    failed = [d for d in LIVE_DATE_LINES if parse_card_date(d, today="2026-09-30") is None]
    assert not failed, f"не разобраны даты: {failed}"


def test_the_date_line_is_not_mistaken_for_the_employer():
    """Дефект C, закреплённый тестом: `rawLines[1]` — дата, `rawLines[2]` — работа.

    Это ровно тот случай, который в отчёте признан латентным. Тест делает его
    невозможным независимо от того, какой селектор сработает на странице."""
    parse_card_date = _import_parser()
    for cid, lines in LIVE_CARDS:
        if len(lines) < 3:
            continue
        date_line, employer_line = lines[1], lines[2]
        assert parse_card_date(date_line, today="2026-09-30") is not None, (
            f"conv {cid}: дата {date_line!r} не распознана"
        )
        assert parse_card_date(employer_line, today="2026-09-30") is None, (
            f"conv {cid}: работодатель {employer_line!r} принят за дату"
        )


# ---------------------------------------------------------------------------
# 6. Дозапись даты в уже накопленные строки (tools/.t728/backfill_sent_at.py)
# ---------------------------------------------------------------------------
# Правка watcher'а заполняет дату только в МОМЕНТ обнаружения сообщения. 37 из
# 38 строк были записаны раньше и остаются пустыми, поэтому дозапись — отдельный
# шаг. Здесь проверяется, что шаг узкий: он не создаёт событий, не воскрешает
# дедуп и не перебивает уже известное время.
#
# Фикстура БД даёт autouse-фикстура conftest: DB_FILE уводится в tmp_path, так
# что тесты не касаются живого state.db.
# ---------------------------------------------------------------------------
import sqlite3

from ai_assistant import config
from ai_assistant.db import (
    backfill_hh_message_sent_at,
    get_hh_message_event,
    init_db,
    save_hh_message_event,
)


@pytest.fixture()
def db(tmp_path):
    """Пустая схема в tmp_path: conftest уже увёл DB_FILE туда.

    Схему создаём явно, а не полагаемся на побочный эффект `save_...`: если
    таблицы нет, падение будет выглядеть как `no such table` и уведёт не туда.
    """
    init_db()
    return config.DB_FILE


def _rows() -> list[tuple[str, str | None]]:
    con = sqlite3.connect(f"file:{config.DB_FILE}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT message_fingerprint, sent_at FROM hh_message_events"
        ).fetchall()
    finally:
        con.close()


def _seed(fingerprint: str, sent_at: str | None) -> None:
    save_hh_message_event(
        message_fingerprint=fingerprint,
        conversation_id="5665125042",
        sender="employer",
        text="Отказ",
        sent_at=sent_at,
    )


def test_backfill_fills_an_empty_sent_at(db):
    _seed("hh_mfp_test_empty", None)
    assert backfill_hh_message_sent_at("hh_mfp_test_empty", "2026-09-29") is True
    assert get_hh_message_event("hh_mfp_test_empty")["sent_at"] == "2026-09-29"


def test_backfill_treats_empty_string_like_none(db):
    """В БД пустая строка и NULL — один и тот же факт «даты нет»."""
    _seed("hh_mfp_test_blank", "")
    assert backfill_hh_message_sent_at("hh_mfp_test_blank", "2026-09-29") is True
    assert get_hh_message_event("hh_mfp_test_blank")["sent_at"] == "2026-09-29"


def test_backfill_never_overwrites_a_known_time(db):
    """Настоящее время сообщения дороже даты с карточки (у неё точность — сутки).

    Если сообщение однажды придёт с меткой, бэкфилл не имеет права её затирать
    вчерашней датой — иначе точность не растёт, а падает."""
    _seed("hh_mfp_test_known", "2026-09-29T14:31:00")
    assert backfill_hh_message_sent_at("hh_mfp_test_known", "2026-09-12") is False
    assert get_hh_message_event("hh_mfp_test_known")["sent_at"] == "2026-09-29T14:31:00"


def test_backfill_is_idempotent(db):
    _seed("hh_mfp_test_twice", None)
    assert backfill_hh_message_sent_at("hh_mfp_test_twice", "2026-09-29") is True
    assert backfill_hh_message_sent_at("hh_mfp_test_twice", "2026-09-29") is False
    assert len(_rows()) == 1


def test_backfill_does_not_create_a_row_for_an_unknown_fingerprint(db):
    """Дозапись — UPDATE, а не INSERT. Иначе бэкфилл стал бы генератором событий,
    которых на странице нет: ровно тот дефект, которого мы избегаем."""
    _seed("hh_mfp_test_real", None)
    assert backfill_hh_message_sent_at("hh_mfp_test_ghost", "2026-09-29") is False
    assert [fp for fp, _ in _rows()] == ["hh_mfp_test_real"]


def test_backfill_does_not_resurrect_deduplication(db):
    """Ключ дедупа — fingerprint, и он не содержит sent_at-значение из карточки.

    Сквозная проверка: после дозаписи повторное вычисление fingerprint для того
    же сообщения всё ещё находит строку. Тест ловит ровно ту ошибку, которую я
    совершил в первом заходе (T7.28), — попытку выкинуть sent_at из ключа."""
    from ai_assistant.hh_message_watcher import compute_message_fingerprint

    key = compute_message_fingerprint(
        conversation_id="5665125042",
        sender="employer",
        sent_at="",
        text="Отказ",
    )
    _seed(key, None)
    assert backfill_hh_message_sent_at(key, "2026-09-29") is True
    again = compute_message_fingerprint(
        conversation_id="5665125042",
        sender="employer",
        sent_at="",
        text="Отказ",
    )
    assert again == key, "fingerprint сообщения изменился после дозаписи даты"
    assert get_hh_message_event(again) is not None


# ---------------------------------------------------------------------------
# 7. Дефект C: фильтр JS не пускает дату в работодателя
# ---------------------------------------------------------------------------
# Дефект жил в JS, который не исполняет ни один тест (нужен живой браузер), а
# тест из раздела 5 проверял только `parse_card_date` — то есть доказывал, что
# ПАРСЕР отличает дату от работодателя, но не что ФИЛЬТР её пропускает мимо.
# Это разные вещи, и вторая не проверялась вообще.
#
# Замер негативного контроля (2026-09-30, tools/.t728/filter_harness.js со старым
# правилом `/^(вчера|сегодня|позавчера|назад)$/`): дата стала работодателем в
# **10 из 12** карточек — `пн` (3), `вс`, `ср`, `20.09` (2), `15.09` (2), `12.09`.
# Выжили ровно те две, где HH напечатал `вчера`, — единственная форма, которую
# старый фильтр знал. То есть дефект был не «латентный по удаче», а широкий:
# он срабатывал на 10 карточках из 12 при отказе одного селектора.
import re

from ai_assistant.hh_message_reply import (
    _CONVERSATIONS_LIST_JS,
    CARD_DATE_LINE_JS_PATTERN,
    _conversations_list_js,
)


def test_the_date_rule_covers_every_live_date_form():
    """Все 12 живых форм проходят правило, которое уезжает на страницу."""
    rule = re.compile(CARD_DATE_LINE_JS_PATTERN)
    refusals = [d for d in LIVE_DATE_LINES if not rule.match(d)]
    assert not refusals, f"правило не ловит даты: {refusals}"


def test_the_date_rule_refuses_every_live_employer():
    """И ни один работодатель под него не попадает — иначе фильтр съест работу.

    Две стороны одного правила: пропустить дату и не тронуть работодателя.
    Без второй стороны «фикс» мог бы просто отбрасывать всё подряд."""
    rule = re.compile(CARD_DATE_LINE_JS_PATTERN)
    swallowed = [lines[2] for _, lines in LIVE_CARDS if rule.match(lines[2])]
    assert not swallowed, f"работодатели приняты за дату: {swallowed}"


def test_the_date_rule_refuses_non_dates():
    """Расширение до дней недели не должно превратиться в «отбрасывать всё»."""
    rule = re.compile(CARD_DATE_LINE_JS_PATTERN)
    for text in ["EvaTeam", "Скайтек", "Битриксолог", "Отказ", "Михаил, здравствуйте",
                 "пн в 10:00", "12", "", "1", "Middle"]:
        assert not rule.match(text), f"{text!r} принято за дату"


def test_the_placeholder_is_substituted_exactly_twice():
    """В шаблоне JS ровно два места подстановки — оба фильтра.

    Три означало бы, что правило записано ещё где-то (и разъедется); одно —
    что второй фильтр остался со старым правилом. Это замер, а не стиль."""
    assert _CONVERSATIONS_LIST_JS.count("__CARD_DATE_LINE__") == 2


def test_the_generated_js_carries_the_rule_and_no_placeholder():
    js = _conversations_list_js()
    assert "__CARD_DATE_LINE__" not in js, "плейсхолдер уехал на страницу"
    assert '(?:вчера|сегодня|позавчера|пн|вт|ср|чт|пт|сб|вс|\\d{1,2}\\.\\d{1,2})' in js, (
        "правило даты не подставилось в JS"
    )
    assert js.count("(?:вчера|сегодня") == 2, "правило подставилось не в оба фильтра"


def test_the_old_narrow_rule_would_have_failed_this_check():
    """Негативный контроль правила: старая форма обязана провалиться.

    Без этого теста расширение (`пн`…`вс`, `ДД.ММ`) можно откатить, и все
    остальные проверки останутся зелёными — как это и было до T7.28.C."""
    old = re.compile(r"^(вчера|сегодня|позавчера|назад)$")
    caught_by_old = [d for d in LIVE_DATE_LINES if not old.match(d)]
    assert len(caught_by_old) == 10, (
        f"старое правило пропускало {len(caught_by_old)} из 12 дат; "
        "ожидалось ровно 10 (выживают только две формы 'вчера')"
    )


# ---------------------------------------------------------------------------
# 8. Дефект A: цикл не принимает слепоту за тишину
# ---------------------------------------------------------------------------
# `hh-message triage` читал список переписок, не проверив сессию. Анонимный
# браузер отдаёт ПУСТОЙ список, и команда печатала `found: 0` — тот же отчёт,
# что и у честно пустого ящика. Дефект жил не в чтении, а в том, что отказ
# читать записывался как результат чтения.
#
# Тесты ниже проверяют три вещи: (а) неавторизованная сессия даёт BLOCKED, а не
# ноль; (б) не отличить сессию можно тоже только через BLOCKED; (в) удачное
# чтение не сломано — гейт не превратился в «всегда отказывать».
import json

from ai_assistant import cli


def _triage_payload():
    return {
        "url": "https://hh.ru/chat/5577169431",
        "conversations": [
            {"conversation_id": "111", "url": "https://hh.ru/chat/111",
             "title": "Dev", "employer": "Emp1", "snippet": "Отказ",
             "is_selected": False},
        ],
    }


def _eval_answering(auth_response):
    """evaluate_fn: на пробу авторизации отвечает `auth_response`, иначе — список."""
    def _eval(expr):
        if "authenticated" in expr:
            return json.dumps(auth_response)
        return json.dumps(_triage_payload())
    return _eval


def test_a_logged_out_session_is_blocked_not_zero(capsys):
    """Главный тест дефекта A: слепота обязана быть отказом, а не нулём."""
    ev = _eval_answering({"authenticated": False,
                          "reason": "Browser is on HH login page",
                          "url": "https://hh.ru/account/login"})

    rc = cli.hh_message_triage(evaluate_fn=ev, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1, "неавторизованная сессия не должна завершаться успехом"
    assert payload["status"] == "BLOCKED", (
        "статус READ-ONLY при неавторизованной сессии — это и есть дефект A"
    )
    assert payload["conversation_count"] == 0
    assert payload["items"] == []
    assert payload["errors"], "отказ без причины не объясняет, что чинить"
    assert "not authenticated" in payload["errors"][0]


def test_an_unreadable_page_is_blocked_with_an_honest_reason(capsys):
    """«Не смог проверить» — тоже отказ, но с другой формулировкой.

    `verified: False` означает, что дело не в истёкшем логине, а в том, что
    страница не читается. Перепутать эти два сообщения — значит отправить
    человека логиниться, когда у него сломан CDP."""
    ev = _eval_answering({"authenticated": False, "verified": False,
                          "reason": "the page could not be read: TimeoutError"})

    rc = cli.hh_message_triage(evaluate_fn=ev, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["status"] == "BLOCKED"
    assert "could not verify" in payload["errors"][0]
    assert "no conversations" in payload["errors"][0]


def test_an_auth_check_that_raises_blocks_the_run(capsys):
    """Проверка, которая сломалась, не имеет права отвечать «всё хорошо».

    Исключение здесь ловится **внутри** `check_hh_session_authenticated`
    (`hh_browser_launcher.py`, BLE001 finding #28) и возвращается как
    `authenticated: False` без `verified`. Свой `try` в `cli.py` нужен на случай,
    если сломается уже сам гейт, — то есть это вторая линия, а не первая.
    Проверяется вердикт, а не то, какой слой его вынес."""
    def _eval(expr):
        if "authenticated" in expr:
            raise RuntimeError("CDP socket closed")
        return json.dumps(_triage_payload())

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["status"] == "BLOCKED"
    assert "could not verify" in payload["errors"][0]
    assert "CDP socket closed" in payload["errors"][0], (
        "причина отказа потерялась по дороге"
    )


def test_a_page_that_answers_something_unrelated_blocks_the_run(capsys):
    """Ответ не про сессию — не «авторизован».

    Негативный контроль на самый вероятный способ сделать гейт формальным:
    считать всё, кроме явного `authenticated: false`, успехом."""
    def _eval(expr):
        # Отвечает на пробу содержимым списка переписок, как старые фейки.
        return json.dumps(_triage_payload())

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1, "страница, ответившая не на тот вопрос, принята за авторизованную"
    assert payload["status"] == "BLOCKED"
    assert "unrelated to the session check" in payload["errors"][0]


def test_an_authenticated_session_still_reads_normally(capsys):
    """Обратная сторона: гейт не должен стать «отказывать всегда»."""
    ev = _eval_answering({"authenticated": True,
                          "reason": "Applicant profile navigation element found",
                          "url": "https://hh.ru/applicant"})

    rc = cli.hh_message_triage(evaluate_fn=ev, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "READ-ONLY"
    assert payload["conversation_count"] == 1
    assert payload["items"][0]["conversation_id"] == "111"
    assert payload.get("errors") == []


def test_a_successful_read_still_returns_zero_even_when_empty(capsys):
    """Пустой, но авторизованный ящик — это по-прежнему ноль, а не BLOCKED.

    Если этот тест упадёт, значит гейт начал путать «нечего читать» с «не могу
    читать», то есть дефект A поменял знак, но не исчез."""
    def _eval(expr):
        if "authenticated" in expr:
            return json.dumps({"authenticated": True, "reason": "Session active",
                               "url": "https://hh.ru/applicant"})
        return json.dumps({"url": "https://hh.ru/chat/1", "conversations": []})

    rc = cli.hh_message_triage(evaluate_fn=_eval, as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "READ-ONLY"
    assert payload["conversation_count"] == 0


def test_the_js_pattern_constant_is_not_the_gate():
    """Гейт стоит в Python, а не в JS: JS-проверку не исполняет ни один тест.

    Тот же урок, что и в разделе 7, но с другой стороны. Здесь проверяется, что
    проверка сессии — это вызов из `cli.py`, который эти тесты дёргают живьём,
    а не строка внутри `_CONVERSATIONS_LIST_JS`."""
    import inspect

    src = inspect.getsource(cli.hh_message_triage)
    assert "check_hh_session_authenticated" in src, (
        "проверка сессии исчезла из triage"
    )
    # Два места: владеемый путь и путь с инъекцией evaluate_fn.
    assert src.count("check_hh_session_authenticated(") == 2, (
        "гейт стоит не на обоих путях чтения"
    )


def test_the_session_is_probed_in_the_main_frame_not_the_chatik_frame():
    """Живой замер 2026-09-30: проба обязана идти в ГЛАВНЫЙ фрейм.

    Это самая дорогая находка T7.28.A, и она не выводится из кода — только из
    браузера. В одном и том же живом сеансе:

        gate(_resolve_hh_evaluate())     -> authenticated=True,  verified=True
        gate(_resolve_chatik_evaluate()) -> authenticated=False, verified=False,
            reason='the page returned something unrelated to the session check:
                    {"error": "chatik frame not found", ...}'

    Причина: `_resolve_chatik_evaluate` привязан к isolated world iframe'а
    chatik.hh.ru. В этом фрейме нет навигации профиля, а при неоткрытой
    переписке он вообще отвечает сторожком `chatik frame not found`. Спросить
    у него «я авторизован?» нельзя — и гейт, поставленный туда, отказал бы
    ЗДОРОВОЙ сессии. Именно это и случилось на первой версии правки.

    Поэтому проверяется не «гейт вызван», а **на каком транспорте**: проба — на
    `auth_ev` (главный фрейм), чтение — на `ev` (chatik). Наблюдатель делает так
    же (`hh_message_watcher.py`: `_resolve_hh_evaluate`, затем чтение)."""
    import inspect

    src = inspect.getsource(cli.hh_message_triage)

    assert "auth_ev = _resolve_hh_evaluate(" in src, (
        "сессию проверяют не в главном фрейме - живой замер даёт ложный BLOCKED"
    )
    assert "check_hh_session_authenticated(auth_ev)" in src, (
        "гейт вызван не на главном транспорте"
    )

    # Владеемый путь обязан пробовать главный фрейм и не пробовать chatik.
    # `check_hh_session_authenticated(ev)` в пути с инъекцией законен: там
    # транспорт дал вызывающий, и другого у гейта нет - поэтому граница между
    # путями берётся по комментарию, а не по `errors = []` (последний встречается
    # в обоих и разрезал бы не там).
    owned_path = src.split("    # Defect A. `evaluate_fn` can be injected")[0]
    assert "check_hh_session_authenticated(auth_ev)" in owned_path, (
        "владеемый путь не проверяет сессию"
    )
    assert "check_hh_session_authenticated(ev)" not in owned_path, (
        "владеемый путь проверяет сессию на chatik-транспорте: фрейм не ответит"
    )


def test_a_verdict_reached_by_the_owned_path_is_not_re_asked():
    """Владеемый путь проверяет ОДИН раз и передаёт вердикт вниз.

    Без этого повторная проверка идёт уже на chatik-транспорте (см. тест выше) и
    снова выдумывает BLOCKED. Живой след: depth=1 `evaluate_fn=None` -> depth=2
    `evaluate_fn=SET`, и отказ приходил из ветки с `_gate`, то есть из второго
    круга, хотя первый круг гейт прошёл."""
    import inspect

    sig = inspect.signature(cli.hh_message_triage)
    assert "auth_verified" in sig.parameters, (
        "вердикт не передаётся в повторный вызов"
    )
    assert sig.parameters["auth_verified"].default is None, (
        "умолчание должно быть None: иначе внешний вызов обязан приносить вердикт"
    )

    src = inspect.getsource(cli.hh_message_triage)
    assert "if auth_verified is None:" in src, (
        "полученный вердикт не используется - повторная проба вернётся"
    )
    # Вердикт действительно уезжает в повторный вызов. Сравнение по переводам
    # строк здесь невозможно: `cli.py` хранит строки как CR CR LF, и
    # `inspect.getsource` отдаёт их удвоенными (пустая строка после каждой).
    # Поэтому переводы строк нормализуются, а проверяется форма вызова.
    flat = " ".join(src.split())
    assert "conversation_id, limit, cdp_url, url_substring, ev, as_json, profile, _auth," in flat, (
        "владеемый путь не передаёт вердикт вниз"
    )


def test_the_empty_report_is_only_trustworthy_because_the_gate_ran():
    """Негативный контроль смысла: тот же вывод, но без гейта — недоказателен.

    `found: 0 / errors: 0 / READ-ONLY` — это ровно то, что печатал дефект
    (tools/.t728/triage_empty_20260930.txt). Сам по себе такой вывод не значит
    ничего; он становится измерением только после проверки сессии. Тест
    фиксирует порядок: гейт стоит ВЫШЕ чтения, а не рядом с ним."""
    import inspect

    src = inspect.getsource(cli.hh_message_triage)
    gate_pos = src.index("check_hh_session_authenticated(auth_ev)")
    read_pos = src.index("fetch_hh_conversations_list_readonly(ev)")
    assert gate_pos < read_pos, (
        "чтение стоит раньше проверки - слепота снова станет 'нулём'"
    )


