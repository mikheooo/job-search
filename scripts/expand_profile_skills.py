"""Расширение профиля навыками из собственных документов Миши.

ВАЖНО ПРО АРХИТЕКТУРУ (T8)
`candidate_profile.json` — НЕ источник правды, а производный артефакт.
Источник — `candidate_kb.json`: секция `claims` (факты из резюме) и секция
`preferences` (решения о поиске, в том числе списки навыков). Профиль
собирается из KB модулем `ai_assistant/candidate/profile_render.py`.

Поэтому править профиль напрямую НЕЛЬЗЯ: тест
`tests/test_profile_render.py::test_the_live_kb_reproduces_the_live_profile`
падает, потому что KB перестаёт воспроизводить файл на диске. Проверено на
собственной ошибке 2026-10-10: правка профиля руками дала ровно этот провал.

Скрипт делает две вещи:
  1. правит `preferences` в candidate_kb.json (с бэкапом),
  2. вызывает штатный `candidate-profile render` и пересобирает профиль.

Источники навыков (всё уже лежит в проекте, ничего не выдумано):
  * resume.md                              — резюме: 32 skill-claim'а + 15 duty
  * candidate_kb.json                      — 122 структурированных утверждения
  * письмо Ланкею, отправленное 2026-10-07 — 10+ лет сисадмина, MS Office, Outlook,
                                             Exchange, M365, IMAP/POP3, GAL, PST/OST,
                                             Autodiscover, GPO
                                             (текст в tools/send_lankey_office_reply.py)

ПОЧЕМУ ВСЁ ИДЁТ В secondary_skills
`CandidateProfile.transferable_skills` — не второй список, а ЗАПАСНОЙ АЛИАС
secondary_skills: `norm_sec = _norm_list(self.secondary_skills or
self.transferable_skills)` (candidate_profile.py:134), затем
`self.transferable_skills = norm_sec` (:144) и то же самое в from_dict (:302).
Пока `secondary_skills` непуст, список `transferable_skills` из JSON
ПОЛНОСТЬЮ ИГНОРИРУЕТСЯ и до матчера не доезжает.

Проверено на живом профиле: из 18 навыков JSON-списка transferable_skills
восемь (`git`, `ci/cd`, `itsm`, `networking`, `vpn`, `troubleshooting`,
`pandas`, `asyncio`) не появлялись ни в `secondary_skills_lc`, ни в
`transferable_skills_lc`, ни в `skills_lc`. Причём это НЕ следствие правки —
они были невидимы и в исходном профиле: те же восемь перечислены в
примере-дефолте класса (candidate_profile.py:486), откуда и попали в JSON.
Намеренность алиаса подтверждает негативный контроль
`tools/.t8/negative_controls.py:55` («collapse transferable_skills onto
secondary_skills»).

Поэтому все реальные навыки кладём в secondary_skills — это единственный
неглавный список, который матчер реально видит. В transferable_skills
оставляем смежное подмножество: JSON остаётся читаемым, а сырые читатели
(генератор черновиков `hh_message_reply.py:744-750` читает secondary первым и
добирает transferable; `gig/assessment.py:275-278` использует setdefault, так
что веса из skill_confidence не перетираются) работают как раньше.

`core_skills` и `skills` НЕ трогаем: их длина задаёт знаменатель для вакансий
семейства OTHER, и раздувание главного списка уронило бы им балл.

Уровни — словарь самого профиля. Для навыков, которые в профиле уже были,
уровень СОХРАНЯЕТСЯ как есть. Новые — по правилу:
    PROFESSIONAL — документировано как повседневная работа
    PROJECT      — документировано как часть проекта/стека
    BASIC        — прямое основание, но не повседневно
    TRANSFERABLE — только соседство с документированным навыком

`aws` и `terraform` добавлены на САМЫЙ НИЗКИЙ уровень (TRANSFERABLE, вес 0.4):
прямых свидетельств нет ни в резюме, ни в KB. Основание — только соседство, и
ровно то же, по которому в профиль попали `gcp` (сосед по облаку: у него
GCP-VPS, Docker, Linux) и `ansible` (сосед по автоматизации инфраструктуры).
Это последние два навыка со спросом >=5 в его APPLY/REVIEW-выборке (38 и 14
упоминаний) — после них верхушка спроса закрыта полностью.

Запуск: .venv/Scripts/python.exe scripts/expand_profile_skills.py
"""

from __future__ import annotations

import collections
import datetime as _dt
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from ai_assistant.candidate.kb import CandidateKnowledgeBase  # noqa: E402
from ai_assistant.candidate.profile_render import (  # noqa: E402
    candidate_profile_render_cmd,
)

KB_PATH = ROOT / "candidate_kb.json"
PROFILE_PATH = ROOT / "candidate_profile.json"
KB_BACKUP = ROOT / "candidate_kb.json.bak-20261010-t-skills-expansion"

# --- то, что НЕ меняем -------------------------------------------------------
KEEP_CORE = [
    "python", "n8n", "automation", "ai agents", "rest api", "webhooks",
    "telegram", "active directory", "sql",
]
KEEP_SECONDARY = [
    "fastapi", "docker", "make", "linux", "bash", "powershell", "whisper",
    "llm", "postgresql",
]
KEEP_TRANSFERABLE = [
    "troubleshooting", "git", "ci/cd", "itsm", "networking", "vpn", "pandas",
    "asyncio",
]

# --- новое: (навык, уровень, источник) --------------------------------------
NEW: list[tuple[str, str, str]] = [
    # AI / LLM — резюме, продакшен
    ("openai", "PROJECT", "resume: OpenAI API, интегрирован в CRM Sayama"),
    ("gemini", "PROJECT", "resume: Google Gemini API в корп-чатах"),
    ("anthropic", "PROJECT", "resume: Anthropic API в core skills"),
    ("rag", "PROJECT", "resume: RAG в core skills и пайплайнах"),
    ("prompt engineering", "PROJECT", "resume: prompt engineering"),
    ("summarization", "PROJECT", "resume: AI summarization"),
    ("transcription", "PROJECT", "resume: Whisper-транскрипция"),
    ("content generation", "PROJECT", "resume: content generation"),
    ("classification", "PROJECT", "resume: lead classification"),
    ("fact-checking", "PROJECT", "resume: fact-checking"),
    ("ai integration", "PROJECT", "resume: AI Integration & Automation"),
    # Telegram-стек
    ("telethon", "PROJECT", "resume: Telethon/MTProto пайплайны"),
    ("aiogram", "PROJECT", "resume: Telegram-боты на aiogram"),
    ("telegram bot api", "PROJECT", "resume: Telegram Bot API"),
    ("mtproto", "PROJECT", "resume: MTProto"),
    # поддержка / Bitrix24
    ("bitrix24", "PROFESSIONAL", "resume: 2021-2026, self-hosted Bitrix24"),
    ("technical support", "PROFESSIONAL", "resume: техподдержка бизнес-пользователей"),
    ("troubleshooting", "PROFESSIONAL", "resume: troubleshooting"),
    ("incident management", "PROJECT", "resume: техподдержка/ITSM"),
    ("it infrastructure", "PROFESSIONAL", "resume: 2009-2018 IT-инфраструктура"),
    ("itsm", "PROJECT", "resume: ITSM-системы"),
    ("service desk", "PROJECT", "resume: техподдержка"),
    ("crm", "PROFESSIONAL", "resume: Bitrix24 CRM, интеграции"),
    ("telephony", "PROJECT", "resume: IP-телефония, телефония в Bitrix24"),
    ("email", "PROJECT", "resume: email в Bitrix24"),
    ("hardware", "PROJECT", "resume: 2009-2018 IT-инфраструктура"),
    # Windows / инфраструктура
    ("windows server", "PROFESSIONAL", "resume: Windows Server, 9 лет"),
    ("exchange", "PROFESSIONAL", "письмо Ланкею: почтовые ящики Exchange"),
    ("hyper-v", "PROJECT", "resume: Hyper-V в инфраструктуре"),
    ("monitoring", "PROJECT", "resume: monitoring, systemd, логи"),
    ("backups", "PROJECT", "resume: maintained backups"),
    ("network security", "PROJECT", "resume: network security"),
    ("systemd", "PROJECT", "resume: systemd services"),
    ("api testing", "PROJECT", "resume: API/web-service testing"),
    # данные
    ("mssql", "PROJECT", "resume: MS SQL"),
    ("etl", "PROJECT", "resume: asynchronous Python ETL pipelines"),
    ("google workspace apis", "PROJECT", "resume: Google Workspace APIs"),
    ("google sheets", "PROJECT", "resume: Google Sheets в n8n"),
    ("google drive", "PROJECT", "resume: Google Drive в медиапайплайнах"),
    # офисный пакет — из отправленного письма Ланкею
    ("ms office", "PROFESSIONAL", "письмо Ланкею: регулярная задача 10+ лет"),
    ("outlook", "PROFESSIONAL", "письмо Ланкею: настройка Outlook"),
    ("office 365", "PROJECT", "письмо Ланкею: M365"),
    ("exchange online", "PROJECT", "письмо Ланкею: Exchange/M365"),
    ("imap", "PROJECT", "письмо Ланкею: IMAP/POP3"),
    ("gpo", "PROJECT", "письмо Ланкею: политики через GPO"),
    ("autodiscover", "PROJECT", "письмо Ланкею: Autodiscover"),
    ("gal", "PROJECT", "письмо Ланкею: адресные книги GAL"),
    # «разбираюсь, но не глубоко» — прямое основание, не повседневно
    ("excel", "BASIC", "10 лет MS Office; Google Sheets в резюме"),
    ("zapier", "BASIC", "Make — прямой аналог, он PROFESSIONAL"),
    ("vmware", "BASIC", "Hyper-V в резюме — то же семейство"),
    ("playwright", "BASIC", "его проекты гоняют headless-браузер (CDP)"),
    ("django", "BASIC", "Python-фреймворк рядом с FastAPI"),
    ("virtualization", "BASIC", "Hyper-V, VMware-вопрос от рекрутера"),
    ("onboarding", "BASIC", "IT-онбординг пользователей в поддержке"),
    # смежное — сосед документированного навыка
    ("kubernetes", "TRANSFERABLE", "Docker в резюме — оркестрация контейнеров"),
    ("gitlab", "TRANSFERABLE", "Git в резюме"),
    ("github actions", "TRANSFERABLE", "Git + ci/cd в резюме"),
    ("jenkins", "TRANSFERABLE", "ci/cd в профиле"),
    ("ansible", "TRANSFERABLE", "автоматизация Bash/PowerShell — та же задача"),
    ("gcp", "TRANSFERABLE", "Google Workspace APIs + Gemini API"),
    ("grafana", "TRANSFERABLE", "monitoring в резюме"),
    ("prometheus", "TRANSFERABLE", "monitoring в резюме"),
    ("mongodb", "TRANSFERABLE", "SQL/PostgreSQL/MS SQL — сосед по классу"),
    ("clickhouse", "TRANSFERABLE", "SQL/PostgreSQL/MS SQL — сосед по классу"),
    ("aws", "TRANSFERABLE", "GCP-VPS + Docker/Linux — сосед по классу облака"),
    ("terraform", "TRANSFERABLE", "ansible/автоматизация инфраструктуры — сосед по классу IaC"),
    # прямое основание из KB: его собственный проект reels_bot
    ("redis", "PROJECT", "candidate_kb: project_stack — ARQ + Redis в reels_bot"),
]

# Смежное подмножество — в transferable_skills, только для читаемости JSON
# и для генератора черновиков (он читает сырой JSON, минуя загрузчик).
TRANSFERABLE_TIER = {"TRANSFERABLE"}


def _patch_preferences(pref: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Новые значения preferences. Возвращает (preferences, добавленные)."""
    conf_before = dict(pref.get("skill_confidence") or {})

    # KEEP_TRANSFERABLE идёт В ОБА списка: в secondary — чтобы матчер его
    # увидел, в transferable — чтобы JSON остался читаемым.
    secondary: list[str] = list(KEEP_SECONDARY) + list(KEEP_TRANSFERABLE)
    transferable: list[str] = list(KEEP_TRANSFERABLE)
    conf: dict[str, str] = {}

    # Уровни уже существующих навыков сохраняем, не переоцениваем.
    for s in KEEP_CORE + KEEP_SECONDARY + KEEP_TRANSFERABLE:
        conf[s] = conf_before.get(s, "PROJECT")

    added: list[tuple[str, str]] = []
    for skill, level, _src in NEW:
        if skill in conf:
            continue
        conf[skill] = level
        added.append((skill, level))
        if skill not in secondary:
            secondary.append(skill)
        if level in TRANSFERABLE_TIER and skill not in transferable:
            transferable.append(skill)

    new_pref = dict(pref)
    new_pref["core_skills"] = KEEP_CORE
    new_pref["skills"] = KEEP_CORE
    new_pref["secondary_skills"] = secondary
    new_pref["transferable_skills"] = transferable
    new_pref["skill_confidence"] = conf
    return new_pref, added


def main() -> int:
    kb = CandidateKnowledgeBase.load(KB_PATH)
    pref_before = dict(kb.preferences)
    sec_before = list(pref_before.get("secondary_skills") or [])

    new_pref, added = _patch_preferences(pref_before)
    if new_pref == pref_before:
        print("preferences уже расширены — KB не меняется.")
    else:
        if not KB_BACKUP.exists():
            shutil.copy2(KB_PATH, KB_BACKUP)
            print(f"бэкап KB: {KB_BACKUP.name}")
        kb.preferences = new_pref
        kb.kb_version += 1
        kb.updated_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        kb.save(KB_PATH)
        print(f"KB обновлена: kb_version -> {kb.kb_version}")

    conf = new_pref["skill_confidence"]
    print(f"core_skills      : {len(new_pref['core_skills'])} (не менялись)")
    print(f"secondary_skills : {len(sec_before)} -> {len(new_pref['secondary_skills'])}")
    print(f"transferable     : {len(new_pref['transferable_skills'])}")
    print(f"skill_confidence : {len(conf)}")
    print(f"уровни: {dict(collections.Counter(conf.values()))}")
    print(f"\nдобавлено {len(added)} навыков:")
    for s, lv in added:
        print(f"   {lv:<12} {s}")

    print("\n--- пересборка профиля из KB ---")
    code = candidate_profile_render_cmd(
        kb_path=str(KB_PATH), profile_path=str(PROFILE_PATH)
    )
    if code != 0:
        print("\nРЕНДЕР НЕ УДАЛСЯ: KB не может собрать профиль. Смотри ошибку выше.")
        return code
    print(f"\nпрофиль: {PROFILE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
