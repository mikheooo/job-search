"""Vacancy Requirements & Domain Analyzer for Resume Tailoring.

Analyzes job title, description, bullet points, and required skills to categorize
the primary domain archetype, extract high-priority ATS keywords, and identify
the employer's core operational needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VacancyDomain(str, Enum):
    TELECOM_RF = "telecom_rf"
    FIELD_ENGINEERING = "field_engineering"
    FIRE_AUTOMATION = "fire_automation"
    NETWORKS_SYSADMIN = "networks_sysadmin"
    MECHANICAL_TRADES = "mechanical_trades"
    SOFTWARE_AUTOMATION = "software_automation"


DOMAIN_PATTERNS: dict[VacancyDomain, list[re.Pattern]] = {
    VacancyDomain.TELECOM_RF: [
        re.compile(r"\btelecom(?:munications)?\b", re.IGNORECASE),
        re.compile(r"\bvsat\b", re.IGNORECASE),
        re.compile(r"\bsatellite\b", re.IGNORECASE),
        re.compile(r"\brf\b|\bradio\s+frequency\b", re.IGNORECASE),
        re.compile(r"\bantennas?\b|\bmicrowave\b", re.IGNORECASE),
        re.compile(r"\bbuc\b|\blnb\b|\bidirect\b", re.IGNORECASE),
        re.compile(r"\bfiber\s+optics?\b|\bcabling\b", re.IGNORECASE),
    ],
    VacancyDomain.FIRE_AUTOMATION: [
        re.compile(r"\bfire\s+alarms?\b", re.IGNORECASE),
        re.compile(r"\bdry\s+fire\b|\bwet\s+fire\b", re.IGNORECASE),
        re.compile(r"\bfire\s+programmers?\b", re.IGNORECASE),
        re.compile(r"\baccess\s+control\b|\bcctv\b", re.IGNORECASE),
        re.compile(r"\bbuilding\s+automation\b|\bbms\b", re.IGNORECASE),
        re.compile(r"\bsignaling\b|\balarm\s+testing\b", re.IGNORECASE),
    ],
    VacancyDomain.MECHANICAL_TRADES: [
        re.compile(r"\bdiesel\s+fitters?\b|\bmechanics?\b", re.IGNORECASE),
        re.compile(r"\bautomotive\s+technicians?\b", re.IGNORECASE),
        re.compile(r"\bmobile\s+plants?\b|\bheavy\s+machinery\b", re.IGNORECASE),
        re.compile(r"\belectro-mechanical\b|\bhydraulics?\b", re.IGNORECASE),
        re.compile(r"\bfleet\s+maintenance\b", re.IGNORECASE),
    ],
    VacancyDomain.NETWORKS_SYSADMIN: [
        re.compile(r"\bnetwork\s+engineers?\b|\bsysadmins?\b|\bsystem\s+administrators?\b", re.IGNORECASE),
        re.compile(r"\bcisco\b|\bmikrotik\b|\brouting\b|\bswitching\b", re.IGNORECASE),
        re.compile(r"\blinux\b|\bwindows\s+server\b", re.IGNORECASE),
        re.compile(r"\btech(?:nical)?\s+support\b|\bl2\b|\bl3\b|\bhelpdesk\b", re.IGNORECASE),
        re.compile(r"\bvpn\b|\bvlan\b|\bzabbix\b|\bsnmp\b", re.IGNORECASE),
    ],
    VacancyDomain.SOFTWARE_AUTOMATION: [
        re.compile(r"\bpython\b|\bdevelopers?\b|\bsoftware\s+engineers?\b", re.IGNORECASE),
        re.compile(r"\bautomation\s+engineers?\b|\bintegrat(?:ions?|ors?)\b", re.IGNORECASE),
        re.compile(r"\bapi\b|\bscraping\b|\bdatabases?\b|\bsql\b", re.IGNORECASE),
        re.compile(r"\bcrm\b|\bbitrix24\b|\bamocrm\b", re.IGNORECASE),
        re.compile(r"\bllm\b|\bai\s+engineers?\b|\brag\b", re.IGNORECASE),
    ],
    VacancyDomain.FIELD_ENGINEERING: [
        re.compile(r"\bfield\s+service\b|\bfield\s+tech(?:nicians?|s)?\b|\bfield\s+engineers?\b", re.IGNORECASE),
        re.compile(r"\bcommissioning\b|\bon-site\b|\bservice\s+technicians?\b", re.IGNORECASE),
        re.compile(r"\bhardware\s+maintenance\b|\bequipment\s+installs?\b", re.IGNORECASE),
        re.compile(r"\bwhs\b|\bswms\b|\bjsea\b|\bppe\b", re.IGNORECASE),
    ],
}

COMMON_TECHNICAL_KEYWORDS = [
    "Cisco", "MikroTik", "Ubiquiti", "Linux", "Windows Server", "VSAT", "C-band", "Ku-band",
    "RF", "BUC", "LNB", "iDirect", "TCP/IP", "VLAN", "VPN", "WireGuard", "Zabbix", "SNMP",
    "Python", "Docker", "WHS", "SWMS", "JSEA", "PPE", "Safe Work", "Commissioning",
    "Troubleshooting", "Preventative Maintenance", "Fault Finding", "Cabling", "Structured Cabling",
    "Access Control", "CCTV", "Fire Alarm", "PLC", "Diagnostics", "Sensors", "Calibration",
]


@dataclass
class VacancyAnalysis:
    domain: VacancyDomain
    target_role_title: str
    company_name: str
    location: str
    matched_keywords: list[str] = field(default_factory=list)
    key_responsibilities: list[str] = field(default_factory=list)
    visa_sponsorship_indicated: bool = False
    original_title: str = ""


def analyze_vacancy(job: dict[str, Any]) -> VacancyAnalysis:
    """Analyze job posting and determine primary domain and targeted role title."""
    title = str(job.get("title") or "Field Technician").strip()
    company = str(job.get("company") or "Direct Employer").strip()
    location = str(job.get("location") or "Australia").strip()
    teaser = str(job.get("teaser") or "")
    bullet_points = [str(b) for b in (job.get("bullet_points") or [])]
    full_text = f"{title} {teaser} {' '.join(bullet_points)}"

    # Score each domain
    scores: dict[VacancyDomain, int] = {d: 0 for d in VacancyDomain}
    for domain, patterns in DOMAIN_PATTERNS.items():
        for pat in patterns:
            # Matches in title are worth 3x
            if pat.search(title):
                scores[domain] += 3
            if pat.search(full_text):
                scores[domain] += 1

    # Choose domain with highest score, fallback to FIELD_ENGINEERING
    best_domain = max(scores, key=lambda d: scores[d])
    if scores[best_domain] == 0:
        best_domain = VacancyDomain.FIELD_ENGINEERING

    # Extract keywords present in vacancy
    matched_keywords: list[str] = []
    for kw in COMMON_TECHNICAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", full_text, re.IGNORECASE):
            matched_keywords.append(kw)

    # Clean target role title (remove words like 'visa sponsorship', 'wanted', etc.)
    cleaned_title = re.sub(
        r"(?:[-–|]\s*)?(?:visa\s+sponsorship(?:\s+available|\s+possible)?|wanted|urgent|immediate\s+start|apply\s+now).*",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned_title:
        cleaned_title = title

    visa_indicated = bool(job.get("matched_keywords")) or bool(
        re.search(r"sponsorship|visa|482|dama", full_text, re.IGNORECASE)
    )

    return VacancyAnalysis(
        domain=best_domain,
        target_role_title=cleaned_title,
        company_name=company,
        location=location,
        matched_keywords=matched_keywords,
        key_responsibilities=bullet_points[:4],
        visa_sponsorship_indicated=visa_indicated,
        original_title=title,
    )
