import os
import sys

# Legacy Telegram callback bot. Production feedback goes through
# ai_assistant.telegram_feedback / the Hermes adapter, not this module.
# Importing it does nothing. Running it requires LEGACY_BOT_OVERRIDE=1, and
# only then are aiogram and the OpenAI client constructed.


def _legacy_bot_enabled() -> bool:
    return os.environ.get("LEGACY_BOT_OVERRIDE", "").strip().lower() in {"1", "true", "yes"}


def _refuse_legacy_bot() -> None:
    print(
        "[SAFETY LOCKOUT] 'ai_assistant.bot' is a legacy entry and is disabled.\n"
        "It is not a production bot. Recruiter and digest callbacks belong to the\n"
        "canonical Telegram feedback processor.\n"
        "Set LEGACY_BOT_OVERRIDE=1 only to inspect this old path deliberately.",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def main():
    if not _legacy_bot_enabled():
        _refuse_legacy_bot()
    await _run_legacy_bot()


async def _run_legacy_bot():
    import asyncio
    import json
    import logging

    from aiogram import Bot, Dispatcher, F
    from aiogram.types import CallbackQuery
    from openai import OpenAI

    from . import config, db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)

    bot = Bot(token=config.TG_BOT_TOKEN)
    dp = Dispatcher()

    client = OpenAI(
        api_key=config.LLM_API_KEY if config.LLM_API_KEY else "dummy-key",
        base_url=config.LLM_BASE_URL if config.LLM_BASE_URL else "https://openrouter.ai/api/v1",
    )

    def get_vacancy_by_id(vac_id: str):
        try:
            with open(config.VACANCIES_FILE, encoding="utf-8") as f:
                vacs = json.load(f)
                for v in vacs:
                    if str(v.get("id")) == vac_id:
                        return v
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Error reading vacancies file: %s", e)
        return None

    def generate_cover_letter_sync(title: str, desc: str) -> str:
        my_resume = (
            "AI Automation Engineer. n8n, Python, Telegram Bots. "
            "Опыт настройки LLM-агентов и автоматизации пайплайнов."
        )

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
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("LLM error: %s", e)
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
        await callback.message.edit_text(
            original_text + "\n\n<i>❌ Пропущено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
        await callback.answer("Вакансия скрыта")

    logger.info("Bot started. Listening for callbacks...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    if not _legacy_bot_enabled():
        _refuse_legacy_bot()
    import asyncio

    asyncio.run(main())
