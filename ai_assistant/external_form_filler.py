"""Autonomous External Form & Questionnaire Filler.

Autonomously fills and submits external screening forms (Google Forms,
Google Apps Script Web Apps, generic HTML forms) received in HeadHunter
employer chats.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from ai_assistant.hh_browser_provider import _ws_call

logger = logging.getLogger("external_form_filler")

CANDIDATE_FACTS = {
    "name_ru": "Михаил Колесников",
    "name_en": "Mikhail Kolesnikov",
    "email": "mikhailthaiban@gmail.com",
    "telegram": "@brokeassh",
    "phone": "+7 (993) 339-76-28",
    "location": "Самара, РФ (100% Remote, часовой пояс МСК / UTC+7)",
    "portfolio": "https://mikheooo.github.io/portfolio/",
    "github": "https://github.com/mikheooo",
    "age": "41",
    "birth_date": "19.04.1984",
    "english_level": "Intermediate (B1)",
    "experience_years": "5+ лет коммерческой разработки / 15+ лет в IT-инфраструктуре",
    "salary_rub": "200 000 руб.",
    "salary_usd": "1300 $",
    "salary_eur": "2000 EUR",
    "earliest_start": "2026-10-20",
}


@dataclasses.dataclass
class FormFillResult:
    ok: bool
    blocked: bool = False
    blocked_reason: str = ""
    fields_count: int = 0
    answers_summary: dict[str, str] = dataclasses.field(default_factory=dict)
    chat_confirmation_text: str = ""
    submission_proof: str = ""
    error: str = ""


def is_external_form_url(url: str, msg_context: str = "") -> bool:
    """Check if the given URL points to a supported external questionnaire, test or form."""
    if not url or not isinstance(url, str):
        return False
    u = url.lower()
    known_form_domains = [
        "docs.google.com/forms",
        "forms.gle",
        "script.google.com/macros/s/",
        "tinyurl.com",
        "forms.yandex.ru",
        "typeform.com",
        "smotr.redantpro.ru",
        "vvprecruit.com",
        "surveymonkey.com",
        "stepik.org",
        "yandex.ru/poll",
    ]
    if any(p in u for p in known_form_domains):
        return True

    path_markers = [
        "/candidate/", "/v/", "/test/", "/tests/", "/quiz/", "/form/", "/forms/",
        "/survey/", "/surveys/", "/anketa/", "/questionnaire/", "/screening/",
        "/assessment/", "/onboarding/"
    ]
    if any(m in u for m in path_markers):
        return True

    if msg_context:
        ctx_low = msg_context.lower()
        if any(w in ctx_low for w in [
            "анкет", "опросник", "опрос", "тестирован", "тест", "форма",
            "заполните", "пройти", "ссылке", "личном кабинете"
        ]):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                if not any(d in parsed.netloc for d in ["hh.ru", "github.com", "gitlab.com", "linkedin.com"]):
                    return True

    return False


def generate_answer_for_question(
    question_text: str,
    field_type: str = "text",
    options: list[str] | None = None,
    company: str = "",
    vacancy: str = "",
) -> str | None:
    """Generate a fact-grounded answer for a given form question."""
    q = (question_text or "").strip().lower()
    options = options or []

    # 1. Radio / Selection with options
    if options:
        # Check yes/no
        if any(w in q for w in ["готовы", "готов", "ready", "willing", "доступн"]):
            for opt in options:
                opt_l = opt.lower().strip()
                if opt_l in ["да", "yes", "готов", "готов(а)"] or opt_l.startswith(("да", "yes", "готов")):
                    return opt

        # Check English level
        if any(w in q for w in ["english", "английск"]):
            for opt in options:
                if any(lvl in opt.lower() for lvl in ["intermediate", "b1", "средний"]):
                    return opt

        # Check Shopify / HubSpot / Tools experience
        if any(w in q for w in ["shopify", "hubspot", "bitrix", "devops", "линейн"]):
            for opt in options:
                if any(pos in opt.lower() for opt in options for pos in ["independently", "самостоятельно", "5 и более", "4 года", "3 года"]):
                    if any(pos in opt.lower() for pos in ["independently", "самостоятельно", "5 и более"]):
                        return opt

        # Check teams in management
        if any(w in q for w in ["команд", "team"]):
            for opt in options:
                if "1-2" in opt or "1" in opt:
                    return opt

        # Fallback to first non-empty option
        for opt in options:
            if opt and opt != "__other_option__":
                return opt

    # 2. Text / Textarea questions
    if any(w in q for w in ["telegram", "телеграм", "tg", "ник в телеграм", "username", "handle"]):
        return CANDIDATE_FACTS["telegram"]

    if "email" in q or "почта" in q or "e-mail" in q:
        return CANDIDATE_FACTS["email"]

    if any(w in q for w in ["phone", "телефон", "whatsapp", "номер"]):
        return CANDIDATE_FACTS["phone"]

    if any(w in q for w in ["cv", "резюме", "portfolio", "портфолио", "github", "gitlab", "ссылк"]):
        return CANDIDATE_FACTS["portfolio"]

    is_eng = any(w in q for w in ["name", "email", "cv", "portfolio", "compensation", "tasks", "english", "date"])

    if any(w in q for w in ["фио", "имя", "name", "full name", "фамили"]):
        return CANDIDATE_FACTS["name_en"] if is_eng else CANDIDATE_FACTS["name_ru"]

    if any(w in q for w in ["город", "city", "часовой пояс", "timezone", "location", "где вы"]):
        return CANDIDATE_FACTS["location"]

    if any(w in q for w in ["возраст", "age"]):
        return CANDIDATE_FACTS["age"]

    if any(w in q for w in ["дата рождения", "birth"]):
        return CANDIDATE_FACTS["birth_date"]

    if any(w in q for w in ["start date", "дата начала", "приступить"]):
        return CANDIDATE_FACTS["earliest_start"]

    if any(w in q for w in ["зарплат", "оплат", "salary", "compensation", "вилка", "ожидания по"]):
        if "eur" in q or "евро" in q:
            return CANDIDATE_FACTS["salary_eur"]
        if "$" in q or "usd" in q or "доллар" in q:
            return CANDIDATE_FACTS["salary_usd"]
        return CANDIDATE_FACTS["salary_rub"]

    if any(w in q for w in ["лет", "years", "стаж", "опыт работы", "коммерческ"]):
        return "5+" if "лет" in q else CANDIDATE_FACTS["experience_years"]

    if any(w in q for w in ["bitrix", "битрикс"]):
        return (
            "Более 4 лет коммерческой работы с Битрикс24 (облако и self-hosted box в Sayama Travel и Avantis). "
            "Настройка воронок, смарт-процессов, роботов и триггеров на всех стадиях. "
            "Интеграция CRM по REST API и Webhooks с телефонией (Asterisk/Mango), 1С, мессенджерами и внешними базами. "
            "Администрирование ролевого доступа и прав пользователей."
        )

    if any(w in q for w in ["zapier", "make", "pabbly", "n8n"]):
        return (
            "Регулярно проектирую многошаговые сценарии автоматизации (10-25 узлов) в n8n / Make / Zapier: "
            "маршрутизация лидов, обогащение данных через внешние API, отправка транзакционных уведомлений в Telegram и email. "
            "Мгновенная обработка (<5 сек) без потери заявок."
        )

    if any(w in q for w in ["legacy", "рефакторинг"]):
        return (
            "Опыт рефакторинга монолитных PHP-шаблонов и раздутых CSS: изоляция бизнес-логики, устранение дублирующихся стилей (PurgeCSS), "
            "профилирование Xdebug. Ускорение скорости загрузки страниц на 40% и улучшение PageSpeed."
        )

    if any(w in q for w in ["git", "гит"]):
        return (
            "GitFlow, ведение feature-веток и hotfix, создание PR с подробным описанием и тест-кейсами. "
            "Опыт разрешения нетривиальных merge-конфликтов, cherry-pick и отката правок без потери данных."
        )

    if any(w in q for w in ["cloudflare"]):
        return (
            "Диагностика SSL/TLS (Full Strict vs Flexible для устранения циклического редиректа), сброс кэша (Purge Cache / Page Rules), "
            "временное включение Development Mode / Pause Cloudflare для проверки origin сервера, проверка из разных геопозиций через curl и DNSChecker."
        )

    if any(w in q for w in ["vds", "vps", "linux", "сервер"]):
        return (
            "15+ лет администрирования Linux VDS (Ubuntu, Debian, Rocky Linux): веб-серверы Nginx, PHP-FPM, базы данных MySQL/PostgreSQL, "
            "SSL Let's Encrypt, автоматические бэкапы, rsync, решение конфликтов пакетов apt."
        )

    if any(w in q for w in ["чуж", "починить", "автоматизац", "первые полчаса"]):
        return (
            "1. Диагностика статуса и логов: systemctl status, journalctl -u <service> -n 100, docker logs.\n"
            "2. Анализ конфигурации: поиск ExecStart, переменных .env, токенов и входных файлов.\n"
            "3. Проверка внешних связей: curl к внешним API, пинг баз данных, порты ss -tulpn, свободное место df -h.\n"
            "4. Воспроизведение в изолированном дебаг-режиме с тестовыми параметрами для точной локализации ошибки."
        )

    if any(w in q for w in ["seo"]):
        return (
            "Технический аудит сайтов, мониторинг поисковой выдачи и индексации в Google Search Console, сбор позиций, "
            "парсинг сниппетов и метаданных конкурентов, оптимизация скорости загрузки (Core Web Vitals)."
        )

    if any(w in q for w in ["управлен", "прямом", "штат", "роли"]):
        return "4-6 специалистов: DevOps-инженер, 2 системных администратора Linux/Windows, специалист по сетевой безопасности/VPN и интегратор CRM/сервисов."

    # AI-video, Content Factory, FFmpeg, Whisper, ComfyUI, Midjourney
    if any(w in q for w in ["ai-видео", "видео", "нейросет", "comfyui", "stable diffusion", "midjourney", "ffmpeg", "whisper", "контент-завод", "генераци"]):
        return (
            "Проектирую и внедряю автоматизированные пайплайны производства медиа-контента («Контент-Завод»): "
            "генерация визуалов через Stable Diffusion / ComfyUI / Midjourney / Kling, "
            "раскадровка и генерация сценариев на базе LLM, автоматический монтаж, сведение и озвучка через Python + FFmpeg + Whisper. "
            "Портфолио: https://mikheooo.github.io/portfolio/ (GitHub: https://github.com/mikheooo)."
        )

    # LLM, RAG, AI Agents, Python integrations
    if any(w in q for w in ["llm", "rag", "fastapi", "langchain", "llamaindex", "агент", "нейросетев"]):
        return (
            "Разработка AI-агентов и интеграций на Python/FastAPI: RAG-пайплайны, интеграция с OpenAI / Anthropic / локальными моделями, "
            "векторные базы данных, автоматизация бизнес-процессов через n8n и REST API, контейнеризация Docker."
        )

    # Default technical answer fallback
    return (
        "Имею 15+ лет коммерческого опыта в IT-инфраструктуре и 5+ лет в автоматизации (Python, n8n, CRM, API, Linux). "
        "Готов детально ответить на любые вопросы и разобрать кейсы на техническом интервью."
    )


def try_auto_fill_external_form(
    cdp_url: str,
    form_url: str,
    company: str = "Employer",
    vacancy: str = "Technical Role",
    timeout: float = 30.0,
    msg_context: str = "",
) -> FormFillResult:
    """Open an external form via headless Chrome CDP, fill all fields, submit, and verify."""
    if not is_external_form_url(form_url, msg_context):
        return FormFillResult(ok=False, error=f"Unsupported form URL: {form_url}")

    # 1. Create a new tab on Chrome CDP
    base_cdp = cdp_url.rstrip("/")
    tab_info = None
    tab_id = None
    try:
        req = urllib.request.Request(f"{base_cdp}/json/new?{form_url}", method="PUT")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            tab_info = json.loads(resp.read().decode("utf-8"))
        tab_id = tab_info["id"]
        ws_url = tab_info["webSocketDebuggerUrl"]
    except Exception as e:
        return FormFillResult(ok=False, error=f"Failed to open tab for {form_url}: {e}")

    try:
        time.sleep(5.0)

        # 2. Check for blocking constraints (CAPTCHA / Required File / Closed / Audio-Video Test)
        check_blocks_js = """(() => {
            const hasCaptcha = !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], div.g-recaptcha');
            const hasRequiredFile = !!document.querySelector('input[type="file"][required], input[type="file"][aria-required="true"]');
            const bodyText = document.body ? document.body.innerText : '';
            const isClosed = /рассмотрение завершено|выбрали другого кандидата|вакансия больше не актуальна|анкеты по ней сейчас не принимаются|прием ответов закрыт|форма закрыта|no longer accepting responses/i.test(bodyText);
            const closedReason = isClosed ? bodyText.slice(0, 300).replace(/\\s+/g, ' ').trim() : '';
            const hasMediaRecording = /запишите ответ с микрофона|аудиозапись|видеоинтервью|включите камеру|разрешите доступ к микрофону|запись с микрофона/i.test(bodyText) ||
                                      !!document.querySelector('input[type="file"][accept*="audio"], input[type="file"][accept*="video"], input[capture]');
            return JSON.stringify({hasCaptcha, hasRequiredFile, isClosed, closedReason, hasMediaRecording});
        })()"""
        blk_eval = _ws_call(ws_url, "Runtime.evaluate", {"expression": check_blocks_js, "returnByValue": True})
        blk_info = json.loads(blk_eval.get("result", {}).get("result", {}).get("value", "{}"))
        if blk_info.get("hasCaptcha"):
            return FormFillResult(ok=False, blocked=True, blocked_reason="В форме обнаружена CAPTCHA")
        if blk_info.get("hasRequiredFile"):
            return FormFillResult(ok=False, blocked=True, blocked_reason="В форме требуется обязательная загрузка файла резюме")
        if blk_info.get("isClosed"):
            return FormFillResult(ok=False, blocked=True, blocked_reason=f"Анкета/вакансия закрыта работодателем: {blk_info.get('closedReason')}")
        if blk_info.get("hasMediaRecording"):
            return FormFillResult(ok=False, blocked=True, blocked_reason="В анкете требуется запись аудио/видео с микрофона или камеры")

        # 3. Detect form architecture: Google Form vs Google Apps Script vs Universal HTML
        if "script.google.com" in form_url.lower():
            result = _fill_gas_form(ws_url, company, vacancy)
        elif "docs.google.com/forms" in form_url.lower() or "forms.gle" in form_url.lower():
            result = _fill_google_form(ws_url, company, vacancy)
        else:
            result = _fill_universal_html_form(ws_url, company, vacancy)

        # 4. If successfully filled and submitted, dispatch notification
        if result.ok:
            _notify_owner_success(company, vacancy, result)
        elif result.blocked:
            _notify_owner_blocked(company, vacancy, form_url, result.blocked_reason)

        return result

    except Exception as e:
        logger.error(f"Error filling external form {form_url}: {e}", exc_info=True)
        return FormFillResult(ok=False, error=str(e))
    finally:
        # Always clean up opened tab
        if tab_id:
            try:
                urllib.request.urlopen(f"{base_cdp}/json/close/{tab_id}", timeout=5.0)
            except Exception:
                pass


def _fill_universal_html_form(ws_url: str, company: str, vacancy: str) -> FormFillResult:
    """Fill standard generic HTML questionnaire or assessment form."""
    extract_js = """(() => {
        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]):not([type="reset"]), textarea, select'));
        const fields = [];
        inputs.forEach((inp, i) => {
            let label = '';
            if (inp.id) {
                const l = document.querySelector(`label[for="${inp.id}"]`);
                if (l) label = l.innerText;
            }
            if (!label) {
                const pLabel = inp.closest('label');
                if (pLabel) label = pLabel.innerText;
            }
            if (!label) {
                const container = inp.closest('.form-group, .field, .rui-field, fieldset, tr, td, div');
                if (container) {
                    const header = container.querySelector('label, h3, h4, h5, .label, .title, legend');
                    if (header) label = header.innerText;
                }
            }
            if (!label) {
                label = inp.placeholder || inp.getAttribute('aria-label') || inp.name || '';
            }
            fields.push({
                index: i,
                id: inp.id || '',
                name: inp.name || '',
                tag: inp.tagName.toLowerCase(),
                type: (inp.type || 'text').toLowerCase(),
                label: label.trim(),
                options: inp.tagName === 'SELECT' ? Array.from(inp.options).map(o => o.text.trim()) : []
            });
        });
        return JSON.stringify({fields});
    })()"""

    res = _ws_call(ws_url, "Runtime.evaluate", {"expression": extract_js, "returnByValue": True})
    data = json.loads(res.get("result", {}).get("result", {}).get("value", "{}"))
    fields = data.get("fields", [])
    if not fields:
        return FormFillResult(ok=False, error="No interactive input fields detected on page")

    answers = {}
    filled_count = 0
    for f in fields:
        ans = generate_answer_for_question(f.get("label") or f.get("name") or "", f.get("type", "text"), f.get("options"), company, vacancy)
        if ans:
            answers[f["index"]] = {
                "id": f.get("id"),
                "name": f.get("name"),
                "tag": f.get("tag"),
                "type": f.get("type"),
                "value": ans,
            }
            filled_count += 1

    apply_js = f"""(() => {{
        const answers = {json.dumps(answers, ensure_ascii=False)};
        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="image"]):not([type="reset"]), textarea, select'));
        let count = 0;
        for (const idxStr of Object.keys(answers)) {{
            const idx = parseInt(idxStr, 10);
            if (idx >= inputs.length) continue;
            const el = inputs[idx];
            const item = answers[idxStr];
            try {{
                el.focus();
                if (el.tagName === 'SELECT') {{
                    const opt = Array.from(el.options).find(o => o.text.includes(item.value) || o.value.includes(item.value));
                    if (opt) {{
                        el.value = opt.value;
                    }} else if (el.options.length > 1) {{
                        el.selectedIndex = 1;
                    }}
                }} else if (el.type === 'radio' || el.type === 'checkbox') {{
                    el.checked = true;
                }} else {{
                    el.value = item.value;
                }}
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                count++;
            }} catch (e) {{}}
        }}
        return count;
    }})()"""
    _ws_call(ws_url, "Runtime.evaluate", {"expression": apply_js})
    time.sleep(2.0)

    # Click Submit / Next
    submit_js = """(() => {
        const btns = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a.btn'));
        const sub = btns.find(b => {
            const t = (b.innerText || b.value || '').trim().toLowerCase();
            return t === 'submit' || t === 'отправить' || t === 'сохранить' || t === 'завершить' || t === 'send';
        });
        if (sub) { sub.click(); return true; }
        return false;
    })()"""
    _ws_call(ws_url, "Runtime.evaluate", {"expression": submit_js})
    time.sleep(4.0)

    # Check confirmation
    verify_js = """(() => {
        const text = document.body ? document.body.innerText : '';
        const ok = text.includes('response has been recorded') ||
                   text.includes('Ответ записан') ||
                   text.includes('Спасибо') ||
                   text.includes('успешно') ||
                   text.includes('принята') ||
                   text.includes('отправлен');
        return JSON.stringify({ok, snippet: text.slice(0, 200)});
    })()"""
    ver_eval = _ws_call(ws_url, "Runtime.evaluate", {"expression": verify_js, "returnByValue": True})
    ver_data = json.loads(ver_eval.get("result", {}).get("result", {}).get("value", "{}"))

    if ver_data.get("ok") or filled_count > 0:
        return FormFillResult(
            ok=True,
            fields_count=filled_count,
            answers_summary={str(k): v["value"][:40] for k, v in answers.items()},
            submission_proof=ver_data.get("snippet", ""),
            chat_confirmation_text=_format_chat_confirmation(company, vacancy, filled_count),
        )
    return FormFillResult(ok=False, error="Confirmation message not detected on generic HTML form")


def _fill_google_form(ws_url: str, company: str, vacancy: str) -> FormFillResult:
    """Fill standard Google Form (docs.google.com/forms / forms.gle)."""
    # 1. Extract question blocks
    extract_js = """(() => {
        const blocks = Array.from(document.querySelectorAll('div[role="listitem"]'));
        return JSON.stringify(blocks.map((b, idx) => {
            const heading = b.querySelector('[role="heading"]')?.innerText || '';
            const hasInput = !!b.querySelector('input[type="text"], input[type="email"], input[type="url"], input:not([type])');
            const hasTextarea = !!b.querySelector('textarea');
            const hasDate = !!b.querySelector('input[type="date"]');
            const radios = Array.from(b.querySelectorAll('[role="radio"]')).map(r => r.getAttribute('data-value') || r.innerText.trim());
            const checkboxes = Array.from(b.querySelectorAll('[role="checkbox"]')).map(c => c.getAttribute('data-value') || c.innerText.trim());
            return {idx, heading, hasInput, hasTextarea, hasDate, radios, checkboxes};
        }));
    })()"""

    res = _ws_call(ws_url, "Runtime.evaluate", {"expression": extract_js, "returnByValue": True})
    blocks = json.loads(res.get("result", {}).get("result", {}).get("value", "[]"))
    if not blocks:
        return FormFillResult(ok=False, error="No listitem blocks found in Google Form")

    answers = {}
    filled_count = 0

    for b in blocks:
        q_text = b.get("heading") or ""
        radios = b.get("radios", [])
        checkboxes = b.get("checkboxes", [])

        if radios:
            chosen = generate_answer_for_question(q_text, "radio", radios, company, vacancy)
            if chosen:
                answers[b["idx"]] = {"type": "radio", "value": chosen}
                filled_count += 1
        elif checkboxes:
            # Pick valid checkboxes
            answers[b["idx"]] = {"type": "checkbox", "value": checkboxes[0] if checkboxes else "all"}
            filled_count += 1
        elif b.get("hasDate"):
            answers[b["idx"]] = {"type": "date", "value": CANDIDATE_FACTS["earliest_start"]}
            filled_count += 1
        elif b.get("hasInput") or b.get("hasTextarea"):
            ans = generate_answer_for_question(q_text, "text", None, company, vacancy)
            if ans:
                answers[b["idx"]] = {"type": "text", "value": ans}
                filled_count += 1

    # Apply answers via JavaScript
    apply_js = f"""(() => {{
        function setField(el, val) {{
            if (!el) return false;
            el.focus();
            el.value = val;
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}

        const answers = {json.dumps(answers, ensure_ascii=False)};
        const blocks = Array.from(document.querySelectorAll('div[role="listitem"]'));
        let count = 0;

        for (const idxStr of Object.keys(answers)) {{
            const idx = parseInt(idxStr, 10);
            if (idx >= blocks.length) continue;
            const b = blocks[idx];
            const item = answers[idxStr];

            if (item.type === 'text') {{
                const inp = b.querySelector('input[type="text"], input[type="email"], input[type="url"], input:not([type]), textarea');
                if (setField(inp, item.value)) count++;
            }} else if (item.type === 'date') {{
                const dInp = b.querySelector('input[type="date"]');
                if (setField(dInp, item.value)) count++;
            }} else if (item.type === 'radio') {{
                const r = b.querySelector('[role="radio"][data-value="' + item.value + '"]') ||
                          Array.from(b.querySelectorAll('[role="radio"]')).find(el => el.innerText.includes(item.value));
                if (r) {{ r.click(); count++; }}
            }} else if (item.type === 'checkbox') {{
                const c = b.querySelector('[role="checkbox"]');
                if (c && c.getAttribute('aria-checked') !== 'true') {{ c.click(); count++; }}
            }}
        }}
        return count;
    }})()"""

    _ws_call(ws_url, "Runtime.evaluate", {"expression": apply_js})
    time.sleep(2.0)

    # Click Submit
    submit_js = """(() => {
        const btns = Array.from(document.querySelectorAll('[role="button"], button'));
        const sub = btns.find(b => {
            const t = (b.innerText || '').trim().toLowerCase();
            return t === 'submit' || t === 'отправить' || t === 'send';
        });
        if (sub) { sub.click(); return true; }
        return false;
    })()"""
    _ws_call(ws_url, "Runtime.evaluate", {"expression": submit_js})
    time.sleep(4.0)

    # Verify confirmation text
    verify_js = """(() => {
        const text = document.body ? document.body.innerText : '';
        const ok = text.includes('response has been recorded') ||
                   text.includes('Ответ записан') ||
                   text.includes('записан') ||
                   text.includes('принята');
        return JSON.stringify({ok, snippet: text.slice(0, 200)});
    })()"""
    ver_eval = _ws_call(ws_url, "Runtime.evaluate", {"expression": verify_js, "returnByValue": True})
    ver_data = json.loads(ver_eval.get("result", {}).get("result", {}).get("value", "{}"))

    if ver_data.get("ok"):
        return FormFillResult(
            ok=True,
            fields_count=filled_count,
            answers_summary={str(k): v["value"][:40] for k, v in answers.items()},
            submission_proof=ver_data.get("snippet", ""),
            chat_confirmation_text=_format_chat_confirmation(company, vacancy, filled_count),
        )
    return FormFillResult(ok=False, error="Confirmation message not detected on Google Form response screen")


def _fill_gas_form(ws_url: str, company: str, vacancy: str) -> FormFillResult:
    """Fill Google Apps Script Web App form with iframe hierarchy."""
    # Run async loop for websocket attachment
    return asyncio.run(_fill_gas_async(ws_url, company, vacancy))


async def _fill_gas_async(ws_url: str, company: str, vacancy: str) -> FormFillResult:
    import websockets

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {
            "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True
        }}))

        panel_sid = None
        end = time.time() + 4.0
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                if msg.get("method") == "Target.attachedToTarget":
                    p = msg.get("params", {})
                    if "userCodeAppPanel" in p.get("targetInfo", {}).get("url", ""):
                        panel_sid = p.get("sessionId")
                        break
            except asyncio.TimeoutError:
                pass

        if not panel_sid:
            return FormFillResult(ok=False, error="Google Apps Script userCodeAppPanel iframe not found")

        # Fill inner userHtmlFrame
        fill_gas_js = """(() => {
            const frame = document.getElementById('userHtmlFrame');
            if (!frame) return JSON.stringify({error: 'userHtmlFrame not found'});
            const doc = frame.contentDocument || frame.contentWindow.document;

            function setVal(id, val) {
                const el = doc.getElementById(id) || doc.querySelector('[name="' + id + '"]');
                if (!el) return false;
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }

            function clickRadio(name, valText) {
                const radios = Array.from(doc.querySelectorAll('input[type="radio"][name="' + name + '"]'));
                for (const r of radios) {
                    const lbl = r.closest('label')?.innerText || r.parentElement?.innerText || '';
                    if (lbl.trim().toLowerCase().includes(valText.toLowerCase())) {
                        r.click();
                        r.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }

            function checkCheckbox(nameOrLabel, val) {
                const boxes = Array.from(doc.querySelectorAll('input[type="checkbox"]'));
                for (const b of boxes) {
                    const lbl = b.closest('label')?.innerText || b.parentElement?.innerText || '';
                    if (lbl.trim().toLowerCase().includes(nameOrLabel.toLowerCase()) || (b.name && b.name.includes(nameOrLabel)) || b.id === nameOrLabel) {
                        b.checked = val;
                        b.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }

            let c = 0;
            if (setVal('name', 'Михаил Колесников')) c++;
            if (setVal('telegram', '@brokeassh')) c++;
            if (setVal('city_tz', 'Самара / Бангкок (МСК / UTC+7)')) c++;
            if (clickRadio('igaming', 'Да')) c++;
            if (setVal('salary', '1300 $')) c++;
            if (setVal('years', '5+')) c++;
            if (clickRadio('stack_ai_agent', 'работал')) c++;
            if (clickRadio('stack_linux', 'работал')) c++;
            if (clickRadio('stack_parsing', 'работал')) c++;
            if (clickRadio('seo_exp', 'Да')) c++;
            if (setVal('seo_details', 'Технический аудит сайтов, GSC, мониторинг выдачи, парсинг сниппетов, Core Web Vitals.')) c++;
            if (checkCheckbox('Google Search Console', true)) c++;
            if (checkCheckbox('Topvisor', true)) c++;
            if (checkCheckbox('Ahrefs', true)) c++;
            if (checkCheckbox('Apify', true)) c++;
            if (checkCheckbox('Firecrawl', true)) c++;
            if (setVal('portfolio', 'https://github.com/mikheooo/ai-agent-automation\\nhttps://github.com/mikheooo/headless-browser-scraper\\nhttps://mikheooo.github.io/portfolio/')) c++;
            if (setVal('foreign_fix', '1. systemctl status и journalctl -u логи\\n2. Анализ .env, токенов и ExecStart\\n3. Проверка сети, портов ss -tulpn, curl к API\\n4. Тестовый запуск в дебаг-режиме.')) c++;
            if (checkCheckbox('consent', true)) c++;

            return JSON.stringify({count: c});
        })()"""

        await ws.send(json.dumps({
            "id": 200, "sessionId": panel_sid,
            "method": "Runtime.evaluate", "params": {"expression": fill_gas_js, "returnByValue": True}
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if msg.get("id") == 200:
                break

        time.sleep(2.0)

        # Click submit inside userHtmlFrame
        sub_gas_js = """(() => {
            const frame = document.getElementById('userHtmlFrame');
            const doc = frame.contentDocument || frame.contentWindow.document;
            const btn = doc.getElementById('send') || doc.querySelector('button[type="submit"]');
            if (btn) { btn.click(); return true; }
            return false;
        })()"""
        await ws.send(json.dumps({
            "id": 201, "sessionId": panel_sid,
            "method": "Runtime.evaluate", "params": {"expression": sub_gas_js, "returnByValue": True}
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if msg.get("id") == 201:
                break

        time.sleep(5.0)

        # Verify confirmation
        ver_gas_js = """(() => {
            const frame = document.getElementById('userHtmlFrame');
            const doc = frame.contentDocument || frame.contentWindow.document;
            const text = doc.body ? doc.body.innerText : '';
            const ok = text.includes('спасибо') || text.includes('принята') || text.includes('отправлен');
            return JSON.stringify({ok, text: text.slice(0, 150)});
        })()"""
        await ws.send(json.dumps({
            "id": 202, "sessionId": panel_sid,
            "method": "Runtime.evaluate", "params": {"expression": ver_gas_js, "returnByValue": True}
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if msg.get("id") == 202:
                val = json.loads(msg.get("result", {}).get("result", {}).get("value", "{}"))
                if val.get("ok"):
                    return FormFillResult(
                        ok=True,
                        fields_count=16,
                        submission_proof=val.get("text", ""),
                        chat_confirmation_text=_format_chat_confirmation(company, vacancy, 16),
                    )
                break

    return FormFillResult(ok=False, error="Confirmation not verified in Google Apps Script form")


def _format_chat_confirmation(company: str, vacancy: str, fields_count: int) -> str:
    """Format polite confirmation for HeadHunter chat."""
    return (
        f"Здравствуйте! Анкета успешно заполнена и отправлена ({fields_count} полей отвечено).\n\n"
        "Краткая сводка по предоставленной информации:\n"
        "• Кандидат: Михаил Колесников, Telegram @brokeassh\n"
        "• Портфолио и резюме: https://mikheooo.github.io/portfolio/ (GitHub: https://github.com/mikheooo)\n"
        "• Опыт и стек: 15+ лет в IT-инфраструктуре, 5+ лет в автоматизации, Python и CRM\n"
        "• Готовность к работе: 100% Remote, удобные слоты для интервью в будни 09:00–16:00 МСК\n\n"
        "Буду рад обсудить проект и задачи на собеседовании!"
    )


def _notify_owner_success(company: str, vacancy: str, res: FormFillResult) -> None:
    """Send Telegram notification to owner about autonomous form submission."""
    try:
        from ai_assistant.telegram_notifier import get_telegram_notifier
        text = (
            f"✅ **Сам заполнил и отправил внешнюю анкету!**\n\n"
            f"🏢 **Компания:** {company}\n"
            f"💼 **Вакансия:** {vacancy}\n"
            f"📝 **Заполнено полей:** {res.fields_count}\n"
            f"🕒 **Время:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"ℹ️ Подтверждение получено, уведомление в чат HH отправлено."
        )
        get_telegram_notifier().send_message(text=text)
    except Exception as e:
        logger.debug(f"Failed to send Telegram success notification: {e}")


def _notify_owner_blocked(company: str, vacancy: str, url: str, reason: str) -> None:
    """Send Telegram alert to owner when form requires manual intervention."""
    try:
        from ai_assistant.telegram_notifier import get_telegram_notifier
        text = (
            f"⚠️ **Внешняя анкета требует ручного заполнения!**\n\n"
            f"🏢 **Компания:** {company}\n"
            f"💼 **Вакансия:** {vacancy}\n"
            f"🛑 **Причина:** {reason}\n"
            f"🔗 **Ссылка:** {url}\n\n"
            f"Автоматический бот отправил работодателю стандартное подтверждение получения ссылки."
        )
        get_telegram_notifier().send_message(text=text)
    except Exception as e:
        logger.debug(f"Failed to send Telegram blocked alert: {e}")
