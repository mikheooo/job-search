"""Мутации №65: нечитаемый payload строки отклика уносит её submission_id.

Что проверяется
---------------
Правка №65 трогает два файла и четыре места:

  * `submission_recovery.inspect_submission_state()` - индекс 1 (submission_id)
    читается ПЕРВЫМ, а разбор индекса 3 (payload) вынесен в отдельную пробу,
    которая ничего не отнимает;
  * там же - флаг `submission_payload_unreadable` едет в `_determine_recovery_status()`
    и в список предупреждений;
  * `_determine_recovery_status()` - новая ветка перед честной: «запись не
    читается» вместо «сверки не было» и вместо совета идти на сайт;
  * `cli.submissions_show()` - разбор заменён пробой, нечитаемая ветка печатает
    честное предложение вместо «No verification performed yet.»;
  * `cli.submissions_verify()` - индекс 1 вместо индекса 3 в качестве ключа.

Мутации ломают предохранитель девятью способами, и каждая обязана быть поймана
своим пином:

  * M0 «разбор снова уносит ключ» - в `except` добавлено `submission_id = None`;
  * M1 «дефект возвращён целиком» - прежняя форма: один `try`, id присваивается
    ПОСЛЕ разбора, `except` выбрасывает оба и пишет `{"raw": ...}`;
  * M2 «флаг не доезжает до решения» - `submission_payload_unreadable=` убран из
    вызова `_determine_recovery_status()`;
  * M3 «ветка есть, а предложение прежнее» - новая ветка возвращает честную пару
    «no verification performed» + «run verification ... on the website»;
  * M4 «ветка открывает калитку» - новая ветка возвращает `NO_ACTION`, то есть
    нечитаемая запись выглядит улаженной;
  * M5 «ключ снова берётся из payload» - `submission_id = submission_row[3]`;
  * M6 «CLI снова разбирает вне try» - прежняя строка `sub_json`-разбора;
  * M7 «CLI не заходит в нечитаемую ветку» - условие заменено на `if False:`;
  * M8 «CLI verify снова берёт колонку submission_id» - прежний `json.loads(sub_row[1])`;
  * M9 «CLI вошёл в ветку, но печатает прежнее предложение» - текст честного
    `print` заменён на `No verification performed yet.`.

M2 и M3 - две разные двери к одному предложению, и это нарочно: M2 ломает
передачу, M3 - содержание. M7 и M9 - то же самое для CLI.

Почему пин №5 не контроль
-------------------------
`test_a_readable_submission_still_finds_its_verification` держится на том, что
ключ поиска - колонка 1. M5 ломает ровно это, поэтому пин №5 входит в улики M5,
а не в контроли: контроль, который обязан краснеть от одной из мутаций,
контролем не является.

Контроли - семь пинов, которые обязаны остаться зелёными при КАЖДОЙ мутации:
честная половина №65 (пин №6), контроль №55 внутри №65 (пин №7) и по три/два
пина самих №55 и №56. Последние печатаются в доказательство, что правка не
поехала по соседям.

Сопутствующий урон M5, и почему он назван, а не спрятан
-------------------------------------------------------
Первый прогон M5 показал красными не только свои улики, но и три контроля:
`q55_control` (пин №7), `nb55_missing`, `nb55_advice`. Разбор по junit: все
семь параметров `nb55_missing` получили `Submission exists but no verification
performed`, а `nb55_advice` - `Run verification to check submission result on
the website`. Механизм один и тот же: M5 подставляет в `submission_id` текст
колонки payload, а этим ключом пользуются ОБА поиска - и №65, и №55. Промах
ключа превращает любую запись верификации в «её нет», поэтому падают пины,
которые к №65 отношения не имеют.

Это не «предохранитель шире дефекта»: здесь дефект достаточно глубок, чтобы
достать до соседа. Но инструмент, печатающий в конце «controls intact», когда
строкой выше напечатано `CONTROL BREAKAGE`, врёт - и это исправлено: у каждой
мутации теперь есть список `collateral`, а сводная строка считает битые
контроли. Три пина внесены в `collateral` M5 ПОСЛЕ замера, с этим механизмом в
обосновании; `nb55_reconcile` остался контролем, потому что он держится на
`last_verification is None`, а промах ключа даёт именно None и его не ломает.

Запись честная: это не предсказание, а атрибуция замера. Предсказанием было бы
обратное утверждение, и оно здесь не делается.

Осторожно с CR в cli.py
-----------------------
`cli.py` держит голые `\\r` (строки кончаются `\\r\\r\\n`), поэтому якоря и
замены записаны здесь с явными `\\r\\r\\n`, а не с `\\n`. Опечатка в этом месте
не проходит молча: якорь просто не найдётся, и инструмент напечатает
«anchor appears 0 times», а не «мутация выжила». Дополнительно `_cr_style_guard`
требует, чтобы в замене КАЖДЫЙ `\\n` был частью `\\r\\r\\n`: иначе мутация
написала бы в файл LF-строки и мерила бы перевод строки, а не предохранитель.

Как читается вердикт
--------------------
Вердикты берутся из ``--junitxml``, а не из stdout: прогон может быть убит
сторожем массовых удалений на уборке `%TEMP%\\pytest-of-Misha\\` ПОСЛЕ `[100%]`,
и тогда сводной строки нет, а junit есть. Критерий - ``any()``: вопрос в том,
слеп ли набор пинов к ошибке, и одного сработавшего пина достаточно. Пин,
отсутствующий в отчёте, - ``UNKNOWN``, а не ``PASS``; ``FAIL`` важнее ``PASS``.

Файлы восстанавливаются байт-в-байт ИЗ ПАМЯТИ, хеши печатаются; без
восстановления инструмент вердикта не даёт.

Ошибка пятая, и она дороже всех остальных: два экземпляра сразу
---------------------------------------------------------------
`M0 M1 M2 M3` одним вызовом оболочки был запущен **дважды**: оболочка иногда
исполняет команду второй раз, а автоперевод в фон этого не отменяет. Экземпляры
наложились, и экземпляр B прочитал себе в «исходные байты» файл, в котором
экземпляр A уже применил M2. Дальше B, как положено, восстановил файл ИЗ ПАМЯТИ -
то есть записал мутацию навсегда. `submission_recovery.py` остался в 563 строках
без строки `submission_payload_unreadable=submission_payload_unreadable,`, и
следующий прогон напечатал «anchor appears 0 times» для M2. Инструмент при этом
честно написал `restored byte-for-byte: True`: он сравнивал с ТЕМ, ЧТО САМ
ПРОЧИТАЛ, а прочитал он уже испорченное.

Файл восстановлен байт-в-байт (`6b93d3cd…` -> `b8084b2b…`, 564 строки, 0 CR), и
в инструмент добавлены три предохранителя ровно против этого:

  * `EXPECTED_SHA` - хеши файлов, на которых набор пинов заведомо зелёный;
    расхождение = отказ запускаться, а не «восстановлю что прочитал»;
  * замок на `%TEMP%\\job-search-mutation.lock` через `msvcrt.locking` - второй
    экземпляр отказывается работать, пока первый держит замок. Замок именно
    ОС-ный, а не файл-флаг: ядро снимает его само при смерти процесса, поэтому
    убитый сторожем прогон не оставляет за собой вечного замка. Первая редакция
    делала файл-флаг и удаляла его в `finally` - сторож убил процесс ровно на
    этом `unlink`, замок остался, и следующий запуск отказался работать;
  * после восстановления сверяется `EXPECTED_SHA`, а не только «до»: без этого
    проверка «восстановлено» ничего не стоит.

Запуск:
    .venv/Scripts/python.exe tools/ble001_submission_payload_mutation.py
    .venv/Scripts/python.exe tools/ble001_submission_payload_mutation.py M0 M5
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:  # Windows-only, как и весь проект; на других ОС замка просто нет.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
TESTS = "tests/test_ble001_fail_closed.py"
RECOVERY = REPO / "ai_assistant" / "submission_recovery.py"
CLI = REPO / "ai_assistant" / "cli.py"
LOCK = Path(tempfile.gettempdir()) / "job-search-mutation.lock"

# Хеши дерева, на котором пины №65 заведомо зелёные. Обновлять ЯВНО после
# каждой правки источников: расхождение здесь - сигнал, а не помеха.
EXPECTED_SHA = {
    RECOVERY: "b8084b2be6bd15c4c1df77d595c757d0f6ef82a890bac98f47fc1db1d0cd79e5",
    CLI: "f9d44e9b39af057e8e726da00bfa84aadf00113d652bdc199aa7280adf01f28c",
}


def _acquire_lock():
    """Замок, который снимает ядро, а не `finally`.

    Файл замка создаётся один раз и больше не удаляется: удаление тратит бюджет
    сторожа массовых удалений и, как показал первый прогон, может быть прервано
    ровно на нём.
    """
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    if msvcrt is None:
        return fd
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(fd)

# --- пины №65 --------------------------------------------------------------
P1 = "test_an_unreadable_submission_payload_does_not_become_no_verification_performed"
P2 = "test_an_unreadable_submission_payload_does_not_send_a_human_to_the_website"
P3 = "test_a_broken_payload_no_longer_costs_the_submission_id"
P4 = "test_the_submission_id_comes_from_the_column_and_not_from_the_payload"
P5 = "test_a_readable_submission_still_finds_its_verification"
P6 = "test_a_missing_verification_is_still_reported_as_missing"
P7 = "test_an_unreadable_verification_record_is_still_named"
P8 = "test_an_unreadable_submission_record_still_fails_closed"
P9 = "test_submissions_show_survives_an_unreadable_submission_record"
P10 = "test_submissions_verify_reads_the_submission_id_column"

# --- соседи №55 и №56: правка не имеет права их двигать -------------------
NB55_A = "test_an_unreadable_verification_record_is_not_reported_as_missing"
NB55_B = "test_an_unreadable_verification_record_does_not_advise_rechecking_the_site"
NB55_C = "test_an_unreadable_verification_record_still_fails_reconcile_closed"
NB56_A = "test_an_unreadable_review_record_is_flagged_by_the_audit"
NB56_B = "test_the_review_gate_does_not_call_an_unreadable_record_missing"

SHORT = {
    P1: "no_false_sentence",
    P2: "no_site_advice",
    P3: "id_survives",
    P4: "id_from_column",
    P5: "readable_finds_ver",
    P6: "missing_still_said",
    P7: "q55_control",
    P8: "still_refuses",
    P9: "show_survives",
    P10: "verify_key",
    NB55_A: "nb55_missing",
    NB55_B: "nb55_advice",
    NB55_C: "nb55_reconcile",
    NB56_A: "nb56_audit",
    NB56_B: "nb56_gate",
}

# ---------------------------------------------------------------------------
# Якоря, дословно. Несовпадение якоря печатается как ошибка, а не как
# «мутация выжила»: протухший якорь и слепые пины выглядят одинаково.
# ---------------------------------------------------------------------------

# --- submission_recovery.py (чистый LF) ------------------------------------

REC_ID_FIRST = (
    "        submission_id = submission_row[1]  # submission_id is at index 1\n"
    "        if submission_row[3]:\n"
    "            try:\n"
    "                json.loads(submission_row[3])\n"
    "            except (ValueError, TypeError) as e:\n"
    "                submission_payload_unreadable = True\n"
)
# M0: разбор снова уносит ключ с собой - тот же ущерб, что и в дефекте, но без
# выбрасывания всего остального. Пин №3 и №4 держат именно это.
REC_ID_TAKEN = (
    "        submission_id = submission_row[1]  # submission_id is at index 1\n"
    "        if submission_row[3]:\n"
    "            try:\n"
    "                json.loads(submission_row[3])\n"
    "            except (ValueError, TypeError) as e:\n"
    "                submission_payload_unreadable = True\n"
    "                submission_id = None\n"
)

REC_FIX_BLOCK = (
    "        # BLE001 finding #65. submission_id is index 1 and owes nothing to the\n"
    "        # payload at index 3, so it is read FIRST and the parse below can no\n"
    "        # longer take it away. The old shape parsed index 3 inside the same try\n"
    "        # that assigned submission_id, and its except discarded both - while the\n"
    "        # parsed value was never read anywhere in this function, so the parse\n"
    "        # was dead work whose only possible effect was to lose the key the\n"
    "        # verification lookup is made of.\n"
    "        #\n"
    "        # Measured on the live state.db: 9 of 15 rows have a payload column that\n"
    "        # is not JSON (an older save_submission() argument order put the\n"
    "        # submission_id there and the payload in submitted_at), and four of them\n"
    "        # have a VERIFIED verification on file that this report then described\n"
    "        # as \"no verification performed\" - the sentence #55 forbade.\n"
    "        submission_id = submission_row[1]  # submission_id is at index 1\n"
    "        if submission_row[3]:\n"
    "            try:\n"
    "                json.loads(submission_row[3])\n"
    "            except (ValueError, TypeError) as e:\n"
    "                submission_payload_unreadable = True\n"
    "                logger.warning(\n"
    "                    \"submission_recovery: submission payload for %s is not readable \"\n"
    "                    \"(%s: %s); submission_id %r survives, because it is not made of it\",\n"
    "                    vacancy_stable_id, type(e).__name__, e, submission_id,\n"
    "                )\n"
    "        last_submission = {\n"
    "            \"submission_id\": submission_id,\n"
    "            \"status\": submission_row[4],\n"
    "            \"submitted_at\": submission_row[5],\n"
    "            \"created_at\": submission_row[6],\n"
    "            \"updated_at\": submission_row[7],\n"
    "            \"executor_version\": submission_row[2],\n"
    "            \"payload_unreadable\": submission_payload_unreadable,\n"
    "        }\n"
)
# M1: прежняя форма целиком. `sub_json` здесь снова не используется - он и в
# дефекте не использовался, это и есть суть находки.
REC_PREFIX_SHAPE = (
    "        try:\n"
    "            # New schema: 0=vacancy_stable_id, 1=submission_id, 2=executor_version, 3=submission_json, 4=status, 5=submitted_at, 6=created_at, 7=updated_at\n"
    "            sub_json = json.loads(submission_row[3]) if submission_row[3] else {}\n"
    "            submission_id = submission_row[1]  # submission_id is at index 1\n"
    "            last_submission = {\n"
    "                \"submission_id\": submission_id,\n"
    "                \"status\": submission_row[4],\n"
    "                \"submitted_at\": submission_row[5],\n"
    "                \"created_at\": submission_row[6],\n"
    "                \"updated_at\": submission_row[7],\n"
    "                \"executor_version\": submission_row[2],\n"
    "            }\n"
    "        except Exception as e:\n"
    "            logger.warning(\n"
    "                \"submission_recovery: submission row for %s is not readable \"\n"
    "                \"(%s: %s); losing submission_id also loses the verification lookup\",\n"
    "                vacancy_stable_id, type(e).__name__, e,\n"
    "            )\n"
    "            last_submission = {\"raw\": str(submission_row)}\n"
)

REC_CALL_WITH_FLAG = (
    "        verification_unreadable=verification_unreadable,\n"
    "        submission_payload_unreadable=submission_payload_unreadable,\n"
    "    )\n"
)
REC_CALL_WITHOUT_FLAG = (
    "        verification_unreadable=verification_unreadable,\n"
    "    )\n"
)

REC_BRANCH = (
    "            if submission_payload_unreadable:\n"
    "                # BLE001 finding #65. The advice must not be \"go and check the\n"
    "                # website\": the record is in the database and this reader could\n"
    "                # not read it, so the honest action is to look at the record.\n"
    "                return (\n"
    "                    RecoveryStatus.NEEDS_VERIFICATION,\n"
    "                    (\n"
    "                        \"Submission record cannot be read - whether its verification \"\n"
    "                        \"exists is unknown\"\n"
    "                    ),\n"
    "                    (\n"
    "                        \"Inspect the stored submission record - its payload cannot be \"\n"
    "                        \"parsed, so a verification stored under a different key would be \"\n"
    "                        \"invisible here. Do not assume it was never verified\"\n"
    "                    ),\n"
    "                )\n"
)
# M3: ветка на месте, но говорит прежнее - и предложение, и совет.
REC_BRANCH_PREFIX_SENTENCE = (
    "            if submission_payload_unreadable:\n"
    "                return (\n"
    "                    RecoveryStatus.NEEDS_VERIFICATION,\n"
    "                    \"Submission exists but no verification performed\",\n"
    "                    \"Run verification to check submission result on the website\",\n"
    "                )\n"
)
# M4: ветка открывает калитку - нечитаемая запись выглядит улаженной.
REC_BRANCH_GATE_OPEN = REC_BRANCH.replace(
    "                    RecoveryStatus.NEEDS_VERIFICATION,\n",
    "                    RecoveryStatus.NO_ACTION,\n",
    1,
)
assert REC_BRANCH_GATE_OPEN != REC_BRANCH

REC_ID_LINE = "        submission_id = submission_row[1]  # submission_id is at index 1\n"
REC_ID_FROM_PAYLOAD = "        submission_id = submission_row[3]  # M5: ключ снова из payload\n"

# --- cli.py (голые CR: строки кончаются \\r\\r\\n) ----------------------------
CRLF = "\r\r\n"

CLI_PROBE = (
    "    payload_unreadable = False" + CRLF
    + "    if sub_row[3]:" + CRLF
    + "        try:" + CRLF
    + "            json.loads(sub_row[3])" + CRLF
    + "        except (ValueError, TypeError):" + CRLF
    + "            payload_unreadable = True"
)
# M6: прежняя строка целиком, вне try. `payload_unreadable = False` оставлена
# нарочно: без неё мутация падала бы ещё и на неопределённом имени в ветке ниже,
# то есть мерила бы две вещи сразу. Здесь мерится ровно падение на разборе.
CLI_PROBE_PREFIX = (
    "    sub_data = json.loads(sub_row[3]) if sub_row[3] else {}" + CRLF
    + "    payload_unreadable = False"
)

CLI_BRANCH_COND = (
    "        if payload_unreadable:" + CRLF
    + "            # BLE001 finding #65: the same false sentence as #55, reached one"
)
# M7: ветка не берётся вовсе - печатается прежняя пара.
CLI_BRANCH_NEVER = (
    "        if False:  # M7: нечитаемая ветка не берётся" + CRLF
    + "            # BLE001 finding #65: the same false sentence as #55, reached one"
)

CLI_HONEST_TEXT = (
    "                \"No verification found for this submission record, and its payload \"" + CRLF
    + "                \"cannot be read - whether a verification exists is unknown.\""
)
# M9: ветка взята, а предложение прежнее.
CLI_HONEST_TEXT_PREFIX = "                \"No verification performed yet.\""

CLI_VERIFY_FIX = "    sub_id = sub_row[1] or sub_row[0]"
CLI_VERIFY_PREFIX = (
    "    sub_data = json.loads(sub_row[1]) if sub_row[1] else {}" + CRLF
    + "    sub_id = sub_data.get(\"submission_id\", sub_row[0])"
)

# (имя, файл, описание, старое, новое, пины, сопутствующий урон)
#
# Сопутствующий урон - пины из CONTROLS, которые мутация вправе покрасить, с
# причиной. Пустой список (у девяти мутаций из десяти) означает полное
# требование: все контроли зелёные.
MUTATIONS = [
    (
        "M0", RECOVERY,
        "разбор снова уносит ключ: submission_id = None в except",
        REC_ID_FIRST, REC_ID_TAKEN,
        [P3, P4],
        [],
    ),
    (
        "M1", RECOVERY,
        "дефект возвращён целиком: один try, id после разбора, except выбрасывает оба",
        REC_FIX_BLOCK, REC_PREFIX_SHAPE,
        [P1, P2, P3, P4],
        [],
    ),
    (
        "M2", RECOVERY,
        "флаг не доезжает до решения: submission_payload_unreadable убран из вызова",
        REC_CALL_WITH_FLAG, REC_CALL_WITHOUT_FLAG,
        [P1, P2],
        [],
    ),
    (
        "M3", RECOVERY,
        "ветка есть, предложение прежнее: no verification performed + совет идти на сайт",
        REC_BRANCH, REC_BRANCH_PREFIX_SENTENCE,
        [P1, P2],
        [],
    ),
    (
        "M4", RECOVERY,
        "ветка открывает калитку: NO_ACTION вместо NEEDS_VERIFICATION",
        REC_BRANCH, REC_BRANCH_GATE_OPEN,
        [P8],
        [],
    ),
    (
        "M5", RECOVERY,
        "ключ снова берётся из payload: submission_id = submission_row[3]",
        REC_ID_LINE, REC_ID_FROM_PAYLOAD,
        [P3, P4, P5],
        [
            (P7, "контроль №55 внутри №65 смотрит тем же ключом, который M5 подменяет"),
            (NB55_A, "пин №55: промах ключа превращает запись верификации в «её нет»"),
            (NB55_B, "пин №55: тот же промах, совет откатывается к «идти на сайт»"),
        ],
    ),
    (
        "M6", CLI,
        "submissions show снова разбирает вне try: JSONDecodeError до первой строки",
        CLI_PROBE, CLI_PROBE_PREFIX,
        [P9],
        [],
    ),
    (
        "M7", CLI,
        "submissions show не заходит в нечитаемую ветку: if False",
        CLI_BRANCH_COND, CLI_BRANCH_NEVER,
        [P9],
        [],
    ),
    (
        "M8", CLI,
        "submissions verify снова берёт колонку submission_id: json.loads(sub_row[1])",
        CLI_VERIFY_FIX, CLI_VERIFY_PREFIX,
        [P10],
        [],
    ),
    (
        "M9", CLI,
        "submissions show вошёл в ветку, но печатает прежнее предложение",
        CLI_HONEST_TEXT, CLI_HONEST_TEXT_PREFIX,
        [P9],
        [],
    ),
]

# Пины, которые обязаны остаться зелёными при КАЖДОЙ мутации.
CONTROLS = [P6, P7, NB55_A, NB55_B, NB55_C, NB56_A, NB56_B]

FILES = [RECOVERY, CLI]


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


def _cr_style_guard(old: str, new: str) -> str | None:
    """В `cli.py` каждый `\\n` обязан быть частью `\\r\\r\\n`.

    Мутация, написанная LF-строками, меняла бы перевод строки файла - и «поймана»
    означала бы что угодно, кроме проверки предохранителя. Это ошибка построения,
    и она должна быть названа ошибкой.
    """
    for label, text in (("anchor", old), ("replacement", new)):
        if text.count("\n") != text.count("\r\r\n"):
            return (f"{label} mixes line endings: {text.count(chr(10))} LF vs "
                    f"{text.count(chr(13) * 2 + chr(10))} CR CR LF")
    return None


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("-")]
    selected = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    if not selected:
        print(f"no mutation matches {wanted}; known: {[m[0] for m in MUTATIONS]}")
        return 2

    # --- предохранитель первый: замок против второго экземпляра -------------
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print(f"REFUSING TO RUN: {LOCK} is held by another instance. Two "
              f"instances interleaving makes one of them read the other's "
              f"mutation as its 'original bytes' and restore the mutation "
              f"permanently (that is how #65 lost a line). Wait for it, or "
              f"check no stray python is running.")
        return 2

    try:
        # --- предохранитель второй: дерево обязано быть тем, на котором
        # набор пинов заведомо зелёный --------------------------------------
        drift = {p: sha(p) for p in FILES if sha(p) != EXPECTED_SHA[p]}
        if drift:
            for path, got in drift.items():
                print(f"REFUSING TO RUN: {path.name} sha256 {got}")
                print(f"                  expected {EXPECTED_SHA[path]}")
            print("The tree is not the one this instrument was calibrated on. "
                  "Re-run the pins green first, then update EXPECTED_SHA.")
            return 2
        return _run(selected)
    finally:
        _release_lock(lock_fd)


def _run(selected: list) -> int:
    originals = {p: p.read_bytes() for p in FILES}
    before = {p: sha(p) for p in originals}
    for path, digest in before.items():
        print(f"{path.name} sha256 before: {digest}")
    print(f"{len(selected)} of {len(MUTATIONS)} mutations\n")

    header = f"{'mut':4s} {'verdict':9s} what changed"
    print(header)
    print("-" * len(header))

    survived = []
    ill_formed = []
    controls_broken = []
    try:
        for name, target, description, old, new, pins, collateral in selected:
            original = originals[target]
            count = original.count(old.encode("utf-8"))
            if count != 1:
                print(f"{name:4s} !! anchor appears {count} times in "
                      f"{target.name}, expected 1: {old.splitlines()[0][:60]!r}")
                survived.append(name)
                continue
            mutated = original.replace(old.encode("utf-8"), new.encode("utf-8"), 1)
            if mutated == original:
                print(f"{name:4s} !! mutation changed nothing")
                survived.append(name)
                continue
            if target == CLI:
                bad_shape = _cr_style_guard(old, new)
                if bad_shape:
                    print(f"{name:4s} !! malformed mutation: {bad_shape}")
                    ill_formed.append(name)
                    continue

            excused = {p for p, _ in collateral}
            checked_controls = [p for p in CONTROLS if p not in excused]

            target.write_bytes(mutated)
            xml = REPO / f".pytest_q65_mut_{name}.xml"
            all_pins = pins + CONTROLS
            verdicts = run_pins(all_pins, xml)
            fired = [p for p in pins if verdicts[p] == "FAIL"]
            unknown = [p for p in all_pins if verdicts[p] == "UNKNOWN"]
            broken = [p for p in checked_controls if verdicts[p] != "PASS"]
            caught = bool(fired)
            print(f"{name:4s} {'caught' if caught else 'SURVIVED':9s} {description}")
            print("     evidence: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in pins))
            print("     controls: " + ", ".join(
                f"{SHORT[p]}={verdicts[p]}" for p in checked_controls))
            for pin, why in collateral:
                print(f"     collateral (not a control here): {SHORT[pin]}"
                      f"={verdicts[pin]} - {why}")
            if unknown:
                print(f"     !! never ran (UNKNOWN, not PASS): "
                      f"{', '.join(SHORT[p] for p in unknown)}")
            if not caught:
                survived.append(name)
            if broken:
                controls_broken.append(f"{name}: {', '.join(SHORT[p] for p in broken)}")
            target.write_bytes(original)
    finally:
        for path, data in originals.items():
            path.write_bytes(data)

    print()
    ok = True
    for path, digest in before.items():
        after = sha(path)
        # Сверка с EXPECTED_SHA, а не только с «до». «До» - это то, что
        # инструмент сам прочитал, и если он прочитал уже испорченный файл
        # (наложение двух экземпляров), то `after == digest` скажет «восстановлено»
        # о мутации, записанной навсегда.
        same = after == digest and after == EXPECTED_SHA[path]
        ok = ok and same
        print(f"{path.name} sha256 after : {after}")
        print(f"  restored byte-for-byte: {same}")
        if not same:
            print(f"  !! expected {EXPECTED_SHA[path]}")
            print(f"  !! read back {digest} - the file did NOT come back to the "
                  f"tree this instrument was calibrated on. Do not trust any "
                  f"verdict above; inspect the file.")
    if not ok:
        return 2

    print()
    if ill_formed:
        print(f"MALFORMED MUTATIONS (not verdicts): {', '.join(ill_formed)}")
    if controls_broken:
        print("CONTROL BREAKAGE (the guard is wider than the defect, or a "
              "neighbour moved, and no collateral reason was declared):")
        for line in controls_broken:
            print(f"  {line}")
    if survived or ill_formed or controls_broken:
        if survived:
            print(f"RESULT: {len(survived)} mutation(s) survived: {', '.join(survived)}")
        if ill_formed:
            print(f"RESULT: {len(ill_formed)} mutation(s) malformed: "
                  f"{', '.join(ill_formed)}")
        if controls_broken:
            print(f"RESULT: {len(controls_broken)} mutation(s) broke an undeclared "
                  f"control")
        return 1
    print(f"RESULT: all {len(selected)} mutations caught, controls intact "
          f"(declared collateral excluded), tree restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
