"""Smart Interview & Employer Negotiation Auto-Responder.

Generates persuasive, context-aware responses to employer questions and interview requests:
- Interview scheduling (MSK daytime / Bangkok local time, Zoom / Google Meet / Telegram).
- Technical stack deep-dives (Linux, Docker, Bitrix24, Python, Networks, Databases, AI).
- Location & legal employment (RF / TK RF / IP / Samozanyaty for CIS; Bangkok on-site for Thailand).
- Salary and compensation discussions.
- Test task / take-home assignments and GitHub portfolio presentation.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from ai_assistant.smart_experience_generator import (
    CONTACTS_BLOCK,
    extract_key_technologies,
    generate_employer_chat_reply,
)

logger = logging.getLogger("interview_auto_responder")

DEFAULT_INTERVIEW_SLOTS_MSK = "будни с 09:00 до 17:00 по МСК (13:00–21:00 по местному времени UTC+7)"
DEFAULT_INTERVIEW_SLOTS_BKK = "weekdays from 10:00 to 18:00 ICT (Bangkok time)"


def generate_interview_response(message_text: str, employer: str = "Employer", vacancy: str = "Role") -> str:
    """Generate professional, grounded interview response draft."""
    res = analyze_employer_inquiry(message_text, context={"company": employer, "role": vacancy})
    return res.get("reply_text") or (
        f"Здравствуйте! Спасибо за приглашение. С радостью готов обсудить вакансию {vacancy} на интервью. "
        f"Мне удобно подключиться в будни в любое рабочее время по Москве — с 09:00 до 17:00 МСК (13:00–21:00 по моему местному времени UTC+7, Google Meet / Zoom / Telegram). "
        f"Мои прямые контакты: Telegram @brokeassh, телефон +7 (993) 339-76-28. Буду ждать ссылку на встречу!"
    )


def analyze_employer_inquiry(message_text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify employer message and generate optimal response draft."""
    ctx = context or {}
    text_clean = message_text.strip()
    text_lower = text_clean.lower()
    is_english = bool(re.search(r"\b(interview|call|meet|salary|experience|resume|cv|bangkok|thailand)\b", text_lower))

    # 1. English inquiries (Bangkok / International)
    if is_english:
        return _generate_english_response(text_lower, ctx)

    # 2. Russian inquiries (HH / CIS remote)
    return _generate_russian_response(text_lower, text_clean, ctx)


def _generate_english_response(text_lower: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Generate professional English response for international employers in Bangkok."""
    role = ctx.get("role", "IT Infrastructure / Systems Engineer")
    comp = ctx.get("company", "your company")

    if any(k in text_lower for k in ["interview", "call", "schedule", "meet", "time", "zoom", "google meet", "available"]):
        reply = (
            f"Dear Hiring Team,\n\n"
            f"Thank you for considering my application for the {role} position! I would be delighted to speak with your team. "
            f"Since I am already permanently based in Bangkok, I am available for an on-site interview at your office or a video call (Google Meet / Zoom / Microsoft Teams). "
            f"I am flexible this week between 10:00 AM and 6:00 PM ICT (Bangkok time).\n\n"
            f"Please let me know a convenient date and time, or reach out to me directly:\n"
            f"• Phone / WhatsApp: +7 (993) 339-76-28\n"
            f"• Telegram: @brokeassh\n"
            f"• Email: mikhailthaiban@gmail.com\n\n"
            f"Best regards,\nMikhail Kolesnikov"
        )
        category = "INTERVIEW_SCHEDULING_EN"

    elif any(k in text_lower for k in ["relocation", "visa", "work permit", "location", "bangkok"]):
        reply = (
            f"Hello!\n\n"
            f"Regarding location: I am currently residing in Bangkok, Thailand. "
            f"I require zero relocation expenses or flight reimbursements, and I am available to start work immediately without overseas waiting periods. "
            f"I am open to standard local employment with Work Permit / Non-B sponsorship, or contractor terms.\n\n"
            f"Best regards,\nMikhail Kolesnikov\n+7 (993) 339-76-28 | @brokeassh"
        )
        category = "LOCATION_EN"

    elif any(k in text_lower for k in ["salary", "rate", "compensation", "budget", "expectation"]):
        reply = (
            f"Hello!\n\n"
            f"My salary expectation for this role in Bangkok is around 80,000 – 120,000 THB/month (net/gross negotiable depending on the full compensation package, health insurance, and role scope). "
            f"I am open to discussing the details during our interview.\n\n"
            f"Best regards,\nMikhail Kolesnikov\n+7 (993) 339-76-28 | @brokeassh"
        )
        category = "SALARY_EN"

    else:
        reply = (
            f"Hello!\n\n"
            f"Thank you for your message regarding the {role} position at {comp}. "
            f"With 15+ years of hands-on experience in Linux/Windows systems administration, Docker infrastructure, network engineering, and Python workflow automation, "
            f"I am confident in delivering immediate value to your technical operations. "
            f"You can review my technical code repositories at https://github.com/mikheooo.\n\n"
            f"I would be glad to discuss details further in an interview!\n\n"
            f"Best regards,\nMikhail Kolesnikov\n+7 (993) 339-76-28 | Telegram: @brokeassh"
        )
        category = "GENERAL_EN"

    return {
        "language": "EN",
        "category": category,
        "reply_text": reply.strip(),
    }


def _generate_russian_response(text_lower: str, text_clean: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Generate sharp Russian response grounded in candidate_kb.json for HH.ru."""
    role = ctx.get("role", "IT-специалист")
    comp = ctx.get("company", "вашей компании")

    # 1. Call / Interview invitation (MSK 09:00 - 16:00 window)
    if any(k in text_lower for k in ["созвон", "интервью", "встреч", "пообщаться", "позвонить", "удобно", "время", "скайп", "зум", "zoom", "meet", "телемост", "когда вы", "когда смож"]):
        reply = (
            f"Здравствуйте! С удовольствием пообщаюсь подробнее по вакансии «{role}» в {comp}.\n\n"
            f"Мне удобно созвониться в будни в любое рабочее время по Москве — с 09:00 до 17:00 МСК (13:00–21:00 по моему местному времени UTC+7). "
            f"Формат любой удобный для вас: Google Meet, Zoom, Telemost или Telegram.\n\n"
            f"Можете назначить встречу на удобный слот или связаться со мной напрямую для согласования слота:\n"
            f"• Telegram: @brokeassh\n"
            f"• Телефон / WhatsApp: +7 (993) 339-76-28\n"
            f"• Email: mikhailthaiban@gmail.com"
        )
        category = "INTERVIEW_SCHEDULING_RU"

    # 2. Test task question or assignment payload
    elif any(k in text_lower for k in ["тестов", "задани", "кейсы", "проверить навыки", "код", "тз"]):
        has_task_payload = any(k in text_lower for k in ["ссылк", "github", "drive", "диск", "notion", "http", "ознаком", "выполн", "дедлайн"])
        if has_task_payload:
            reply = (
                f"Здравствуйте! Тестовое задание получил, ознакомился с требованиями. "
                f"Беру в работу, в ближайшее время вернусь с готовым решением и ссылкой на репозиторий на GitHub: "
                f"https://github.com/mikheooo.\n\n"
                f"Если возникнут уточняющие вопросы по ТЗ, оперативно свяжусь с вами здесь или в Telegram: @brokeassh. Спасибо!"
            )
            category = "TEST_TASK_ACKNOWLEDGED"
        else:
            reply = (
                f"Здравствуйте! Да, готов выполнить профильное тестовое задание, чтобы подтвердить практические навыки в реальных условиях. "
                f"Также можете ознакомиться с примерами моего рабочего кода, скриптов автоматизации и архитектурных решений в открытом репозитории на GitHub: "
                f"https://github.com/mikheooo.\n\n"
                f"Присылайте условия и ТЗ сюда или в Telegram: @brokeassh — оперативно приступлю к реализации!"
            )
            category = "TEST_TASK"

    # 3. Rejection or archive follow-up
    elif any(k in text_lower for k in ["к сожалению", "отказ", "другой кандидат", "приостановлена"]):
        reply = (
            f"Здравствуйте! Спасибо за обратную связь. Если у вас в будущем откроются смежные роли по администрированию серверов, "
            f"DevOps, Bitrix24 или автоматизации на Python — буду рад сотрудничеству. "
            f"Мои прямые контакты для связи: Telegram @brokeassh, телефон +7 (993) 339-76-28, email mikhailthaiban@gmail.com. "
            f"Успехов вашему проекту!"
        )
        category = "POLITE_CLOSURE"

    # 4. Location & Remote confirmation
    elif any(k in text_lower for k in ["где вы", "локаци", "город", "проживан", "рф", "росси", "удален", "remote", "формат работ"]):
        reply = (
            f"Здравствуйте! Удаленно (РФ, часовой пояс МСК+4 / Новосибирск, гражданство РФ). "
            f"Полностью готов работать по московскому времени (09:00–18:00 или 10:00–19:00 МСК) — разница в 4 часа мне абсолютно комфортна и проверена годами удаленной работы. "
            f"Рабочее место профессионально оборудовано: высокоскоростной стабильный интернет, резервный канал связи, "
            f"производительная рабочая станция, бесперебойное питание. "
            f"Готов к официальному оформлению по ТК РФ, ИП или Самозанятости."
        )
        category = "LOCATION_RU"

    # 5. Salary question
    elif any(k in text_lower for k in ["зарплат", "ожидани", "вилка", "ставк", "оплат"]):
        reply = (
            f"Здравствуйте! Мой ориентир по заработной плате — от 100 000 руб. на руки. "
            f"При этом я полностью открыт к диалогу и готов согласовать комфортные условия под бюджет компании и задачи проекта."
        )
        category = "SALARY_RU"

    # 6. Specific tech, company experience (Sayama, Avantis, Freelance, Bitrix24, n8n, Python, Linux)
    else:
        base_res = generate_employer_chat_reply(text_clean, role, comp)
        reply = base_res.get("reply_text", "")
        category = base_res.get("intent", "GENERAL_TECH")

    return {
        "language": "RU",
        "category": category,
        "reply_text": reply.strip(),
    }
