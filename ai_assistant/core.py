import json
import logging
import requests
from pydantic import BaseModel, Field
from openai import OpenAI
from . import config

class VacancyAnalysis(BaseModel):
    score: int = Field(..., ge=1, le=10, description="Оценка от 1 до 10")
    interview_probability: str = Field(..., description="Низкая/Средняя/Высокая с кратким обоснованием")
    offer_probability: str = Field(..., description="Низкая/Средняя/Высокая")
    strengths: list[str] = Field(..., description="Мои сильные стороны для этой вакансии")
    weaknesses: list[str] = Field(..., description="Мои слабые стороны или нехватка опыта")
    red_flags: list[str] = Field(..., description="Красные флаги (если есть)")
    apply_reasons: list[str] = Field(..., description="Почему стоит откликнуться")
    skip_reasons: list[str] = Field(..., description="Причины пропустить")
    recommendation: str = Field(..., description="Откликаться | Возможно | Не тратить время")

client = OpenAI(
    api_key=config.LLM_API_KEY if config.LLM_API_KEY else "dummy-key",
    base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else "https://openrouter.ai/api/v1"
)

def analyze_vacancy(title: str, description: str, my_resume: str) -> VacancyAnalysis:
    schema_json = VacancyAnalysis.model_json_schema()
    system_prompt = (
        "Ты AI-ассистент по поиску работы. Проанализируй вакансию на основе резюме кандидата. "
        "Обязательно верни ответ в виде валидного JSON, СТРОГО соответствующего следующей схеме: "
        f"{json.dumps(schema_json)}"
    )
    user_prompt = f"Резюме:\n{my_resume}\n\nВакансия:\n{title}\n{description}"
    
    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"} 
    )
    
    response_text = response.choices[0].message.content
    return VacancyAnalysis.model_validate_json(response_text)

def send_to_telegram(vacancy_id: str, title: str, company: str, salary: str, analysis: VacancyAnalysis, url: str):
    if not config.TG_BOT_TOKEN or not config.TG_CHAT_ID:
        logging.warning("Telegram credentials not set, skipping notification.")
        return

    text = f"🎯 <b>{title}</b>\n"
    text += f"🏢 {company}\n"
    if salary:
         text += f"💰 {salary}\n"
    text += f"🔗 <a href='{url}'>Ссылка</a>\n\n"
    
    text += f"📊 <b>Score:</b> {analysis.score}/10\n"
    text += f"🤝 <b>Интервью:</b> {analysis.interview_probability}\n"
    text += f"💼 <b>Оффер:</b> {analysis.offer_probability}\n\n"
    
    if analysis.strengths:
        text += f"✅ <b>Плюсы:</b>\n- " + "\n- ".join(analysis.strengths) + "\n\n"
    if analysis.red_flags:
        text += f"⚠️ <b>Флаги:</b>\n- " + "\n- ".join(analysis.red_flags) + "\n\n"
        
    text += f"💡 <b>Вердикт:</b> {analysis.recommendation}"

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Подготовить отклик", "callback_data": f"reply_{vacancy_id}"},
                {"text": "❌ Пропустить", "callback_data": f"skip_{vacancy_id}"}
            ]
        ]
    }

    url_api = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup)
    }
    
    response = requests.post(url_api, json=payload)
    response.raise_for_status()
