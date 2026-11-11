"""Bangkok & International English CV Generator.

Generates a polished, executive-quality A4 English CV tailored for international
employers and tech companies in Bangkok, Thailand (e.g. Agoda, Shopee, Allianz, Zaapi, Bitkub).

Key differentiators highlighted:
- Local Bangkok Presence: Already based locally in Bangkok, Thailand. Zero relocation costs, immediate in-person availability.
- 15+ Years Enterprise IT Experience: Linux, Windows Server, Docker, Networks, Python Automation, AI/LLM integrations, Bitrix24 CRM.
- Track record in Thailand: Sayama Travel (Nov 2022 - Apr 2026).
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
from typing import Optional

logger = logging.getLogger("bangkok_resume_builder")

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PDF = ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_Bangkok.pdf"
DEFAULT_OUTPUT_HTML = ROOT / "artifacts" / "Mikhail_Kolesnikov_CV_Bangkok.html"

CHROME_PATHS = (
    pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    pathlib.Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


RESUME_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mikhail Kolesnikov — Senior Systems & DevOps Engineer</title>
<style>
@page {
  size: A4;
  margin: 12mm 14mm 12mm 14mm;
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
  font-size: 9.8pt;
  line-height: 1.38;
  color: #212529;
  background: #ffffff;
}

.header {
  border-bottom: 2px solid #0056b3;
  padding-bottom: 8pt;
  margin-bottom: 10pt;
}

.name {
  font-size: 21pt;
  font-weight: 700;
  color: #111827;
  letter-spacing: -0.3pt;
  margin: 0 0 2pt 0;
}

.headline {
  font-size: 11pt;
  font-weight: 600;
  color: #0056b3;
  margin: 0 0 4pt 0;
}

.local-banner {
  background: #eef6ff;
  border-left: 3px solid #0056b3;
  padding: 3pt 8pt;
  font-size: 9pt;
  font-weight: 600;
  color: #0f3f6d;
  margin-bottom: 5pt;
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
  margin-bottom: 9pt;
}

h2 {
  font-size: 10.5pt;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5pt;
  color: #111827;
  border-bottom: 1px solid #d1d5db;
  padding-bottom: 2pt;
  margin: 0 0 5pt 0;
}

.summary-text {
  font-size: 9.3pt;
  color: #374151;
  text-align: justify;
  margin: 0 0 4pt 0;
}

.skills-grid {
  display: grid;
  grid-template-columns: 140pt 1fr;
  row-gap: 3pt;
  font-size: 9pt;
  margin-bottom: 4pt;
}

.skill-category {
  font-weight: 600;
  color: #1f2937;
}

.skill-items {
  color: #4b5563;
}

.job {
  margin-bottom: 7pt;
  page-break-inside: avoid;
}

.job-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1pt;
}

.job-title {
  font-size: 10pt;
  font-weight: 700;
  color: #111827;
}

.job-company {
  font-weight: 600;
  color: #0056b3;
}

.job-period {
  font-size: 8.8pt;
  font-weight: 500;
  color: #6b7280;
}

ul {
  margin: 2pt 0 0 0;
  padding-left: 14pt;
}

li {
  font-size: 9pt;
  color: #374151;
  margin-bottom: 2pt;
}

li strong {
  color: #1f2937;
}

.edu-item {
  display: flex;
  justify-content: space-between;
  font-size: 9pt;
}

.lang-grid {
  font-size: 9pt;
  color: #374151;
}
</style>
</head>
<body>

<div class="header">
  <div class="name">Mikhail Kolesnikov</div>
  <div class="headline">Senior Systems Administrator | DevOps & Infrastructure Engineer | Python Automation</div>
  <div class="local-banner">
    📍 Already based locally in Bangkok, Thailand · Zero relocation expenses for employer · Immediate in-person availability
  </div>
  <div class="contacts">
    <span>✉️ <a href="mailto:mikhailthaiban@gmail.com">mikhailthaiban@gmail.com</a></span>
    <span>📱 WhatsApp / Call: +7 (993) 339-76-28</span>
    <span>💬 Telegram: <a href="https://t.me/brokeassh">@brokeassh</a></span>
    <span>💻 GitHub: <a href="https://github.com/mikheooo">github.com/mikheooo</a></span>
  </div>
</div>

<section>
  <h2>Professional Summary</h2>
  <p class="summary-text">
    Results-oriented Senior Systems & DevOps Engineer with <strong>15+ years of enterprise IT experience</strong> spanning Linux/Windows infrastructure, Docker containerization, network administration, Python workflow automation, and modern AI/LLM integration. Proven track record managing high-availability environments, automating enterprise CRM systems (Bitrix24), and deploying asynchronous microservices. <strong>Already residing in Bangkok, Thailand</strong>; immediately available for in-person interviews and full-time on-site/hybrid employment with zero relocation cost or wait time. English B1 working proficiency.
  </p>
</section>

<section>
  <h2>Core Technical Skills</h2>
  <div class="skills-grid">
    <div class="skill-category">Operating Systems & Infra:</div>
    <div class="skill-items">Linux (Ubuntu, Debian, RHEL, CentOS), Windows Server (2012–2022), Active Directory, Hyper-V, VMware, Systemd, Backup & Disaster Recovery</div>

    <div class="skill-category">DevOps & Containers:</div>
    <div class="skill-items">Docker, Docker Compose, Containerization, CI/CD pipelines, Nginx (Reverse Proxy, Load Balancing, SSL/TLS), WireGuard VPN, OpenVPN</div>

    <div class="skill-category">Networking & Security:</div>
    <div class="skill-items">TCP/IP, DNS, DHCP, VLAN, Firewalls (iptables, UFW), Routing, NAT, Cisco, Mikrotik RouterOS, IP Telephony (SIP / Asterisk)</div>

    <div class="skill-category">Python & Automation:</div>
    <div class="skill-items">Python 3 (asyncio, FastAPI, aiohttp, aiogram, Telethon), Bash scripting, PowerShell, REST APIs, Webhooks, n8n, Make</div>

    <div class="skill-category">Databases & CRM:</div>
    <div class="skill-items">PostgreSQL, MySQL, MS SQL, Redis, Bitrix24 (On-Premise & Cloud Administration, REST API, Automated Workflows, Robots)</div>

    <div class="skill-category">AI & Workflow Integration:</div>
    <div class="skill-items">OpenAI API, Google Gemini API, Claude API, Whisper Speech-to-Text, Prompt Engineering, RAG Pipelines, Automated Data Extraction</div>
  </div>
</section>

<section>
  <h2>Professional Experience</h2>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Sayama Travel</span> — AI Automation & IT Infrastructure Engineer</div>
      <div class="job-period">Thailand (Bangkok / Pattaya) | Nov 2022 – Apr 2026</div>
    </div>
    <ul>
      <li>Administered core <strong>Linux and Docker server infrastructure</strong>, ensuring 99.9% service availability, automated snapshot backups, and secure WireGuard site-to-site VPN across branch locations.</li>
      <li>Architected enterprise automations for <strong>Bitrix24 CRM</strong>, configuring custom robotic rules, webhooks, multi-channel customer intake, and VoIP telephony routing.</li>
      <li>Engineered an automated voice-message processing pipeline utilizing <strong>OpenAI Whisper</strong> to transcribe audio messages into actionable text and attach summarized deal notes into CRM records.</li>
      <li>Developed asynchronous <strong>Telegram bots and ETL pipelines</strong> using Python (aiogram, asyncio, Telethon), processing high-volume customer inquiries and file transfers.</li>
      <li>Orchestrated multi-service workflows via <strong>n8n</strong> connecting Bitrix24, Google Workspace APIs, external relational databases, and notification channels.</li>
      <li>Integrated Google Gemini and OpenAI LLM endpoints into corporate customer support, automating message classification and multilingual draft responses.</li>
    </ul>
  </div>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Avantis</span> — Systems & Bitrix24 Technical Specialist</div>
      <div class="job-period">Moscow | Apr 2021 – Aug 2022</div>
    </div>
    <ul>
      <li>Administered self-hosted Bitrix24 on Linux; configured custom business processes, automation triggers, user access permissions, and external REST API integrations.</li>
      <li>Provided L2/L3 technical support, resolved hardware and network issues, and authored standard operating procedures (SOPs) for business teams.</li>
    </ul>
  </div>

  <div class="job">
    <div class="job-header">
      <div class="job-title"><span class="job-company">Speedcast / Germsat · Intravision · Russian Post</span> — Enterprise IT Systems Administrator</div>
      <div class="job-period">2009 – 2021</div>
    </div>
    <ul>
      <li>12+ years administering corporate IT infrastructure: Windows Server, Active Directory (GPO, DNS, DHCP), MS Exchange, and Hyper-V virtualization.</li>
      <li>Managed network equipment (Cisco, Mikrotik switches/routers), configured VLANs, firewall policies, IP telephony, and satellite communication equipment (Speedcast).</li>
      <li>Maintained relational database servers (MS SQL, PostgreSQL) and developed administrative automation scripts using PowerShell and Bash.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Key Technical Projects</h2>
  <div class="job">
    <ul>
      <li><strong>Autonomous AI Job Search Assistant (Python / SQLite / Playwright):</strong> Full-stack agentic system that continuously discovers vacancies across 6+ platforms, analyzes JD technical stacks, evaluates eligibility, and automates customized applications and employer notifications.</li>
      <li><strong>Reels / Video Content Analyzer (Python / Redis / ARQ / PostgreSQL / Docker):</strong> Asynchronous video analysis pipeline accepting social URLs, extracting audio transcripts, fact-checking claims with web APIs, and delivering structured markdown reports.</li>
    </ul>
  </div>
</section>

<section>
  <h2>Education & Languages</h2>
  <div class="edu-item">
    <div><strong>Sakhalin State University</strong> — Technological Institute, Technology & Entrepreneurship (Higher Education Degree)</div>
    <div>2005</div>
  </div>
  <div class="lang-grid" style="margin-top: 3pt;">
    <strong>Languages:</strong> English — B1 (Professional Working Proficiency for technical communications) · Russian — Native
  </div>
</section>

</body>
</html>
"""


def get_chrome_executable() -> pathlib.Path:
    """Find local Google Chrome executable."""
    import shutil
    for cmd in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(cmd)
        if found:
            return pathlib.Path(found)
    for path in CHROME_PATHS:
        if path.exists():
            return path
    raise RuntimeError("Google Chrome not found at standard paths.")


def generate_bangkok_cv(
    output_pdf: Optional[pathlib.Path] = None,
    output_html: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Compile and export the Bangkok English CV to PDF via headless Chrome."""
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

    logger.info(f"Successfully generated Bangkok CV PDF: {pdf_target} ({pdf_target.stat().st_size} bytes)")
    return pdf_target


if __name__ == "__main__":
    pdf = generate_bangkok_cv()
    print(f"Generated PDF: {pdf} ({pdf.stat().st_size} bytes)")
