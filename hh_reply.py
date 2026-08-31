import os
import sys
import time

# ==============================================================================
# STAGE 75 SAFETY GATE: LEGACY HH_REPLY IS DEPRECATED & LOCKED
# ==============================================================================
# All HeadHunter recruiter dialogs MUST be handled through canonical Stage 22-53
# modules (hh_message_watcher.py, hh_message_reply.py, hh_autonomous_agent.py)
# to guarantee truth-only facts, candidate profile validation, and state.db recording.
# Raw automated injection is strictly forbidden to prevent dual-reply races.
# ==============================================================================

if not os.environ.get("LEGACY_HH_REPLY_OVERRIDE"):
    print(
        "[SAFETY LOCKOUT] 'hh_reply.py' is deprecated and locked to prevent dual-reply conflicts.\n"
        "To triage or send recruiter replies with truth-only validation, use:\n"
        "  - Triage/Preview: python -m ai_assistant.cli hh-message triage\n"
        "  - Preview draft:  python -m ai_assistant.cli hh-message preview [conversation_id]\n"
        "  - Send confirmed: python -m ai_assistant.cli hh-message send [conversation_id] --confirm\n",
        file=sys.stderr,
    )
    sys.exit(1)

from playwright.sync_api import sync_playwright

def send_reply(url, text):
    with sync_playwright() as p:
        try:
            # Подключаемся к уже открытому Chrome
            browser = p.chromium.connect_over_cdp("http://localhost:9222", timeout=10000)
        except Exception as e:
            print("ERROR: Could not connect to Chrome CDP (port 9222). Is Chrome running with debug port?")
            return False
            
        context = browser.contexts[0]
        page = context.pages[0]
        
        try:
            print(f"Navigating to {url}...")
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(3)
            
            chat_input = page.locator('[contenteditable="true"]').or_(page.locator('textarea')).or_(page.locator('[data-qa="negotiations-chat-message-input"]'))
            if chat_input.count() > 0:
                target = chat_input.last
                target.click()
                
                # Вставляем текст
                page.evaluate(f'''() => {{
                    let el = document.querySelectorAll('[contenteditable="true"], textarea');
                    if(el.length > 0) {{
                        let t = el[el.length-1];
                        if(t.tagName === 'TEXTAREA') t.value = `{text}`;
                        else t.innerText = `{text}`;
                        t.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                }}''')
                time.sleep(0.5)
                target.click()
                page.keyboard.type(" ")
                time.sleep(0.5)
                
                # Отправляем
                send_btn = page.locator('button[data-qa="chat-message-send-button"]').or_(page.locator('button[data-qa="negotiations-chat-message-send"]')).or_(page.locator('button:has(svg[data-name="Send"])'))
                if send_btn.count() > 0:
                    send_btn.last.click()
                else:
                    page.keyboard.press("Control+Enter")
                    time.sleep(0.5)
                    page.keyboard.press("Enter")
                    
                time.sleep(2)
                print("SUCCESS")
                return True
            else:
                print("ERROR: Chat input not found on page.")
                return False
        except Exception as e:
            print(f"ERROR: {e}")
            return False
        finally:
            browser.close()

if __name__ == "__main__":
    if len(sys.argv) > 2:
        send_reply(sys.argv[1], sys.argv[2])
    else:
        print("Usage: python hh_reply.py <URL> <TEXT>")
