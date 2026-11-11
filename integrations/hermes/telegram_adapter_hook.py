"""Stage 89.2: Canonical Hermes Telegram Adapter Integration Hook.

This hook is injected into Hermes TelegramAdapter._handle_callback_query
in plugins/platforms/telegram/adapter.py to route fb: callback queries
to TelegramFeedbackProcessor.
"""

HERMES_CALLBACK_HOOK_MARKER = "# --- Job search feedback callbacks (fb:action:id) ---"

# Injected immediately AFTER the anchor line inside _handle_callback_query,
# because the body below reads both `data` and the `cb` context dict that the
# upstream method builds on the line just above the anchor. The adapter is
# upstream Hermes code and is rewritten wholesale on every Hermes self-update,
# so a hard-coded anchor eventually rots -- which is exactly what produced the
# DRIFTED adapter on 2026-10-02: the previous anchor ("# --- Update prompt
# callbacks ---") vanished in the 15:20 upstream rewrite of this method.
ADAPTER_HOOK_ANCHOR = "        cb = self._callback_ctx(query)\n"

HERMES_CALLBACK_HOOK_CODE = '''        # --- Job search feedback callbacks (fb:action:id) ---
        if data.startswith("fb:"):
            try:
                import os
                import sys
                job_search_root = os.getenv("JOB_SEARCH_ROOT") or os.path.expanduser(r"~\Documents\job-search")
                if job_search_root not in sys.path:
                    sys.path.insert(0, job_search_root)
                from ai_assistant.telegram_feedback import TelegramFeedbackProcessor

                query_user = getattr(query, "from_user", None)
                query_message = getattr(query, "message", None)
                processor = TelegramFeedbackProcessor()
                raw_cb = {
                    "id": str(getattr(query, "id", "") or ""),
                    "from": {
                        "id": getattr(query_user, "id", None),
                        "username": getattr(query_user, "username", None),
                        "first_name": getattr(query_user, "first_name", None),
                    },
                    "message": {
                        "message_id": getattr(query_message, "message_id", None),
                        "chat": {"id": cb.get("chat_id")},
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
