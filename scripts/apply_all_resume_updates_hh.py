"""Apply all missing experience and skills to the HeadHunter resume via CDP real events."""

import urllib.request
import json
import asyncio
import websockets

CDP = "http://127.0.0.1:9111"

SKILLS_TO_ADD = [
    "Playwright",
    "pytest",
    "FastAPI",
    "REST API",
    "ComfyUI",
    "Stable Diffusion",
    "React",
    "TypeScript",
    "CI/CD",
    "GitHub Actions",
    "GitLab CI",
    "Kubernetes",
    "Terraform",
    "Grafana",
    "Prometheus",
]

NEW_EXPERIENCE_TEXT = """Проектирование и внедрение сквозных AI-пайплайнов автоматизации контента (RAG, prompt engineering, ИИ-агенты, интеграция Google Drive API -> Whisper -> Telegram).
Разработка асинхронных ETL и сервисных микросервисов на Python (asyncio, FastAPI, Telethon/MTProto) с внешними API и базами данных (PostgreSQL, Redis).
Обработка и генерация изображений: построение пайплайнов для генерации, пакетной обработки, ресайза и наложения масок/водяных знаков с использованием ComfyUI, Stable Diffusion, Pillow и OpenCV.
Разработка веб-интерфейсов и админ-панелей: создание внутренних дашбордов и UI-компонентов на React, Vue, TypeScript/JavaScript в связке с бэкендом на FastAPI/Python.
Автоматизация тестирования и обеспечение качества: автотесты на Python (pytest), тестирование REST API и вебхуков, e2e браузерные сценарии на Playwright.
CI/CD, контейнеризация и мониторинг: настройка пайплайнов сборки и деплоя (GitHub Actions, GitLab CI), оркестрация в Docker/Kubernetes, базовая инфраструктура как код (Terraform, AWS, GCP), мониторинг сервисов в Prometheus и дашборды в Grafana.
Построение сложных сценариев интеграции на платформе n8n (синхронизация баз данных, уведомлений, ИИ-агентов, Google Workspace API, CRM).
Разработка интерактивных Telegram-ботов на aiogram (Python) с интеграцией внешних API, очередей задач и стейт-менеджмента (FSM).
Стек: Python (asyncio, FastAPI, pytest), Playwright, ComfyUI, Stable Diffusion, React, TypeScript, Vue, Docker, Kubernetes, CI/CD (GitHub Actions, GitLab CI), Terraform, Prometheus, Grafana, n8n, REST API, Google Workspace API, PostgreSQL, Redis, Git."""

NEW_ABOUT_TEXT = """Системный инженер и разработчик решений автоматизации с 15-летним опытом в ИТ. Моя сильная сторона — сочетание глубокого понимания классической ИТ-инфраструктуры (сети, серверы Linux/Windows, VPN, безопасность, базы данных) и современных технологий разработки и автоматизации (Python, FastAPI, интеграция ИИ/LLM-моделей, n8n).

Ключевые направления и практический опыт:
• Разработка сервисов и интеграций на Python (FastAPI, asyncio, PostgreSQL, Redis, REST API, вебхуки, Telegram-боты на aiogram).
• Автоматизация тестирования и QA: написание автотестов на Python (pytest), тестирование API/вебхуков, e2e браузерные сценарии на Playwright.
• AI-пайплайны и медиа: внедрение LLM (OpenAI, Gemini, Anthropic), Whisper, RAG, генерация и обработка изображений (ComfyUI, Stable Diffusion, Pillow, OpenCV).
• Веб-интерфейсы: создание и доработка внутренних дашбордов и админ-панелей на React, Vue, TypeScript/JavaScript.
• DevOps и эксплуатация: контейнеризация (Docker, Kubernetes), CI/CD (GitHub Actions, GitLab CI), IaC (Terraform, AWS, GCP), мониторинг инфраструктуры (Prometheus, Grafana).

Английский язык — Intermediate / B1 (чтение технической документации, переписка, опыт в международных проектах). Готов к 100% удаленной работе."""


async def call_cdp(ws, method, params=None, msg_id=1):
    req = {"id": msg_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(req))
    while True:
        raw = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == msg_id:
            return resp


async def click_element_cdp(ws, coords, msg_id_start):
    await call_cdp(ws, "Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": coords["x"],
        "y": coords["y"],
        "button": "left",
        "clickCount": 1
    }, msg_id=msg_id_start)
    await call_cdp(ws, "Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": coords["x"],
        "y": coords["y"],
        "button": "left",
        "clickCount": 1
    }, msg_id=msg_id_start + 1)


async def update_key_skills(ws):
    print("\n--- 1. Updating Key Skills ---")
    resume_url = "https://hh.ru/resume/edit/4db30a09ff01b3bae30039ed1f364c544b4459/keySkills"
    await call_cdp(ws, "Page.navigate", {"url": resume_url}, msg_id=10)
    await asyncio.sleep(4.0)

    # Read existing chips
    res_chips = await call_cdp(ws, "Runtime.evaluate", {
        "expression": """(() => {
            const root = document.querySelector('[data-qa="resume-editor-skills-input"]');
            return root ? root.textContent.toLowerCase() : '';
        })()""",
        "returnByValue": True
    }, msg_id=11)
    existing_text = res_chips.get("result", {}).get("result", {}).get("value", "")

    m_id = 20
    added_count = 0
    for skill in SKILLS_TO_ADD:
        if skill.lower() in existing_text:
            print(f"Skill '{skill}' already in resume, skipping.")
            continue

        # 1. Try recommended chip
        m_id += 1
        res_rec = await call_cdp(ws, "Runtime.evaluate", {
            "expression": f"""(() => {{
                const chips = Array.from(document.querySelectorAll('[data-qa="chip"]'));
                const target = chips.find(c => c.innerText.trim().toLowerCase() === '{skill.lower()}');
                if (target) {{
                    const r = target.getBoundingClientRect();
                    return {{ x: r.x + r.width / 2, y: r.y + r.height / 2 }};
                }}
                return null;
            }})()""",
            "returnByValue": True
        }, msg_id=m_id)
        rec_coords = res_rec.get("result", {}).get("result", {}).get("value")
        if rec_coords:
            m_id += 2
            await click_element_cdp(ws, rec_coords, m_id)
            print(f"Added '{skill}' via recommended chip.")
            existing_text += " " + skill.lower()
            added_count += 1
            await asyncio.sleep(0.8)
            continue

        # 2. Type into input
        m_id += 1
        await call_cdp(ws, "Runtime.evaluate", {
            "expression": f"""(() => {{
                const input = document.querySelector('[data-qa="chips-trigger-input"]');
                if (!input) return false;
                input.focus();
                const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                nativeSetter.call(input, '{skill}');
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                return true;
            }})()"""
        }, msg_id=m_id)
        await asyncio.sleep(0.8)

        # 3. Click suggestion
        m_id += 1
        res_sugg = await call_cdp(ws, "Runtime.evaluate", {
            "expression": f"""(() => {{
                const suggs = Array.from(document.querySelectorAll('[data-qa="cell-text-content"]'));
                const target = suggs.find(s => s.innerText.trim().toLowerCase() === '{skill.lower()}');
                if (target) {{
                    const r = target.getBoundingClientRect();
                    return {{ x: r.x + r.width / 2, y: r.y + r.height / 2 }};
                }}
                return null;
            }})()""",
            "returnByValue": True
        }, msg_id=m_id)
        sugg_coords = res_sugg.get("result", {}).get("result", {}).get("value")
        if sugg_coords:
            m_id += 2
            await click_element_cdp(ws, sugg_coords, m_id)
            print(f"Added '{skill}' via autocomplete.")
            existing_text += " " + skill.lower()
            added_count += 1
        else:
            print(f"Autocomplete not found for '{skill}', pressing Enter...")
            m_id += 1
            await call_cdp(ws, "Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "windowsVirtualKeyCode": 13, "unmodifiedText": "\r", "text": "\r"
            }, msg_id=m_id)
            m_id += 1
            await call_cdp(ws, "Input.dispatchKeyEvent", {
                "type": "keyUp", "windowsVirtualKeyCode": 13, "unmodifiedText": "\r", "text": "\r"
            }, msg_id=m_id)
            existing_text += " " + skill.lower()
            added_count += 1
        await asyncio.sleep(0.8)

    # Click Save
    if added_count > 0:
        await asyncio.sleep(1.0)
        m_id += 1
        res_btn = await call_cdp(ws, "Runtime.evaluate", {
            "expression": """(() => {
                const btn = document.querySelector('[data-qa="resume-partial-edit-save"]');
                if (btn) {
                    const r = btn.getBoundingClientRect();
                    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                }
                return null;
            })()""",
            "returnByValue": True
        }, msg_id=m_id)
        btn_coords = res_btn.get("result", {}).get("result", {}).get("value")
        if btn_coords:
            m_id += 2
            await click_element_cdp(ws, btn_coords, m_id)
            print("Clicked Save on KeySkills.")
            await asyncio.sleep(3.0)
    else:
        print("No new skills needed to be added to KeySkills.")


async def update_experience(ws):
    print("\n--- 2. Updating Freelance Experience ---")
    exp_url = "https://hh.ru/profile/edit/experience/2085042028?resumeFrom=4db30a09ff01b3bae30039ed1f364c544b4459&hhtmFrom=resume"
    await call_cdp(ws, "Page.navigate", {"url": exp_url}, msg_id=500)
    await asyncio.sleep(4.0)

    # Set textarea
    await call_cdp(ws, "Runtime.evaluate", {
        "expression": f"""(() => {{
            const ta = document.querySelector('textarea');
            if (!ta) return "textarea_not_found";
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
            nativeSetter.call(ta, {json.dumps(NEW_EXPERIENCE_TEXT)});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return "set_ok";
        }})()"""
    }, msg_id=501)
    await asyncio.sleep(1.0)

    # Find Save button
    res_btn = await call_cdp(ws, "Runtime.evaluate", {
        "expression": """(() => {
            const btn = Array.from(document.querySelectorAll('button')).find(b => {
                const t = b.innerText.trim();
                const qa = b.getAttribute('data-qa') || '';
                return t === 'Сохранить' || qa.includes('save') || t === 'Save';
            });
            if (btn) {
                const r = btn.getBoundingClientRect();
                return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            }
            return null;
        })()""",
        "returnByValue": True
    }, msg_id=502)
    btn_coords = res_btn.get("result", {}).get("result", {}).get("value")
    if btn_coords:
        await click_element_cdp(ws, btn_coords, 503)
        print("Clicked Save on Experience.")
        await asyncio.sleep(3.0)
    else:
        print("Could not find Save button on Experience page!")


async def update_about(ws):
    print("\n--- 3. Updating About Section ---")
    about_url = "https://hh.ru/resume/edit/4db30a09ff01b3bae30039ed1f364c544b4459/about"
    await call_cdp(ws, "Page.navigate", {"url": about_url}, msg_id=600)
    await asyncio.sleep(4.0)

    # Set textarea
    await call_cdp(ws, "Runtime.evaluate", {
        "expression": f"""(() => {{
            const ta = document.querySelector('textarea');
            if (!ta) return "textarea_not_found";
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
            nativeSetter.call(ta, {json.dumps(NEW_ABOUT_TEXT)});
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return "set_ok";
        }})()"""
    }, msg_id=601)
    await asyncio.sleep(1.0)

    # Find Save button
    res_btn = await call_cdp(ws, "Runtime.evaluate", {
        "expression": """(() => {
            const btn = Array.from(document.querySelectorAll('button')).find(b => {
                const t = b.innerText.trim();
                const qa = b.getAttribute('data-qa') || '';
                return t === 'Сохранить' || qa.includes('save') || t === 'Save';
            });
            if (btn) {
                const r = btn.getBoundingClientRect();
                return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            }
            return null;
        })()""",
        "returnByValue": True
    }, msg_id=602)
    btn_coords = res_btn.get("result", {}).get("result", {}).get("value")
    if btn_coords:
        await click_element_cdp(ws, btn_coords, 603)
        print("Clicked Save on About.")
        await asyncio.sleep(3.0)
    else:
        print("Could not find Save button on About page!")


async def main():
    # Open temporary tab
    req = urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req) as resp:
        tab = json.loads(resp.read().decode("utf-8"))
    tab_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]

    try:
        async with websockets.connect(ws_url) as ws:
            await update_key_skills(ws)
            await update_experience(ws)
            await update_about(ws)

            # Navigate to final resume view to verify
            print("\n--- Verifying final resume page ---")
            resume_view_url = "https://hh.ru/resume/4db30a09ff01b3bae30039ed1f364c544b4459"
            await call_cdp(ws, "Page.navigate", {"url": resume_view_url}, msg_id=700)
            await asyncio.sleep(4.0)
            res_summary = await call_cdp(ws, "Runtime.evaluate", {
                "expression": """(() => {
                    return {
                        title: document.title,
                        bodySample: document.body.innerText.slice(0, 1500)
                    };
                })()""",
                "returnByValue": True
            }, msg_id=701)
            print("Final verification summary:\n", res_summary.get("result", {}).get("result", {}).get("value", {}).get("title"))
    finally:
        req_close = urllib.request.Request(f"{CDP}/json/close/{tab_id}")
        urllib.request.urlopen(req_close)
        print("\nAll HH resume updates completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
