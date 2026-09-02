"""Stage 89.2: Canonical Hermes Telegram Adapter Integration Hook.

This hook is injected into Hermes TelegramAdapter._handle_callback_query
in plugins/platforms/telegram/adapter.py to route fb: callback queries
to TelegramFeedbackProcessor.
"""

HERMES_CALLBACK_HOOK_MARKER = "# --- Job search feedback callbacks (fb:action:id) ---"

HERMES_CALLBACK_HOOK_CODE = '''        # --- Job search feedback callbacks (fb:action:id) ---
        if data.startswith("fb:"):
            try:
                import sys
                job_search_root = r"c:\\Users\\Misha\\Documents\\job-search"
                if job_search_root not in sys.path:
                    sys.path.insert(0, job_search_root)
                from ai_assistant.telegram_feedback import TelegramFeedbackProcessor

                processor = TelegramFeedbackProcessor()
                raw_cb = {
                    "id": str(query.id),
                    "from": {
                        "id": getattr(query.from_user, "id", None),
                        "username": getattr(query.from_user, "username", None),
                        "first_name": getattr(query.from_user, "first_name", None),
                    },
                    "message": {
                        "message_id": getattr(query_message, "message_id", None),
                        "chat": {"id": query_chat_id},
                    },
                    "data": data,
                }
                res = processor.process_callback_query(raw_cb)
                logger.info("[Telegram] Handled job-search feedback callback: %s -> %s", data, res)
            except Exception as fb_exc:
                logger.error("[Telegram] Error processing job-search feedback callback: %s", fb_exc, exc_info=True)
                try:
                    await query.answer(text="⚠️ Ошибка обработки отклика", show_alert=True)
                except Exception:
                    pass
            return
'''
