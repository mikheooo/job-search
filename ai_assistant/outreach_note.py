import json
import logging
from pydantic import BaseModel, Field
from openai import OpenAI
import config

client = OpenAI(
    api_key=config.LLM_API_KEY if config.LLM_API_KEY else "dummy-key",
    base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else "https://openrouter.ai/api/v1",
)

MAX_NOTE_LENGTH = 300


class ConnectionNoteResult(BaseModel):
    note: str = Field(..., max_length=300, description="Персонализированная заметка до 300 символов")


def generate_connection_note(title: str, desc: str, company: str, lead_name: str = "") -> str:
    system_prompt = (
        "Ты эксперт по B2B-аутричу. Напиши персонализированную заметку (Connection Note) на русском или английском языке, "
        "максимум 300 символов. Она должна быть конкретной, без шаблонов '[Имя]' или '[Компания]'. "
        "Упоминай миссию/продукт компании и релевантный кейс из практики (n8n, Telegram, LLM, Python). "
        "Язык: английский, если компания явно US/global; русский, если явно CIS. "
        "Никаких восклицательных маркетинговых фраз. Ответ строго в JSON с полем `note`."
    )
    user_prompt = (
        f"Компания: {company}\n"
        f"Контакт: {lead_name}\n"
        f"Вакансия: {title}\n"
        f"Описание: {desc}\n\n"
        "Сгенерируй Connection Note."
    )

    schema = {
        "type": "object",
        "properties": {"note": {"type": "string", "maxLength": MAX_NOTE_LENGTH}},
        "required": ["note"],
        "additionalProperties": False,
    }

    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "connection_note",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        raw = response.choices[0].message.content or "{}"
        obj = json.loads(raw)
        note = obj.get("note", "").strip()
    except Exception as e:
        logging.error("LLM Error: %s", e)
        return f"Ошибка генерации: {e}"

    if len(note) > MAX_NOTE_LENGTH:
        note = note[: MAX_NOTE_LENGTH - 3] + "..."
    return note or "Привет! Увидел вакансию — подходит по стеку n8n/Python/LLM."
