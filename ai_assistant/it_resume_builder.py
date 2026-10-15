"""Russian Enterprise IT CV Generator (AI Automation & Infrastructure Engineer).

Generates a polished, executive-quality A4 Russian CV tailored for Russian and CIS
remote engineering roles (AI / Python Automation, CRM Bitrix24, Infrastructure & Support).

Sourced exclusively from confirmed facts in candidate_kb.json and master_resume.ru.md.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
from typing import Optional

logger = logging.getLogger("it_resume_builder")

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PDF = ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_IT_Engineer.pdf"
DEFAULT_OUTPUT_HTML = ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_IT_Engineer.html"

CHROME_PATHS = (
    pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    pathlib.Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)

RESUME_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Михаил Колесников — AI Automation & IT Infrastructure Engineer</title>
<style>
@page {
  size: A4;
  margin: 11mm 13mm 11mm 13mm;
}
* {
  box-sizing: border-box;
}
html {
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
body {
  margin: 0;
  font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
  font-size: 9.6pt;
  line-height: 1.36;
  color: #212529;
  background: #ffffff;
}

.header {
  border-bottom: 2px solid #0056b3;
  padding-bottom: 6pt;
  margin-bottom: 8pt;
}

.name {
  font-size: 20pt;
  font-weight: 700;
  color: #111827;
  letter-spacing: -0.3pt;
  margin: 0 0 2pt 0;
}

.headline {
  font-size: 11pt;
  font-weight: 600;
  color: #0056b3;
  margin: 0 0 3pt 0;
}

.banner {
  background: #f0f7ff;
  border-left: 3px solid #0056b3;
  padding: 3pt 8pt;
  font-size: 8.8pt;
  font-weight: 600;
  color: #0f3f6d;
  margin-bottom: 4pt;
  border-radius: 2px;
}

.contacts {
  font-size: 8.8pt;
  color: #4b5563;
  display: flex;
  flex-wrap: wrap;
  gap: 10pt;
}

.contacts a {
  color: #0056b3;
  text-decoration: none;
}

section {
  margin-bottom: 8pt;
}

h2 {
  font-size: 10pt;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5pt;
  color: #111827;
  border-bottom: 1px solid #d1d5db;
  padding-bottom: 2pt;
  margin: 0 0 4pt 0;
}

.summary-text {
  font-size: 9.2pt;
  color: #374151;
  text-align: justify;
  margin: 0 0 3pt 0;
}

.skills-grid {
  display: grid;
  grid-template-columns: 145pt 1fr;
  row-gap: 2.5pt;
  font-size: 8.8pt;
  margin-bottom: 3pt;
}

.skill-category {
  font-weight: 600;
  color: #1f2937;
}

.skill-items {
  color: #4b5563;
}

.job {
  margin-bottom: 6pt;
  page-break-inside: avoid;
}

.job-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1pt;
}

.job-title {
  font-size: 9.8pt;
  font-weight: 700;
  color: #111827;
}

.job-company {
  font-weight: 600;
  color: #0056b3;
}

.job-period {
  font-size: 8.6pt;
  font-weight: 500;
  color: #6b7280;
}

ul {
  margin: 1.5pt 0 0 0;
  padding-left: 13pt;
}

li {
  font-size: 8.8pt;
  color: #374151;
  margin-bottom: 1.5pt;
}

li strong {
  color: #1f2937;
}

.edu-item {
  display: flex;
  justify-content: space-between;
  font-size: 8.8pt;
}

.lang-grid {
  font-size: 8.8pt;
  color: #374151;
}
</style>
</head>
<body>

<div class="header">
  <div class="name">Михаил Колесников</div>
  <div class="headline">AI Automation & Python Engineer | Инженер IT-инфраструктуры и CRM</div>
  <div class="banner">
    🌐 100% удаленная работа · РФ / СНГ (часовой пояс МСК / UTC+3) · Оформление ТК РФ / ИП / СЗ · 15+ лет опыта в IT
  </div>
  <div class="contacts">
    <span>✉️ <a href="mailto:mikhailthaiban@gmail.com">mikhailthaiban@gmail.com</a></span>
    <span>📱 Телефон / WhatsApp: +7 (993) 339-76-28</span>
    <span>💬 Telegram: <a href="https://t.me/brokeassh">@brokeassh</a></span>
    <span>💻 GitHub: <a href="https://github.com/mikheooo">github.com/mikheooo</a></span>
  </div>
</div>

<section>
  <h2>О себе / Профессиональный профиль</h2>
  <p class="summary-text">
    IT-инженер с <strong>15+ годами практики в enterprise-инфраструктуре</strong>, автоматизации бизнес-процессов и разработке интеграционных сервисов на Python. Специализируюсь на внедрении современных AI/LLM решений (OpenAI, Gemini, Whisper Speech-to-Text, RAG), сквозной автоматизации CRM-систем (Битрикс24) и поддержке отказоустойчивых Linux/Docker сред. Большой опыт администрирования корпоративных сетей и систем. Полностью удаленный формат работы, готовность к сложным комплексным задачам.
  </p>
</section>

<section>
  <h2>Ключевые навыки</h2>
  <div class="skills-grid">
    <div class="skill-category">AI, LLM & Автоматизация:</div>
    <div class="skill-items">OpenAI API, Google Gemini API, Claude API, Whisper (Speech-to-Text), RAG-пайплайны, Prompt Engineering, n8n, Make, классификация и анализ данных</div>

    <div class="skill-category">Python & Интеграции:</div>
    <div class="skill-items">Python 3 (asyncio, FastAPI, aiohttp, aiogram, Telethon, MTProto), REST APIs, Webhooks, очереди задач (Redis, ARQ), ETL-пайплайны, парсинг данных</div>

    <div class="skill-category">CRM & Базы данных:</div>
    <div class="skill-items">1С-Битрикс24 (On-Premise & Cloud, REST API, бизнес-процессы, роботы, вебхуки, телефония), PostgreSQL, MySQL, MS SQL, Redis, SQLite</div>

    <div class="skill-category">Инфраструктура & DevOps:</div>
    <div class="skill-items">Linux (Ubuntu, Debian, RHEL), Docker, Docker Compose, Nginx (Reverse Proxy, SSL), Systemd, CI/CD, WireGuard VPN, OpenVPN, бэкапы</div>

    <div class="skill-category">Сети & Системное администрирование:</div>
    <div class="skill-items">Windows Server (AD DS, GPO, DNS, DHCP, Hyper-V, Exchange), TCP/IP, VLAN, Cisco, Mikrotik RouterOS, IP-телефония (SIP / Asterisk), ITSM / Service Desk</div>
  </div>
</section>

<section>
  <h2>Опыт работы</h2>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Sayama Travel</span> — AI Automation & IT Infrastructure Engineer</div>
      <div class="job-period">Ноябрь 2022 – Апрель 2026 (3 года 6 месяцев)</div>
    </div>
    <ul>
      <li>Построил сквозной аудио-пайплайн: транскрибация голосовых сообщений клиентов через <strong>OpenAI Whisper</strong>, последующая LLM-суммаризация и автопривязка структурированных заметок в сделки Битрикс24.</li>
      <li>Интегрировал <strong>OpenAI и Google Gemini API</strong> в корпоративные каналы коммуникации и CRM: классификация входящих лидов, интеллектуальная маршрутизация и генерация контекстных ответов.</li>
      <li>Разрабатывал асинхронные <strong>Telegram-боты и микросервисы на Python</strong> (aiogram, asyncio, Telethon) для автоматической обработки клиентских заявок и передачи медиа.</li>
      <li>Оркестрировал комплексные интеграционные воркфлоу в <strong>n8n</strong>, связавшие Битрикс24, Google Workspace APIs, внешние базы данных и системы уведомлений.</li>
      <li>Администрировал серверный стек <strong>Linux и Docker</strong>, обеспечивал высокую доступность сервисов (99.9%), резервное копирование и безопасный межфилиальный WireGuard VPN.</li>
    </ul>
  </div>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Avantis</span> — Специалист по внедрению и сопровождению Битрикс24 / CRM</div>
      <div class="job-period">Москва | Апрель 2021 – Август 2022 (1 год 5 месяцев)</div>
    </div>
    <ul>
      <li>Администрировал коробочный Битрикс24 (self-hosted Linux); настраивал автоматические воркфлоу, роботов, триггеры, распределение прав и REST API интеграции.</li>
      <li>Оказывал L2/L3 техническую поддержку пользователям, обучал сотрудников и формировал регламенты работы с CRM-системой.</li>
    </ul>
  </div>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Speedcast / Germsat · Intravision · Почта России</span> — Системный администратор enterprise-инфраструктуры</div>
      <div class="job-period">2009 – 2021 (12 лет)</div>
    </div>
    <ul>
      <li>Администрировал корпоративную IT-инфраструктуру: серверы Windows Server (Active Directory, Group Policy, DNS, DHCP), Hyper-V, MS Exchange, Linux.</li>
      <li>Настраивал сетевую инфраструктуру (маршрутизаторы и коммутаторы Cisco, Mikrotik), межсетевые экраны, VLAN, VPN-туннели и спутниковые каналы связи (Speedcast).</li>
      <li>Поддерживал серверы реляционных СУБД (MS SQL, PostgreSQL), автоматизировал рутинные сценарии администрирования на PowerShell и Bash.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Ключевые технические проекты</h2>
  <div class="job">
    <ul>
      <li><strong>Autonomous AI Job Search Assistant (Python / SQLite / Playwright / LLM):</strong> полнофункциональная агентная система автоматизации поиска работы, анализа технического стека вакансий, генерации кастомизированных сопроводительных писем и безопасного сабмита откликов.</li>
      <li><strong>Reels Content Analyzer (Python 3.13 / Redis / ARQ / PostgreSQL / Docker / Gemini API):</strong> асинхронный воркер-пайплайн анализа медиаконтента: извлечение аудиодорожки, транскрибация, факт-чекинг утверждений через поисковые API и формирование markdown-отчетов.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Образование и языки</h2>
  <div class="edu-item">
    <div><strong>Сахалинский государственный университет</strong> — Технологический институт, технология и предпринимательство (высшее образование)</div>
    <div>2005</div>
  </div>
  <div class="lang-grid" style="margin-top: 3pt;">
    <strong>Языки:</strong> Русский — родной · Английский — B1 (чтение технической документации, профессиональная переписка)
  </div>
</section>

</body>
</html>
"""


def get_chrome_executable() -> pathlib.Path:
    """Find local Google Chrome executable."""
    for path in CHROME_PATHS:
        if path.exists():
            return path
    raise RuntimeError("Google Chrome not found at standard paths.")


def generate_it_cv(
    output_pdf: Optional[pathlib.Path] = None,
    output_html: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Compile and export the Russian IT Engineer CV to PDF via headless Chrome."""
    pdf_target = output_pdf or DEFAULT_OUTPUT_PDF
    html_target = output_html or DEFAULT_OUTPUT_HTML

    pdf_target.parent.mkdir(parents=True, exist_ok=True)
    html_target.write_text(RESUME_HTML_TEMPLATE, encoding="utf-8")
    logger.info(f"Saved HTML resume to {html_target}")

    chrome = get_chrome_executable()
    profile = pathlib.Path.home() / ".cache" / "cv-chrome-profile"

    cmd = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={profile}",
        "--no-pdf-header-footer",
        "--no-first-run",
        f"--print-to-pdf={pdf_target.resolve()}",
        html_target.resolve().as_uri(),
    ]

    logger.info("Executing headless Chrome PDF print...")
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)

    if not pdf_target.exists() or pdf_target.stat().st_size == 0:
        raise RuntimeError(f"Failed to generate PDF at {pdf_target}")

    logger.info(f"Successfully generated IT CV PDF: {pdf_target} ({pdf_target.stat().st_size} bytes)")
    return pdf_target


if __name__ == "__main__":
    pdf = generate_it_cv()
    print(f"Generated PDF: {pdf} ({pdf.stat().st_size} bytes)")
