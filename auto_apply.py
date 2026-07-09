import json
import time
import sys
import subprocess
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
    from playwright.sync_api import sync_playwright

VACANCIES_FILE = 'C:\\Users\\Misha\\Documents\\job-search\\vacancies.json'

q1_answer = "Паттайя, Таиланд (работаю полностью удаленно, готов подстраиваться под нужный часовой пояс, например МСК)."
q2_answer = "Прямого коммерческого опыта в финтехе/web3 нет, однако есть глубокий опыт построения отказоустойчивой IT-инфраструктуры, сложных API-интеграций (n8n, Python) и автоматизации процессов. Готов быстро погрузиться в специфику отрасли."

letter_ru = """Здравствуйте!
Меня зовут Михаил. Я инженер по AI-автоматизации и интеграциям (Python, n8n), имею опыт работы с мультиагентными системами. 
В моем арсенале:
— Создание отказоустойчивых пайплайнов на n8n и связка с LLM API.
— Python-бэкенд для интеграций, веб-скрапинга и создания Telegram-ботов (aiogram).
— Автоматизация процессов, выгрузка дашбордов.
Буду рад пообщаться и обсудить, как могу быть полезен для ваших задач."""

letter_en = """Hello!
I am an AI Automation and Integrations Engineer (Python, n8n) with experience in building multi-agent systems.
My tech stack includes:
- Building robust pipelines in n8n and integrating with LLM APIs (OpenAI, Claude, Gemini).
- Python backend for integrations, web scraping, and Telegram bots (aiogram).
- Process automation and self-hosted infrastructure (Docker, databases).
I would be glad to discuss how my skills align with your needs."""

def process_vacancy(page, url, lang):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Processing {url}...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        # Check if already applied
        text = page.evaluate("document.body.innerText")
        if "You applied" in text or "Вы откликнулись" in text:
            print("Already applied (detected via page text).")
            return True
            
        # Click respond
        respond_btn = page.get_by_role("link", name="Откликнуться").or_(page.get_by_role("button", name="Откликнуться")).or_(page.get_by_role("link", name="Respond")).or_(page.get_by_role("button", name="Respond"))
        count = respond_btn.count()
        if count == 0:
            print("No respond button found. Skipping.")
            return False
            
        respond_btn.nth(count-1).click(force=True)
        page.wait_for_timeout(3000)
        
        # If URL didn't change and we are on hh.ru, force navigate to response page
        if "hh.ru/vacancy/" in page.url:
            vid = url.split('/')[-1].split('?')[0]
            resp_url = f"https://hh.ru/applicant/vacancy_response?vacancyId={vid}"
            page.goto(resp_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            
        # Fill questions if any
        textareas = page.locator('textarea')
        ta_count = textareas.count()
        if ta_count >= 2:
            textareas.nth(0).fill(q1_answer)
            textareas.nth(1).fill(q2_answer)
            
        # Add cover letter
        add_letter = page.locator('[data-qa="vacancy-response-letter-toggle"]').or_(page.get_by_text("Написать сопроводительное")).or_(page.get_by_text("Covering letter Add"))
        if add_letter.count() > 0:
            add_letter.first.click(force=True)
            page.wait_for_timeout(2000)
            
        letter_input = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
        if letter_input.count() == 0:
            letter_input = page.locator('textarea').last
            
        if str(letter_input) != '' and letter_input.count() > 0:
            letter = letter_en if lang == 'en' else letter_ru
            letter_input.fill(letter)
        else:
            print("ERROR: No textarea for cover letter found. Aborting application to avoid empty submission.")
            return False # Строгий предохранитель: не отправлять без письма!
            
        page.wait_for_timeout(1000)
        
        # Submit
        submit_btn = page.locator('button[data-qa="vacancy-response-submit-popup"]').or_(page.get_by_role("button", name="Отправить")).or_(page.get_by_role("button", name="Send application"))
        if submit_btn.count() > 0:
            submit_btn.first.click()
            page.wait_for_timeout(3000)
            return True
        else:
            print("Submit button not found.")
            return False
            
    except Exception as e:
        print(f"Error processing {url}: {e}")
        return False

def main():
    with open(VACANCIES_FILE, 'r', encoding='utf-8') as f:
        vacancies = json.load(f)
        
    with sync_playwright() as p:
        print("Connecting to Chrome CDP...")
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            # Open a new page for background processing so we don't disrupt the user's active tab too much
            page = context.new_page()
            
            applied_count = 0
            for vac in vacancies:
                if not vac.get('applied', False) and 'hh.ru' in vac.get('url', ''):
                    success = process_vacancy(page, vac['url'], vac.get('cover_letter_lang', 'ru'))
                    if success:
                        vac['applied'] = True
                        applied_count += 1
                        # Save progress
                        with open(VACANCIES_FILE, 'w', encoding='utf-8') as fw:
                            json.dump(vacancies, fw, indent=2, ensure_ascii=False)
                        print(f"Successfully applied to {vac['title']}.")
                    
                    # Wait 60 seconds between applications to avoid anti-bot triggers
                    print("Waiting 60 seconds before next application...")
                    time.sleep(60)
                    
            print(f"Finished! Applied to {applied_count} new hh.ru vacancies.")
            page.close()
            browser.close()
        except Exception as e:
            print(f"Main Loop Error: {e}")

if __name__ == "__main__":
    main()