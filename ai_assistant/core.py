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
    """Legacy helper: routes vacancy discovery review through Telegram gateway."""
    from .telegram_notifier import get_telegram_notifier

    details = {
        "vacancy_id": vacancy_id,
        "title": title,
        "company": company,
        "salary": salary,
        "score": analysis.score,
        "recommendation": analysis.recommendation,
        "url": url,
    }
    # Vacancy discovery/matching is filtered from Telegram by the gateway (stored in logs/DB only)
    return get_telegram_notifier().deliver_notification("VACANCY_MATCHED", details, delivery_key=f"match_{vacancy_id}")
