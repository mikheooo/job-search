import json
import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery
from openai import OpenAI

from . import config
from . import db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = Bot(token=config.TG_BOT_TOKEN)
dp = Dispatcher()

client = OpenAI(
    api_key=config.LLM_API_KEY if config.LLM_API_KEY else "dummy-key",
    base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else "https://openrouter.ai/api/v1"
)

def get_vacancy_by_id(vac_id: str):
    try:
        with open(config.VACANCIES_FILE, 'r', encoding='utf-8') as f:
            vacs = json.load(f)
            for v in vacs:
                if str(v.get("id")) == vac_id:
                    return v
    except Exception as e:
        logging.error(f"Error reading JSON: {e}")
    return None

def generate_cover_letter_sync(title: str, desc: str) -> str:
    my_resume = "AI Automation Engineer. n8n, Python, Telegram Bots. Опыт настройки LLM-агентов и автоматизации пайплайнов."
    
    system_prompt = (
        "Ты эксперт по трудоустройству. Твоя задача — помочь AI Automation инженеру получить работу. "
        "Напиши профессиональное, но лаконичное сопроводительное письмо (Cover Letter) на русском языке. "
        "Оно должно быть сразу готово к отправке (без заглушек вроде [Имя компании]). "
        "После письма напиши 2-3 возможных вопроса от HR по этой вакансии и краткие ответы на них."
    )
    user_prompt = f"Мое резюме/скиллы:\n{my_resume}\n\nВакансия:\nНазвание: {title}\nОписание:\n{desc}"
    
    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"LLM Error: {e}")
        return f"Ошибка при генерации отклика: {e}"

@dp.callback_query(F.data.startswith("reply_"))
async def handle_reply(callback: CallbackQuery):
    vac_id = callback.data.split("_")[1]
    await callback.answer()
    
    msg = await callback.message.answer("⏳ <i>Изучаю вакансию и генерирую отклик...</i>", parse_mode="HTML")
    
    db.save_status(vac_id, "applied")
    
    vac = get_vacancy_by_id(vac_id)
    if not vac:
        await msg.edit_text("❌ Вакансия не найдена в исходном файле.")
        return
        
    title = vac.get("title", "Без названия")
    desc = vac.get("description", "")
    
    loop = asyncio.get_running_loop()
    cover_letter_text = await loop.run_in_executor(None, generate_cover_letter_sync, title, desc)
    
    final_text = f"📝 <b>Готовый отклик: {title}</b>\n\n{cover_letter_text}"
    
    if len(final_text) > 4000:
        final_text = final_text[:4000] + "...\n(Текст обрезан)"
        
    await msg.edit_text(final_text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("skip_"))
async def handle_skip(callback: CallbackQuery):
    vac_id = callback.data.split("_")[1]
    
    db.save_status(vac_id, "skipped")
    
    original_text = callback.message.html_text or callback.message.text
    await callback.message.edit_text(original_text + "\n\n<i>❌ Пропущено</i>", parse_mode="HTML", reply_markup=None)
    await callback.answer("Вакансия скрыта")

async def main():
    logging.info("Bot started. Listening for callbacks...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
