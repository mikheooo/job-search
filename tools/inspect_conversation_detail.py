import json
import time
import sys
sys.stdout.reconfigure(encoding='utf-8')
from ai_assistant.prefill_execute import make_cdp_evaluate, probe_frames_and_world
from ai_assistant.hh_browser_launcher import ensure_hh_browser

ensure_hh_browser()

ev = make_cdp_evaluate("http://127.0.0.1:9222", "hh.ru")
ev("location.href = 'https://hh.ru/chat/5585426482'")

time.sleep(3)

js = """(() => {
    const messages = Array.from(document.querySelectorAll('[class*="message"], [data-qa*="message"], [class*="bubble"]')).map(el => ({
        tag: el.tagName,
        qa: el.getAttribute('data-qa'),
        className: el.className,
        text: (el.innerText || '').trim()
    })).filter(m => m.text && m.text.length < 500);

    const allTextBlocks = Array.from(document.querySelectorAll('div, p, span')).filter(el => {
        const t = (el.innerText || '').trim();
        return t.includes('Михаил Кириллович') || t.includes('Здравствуйте') || t.includes('резюме');
    }).map(el => ({
        tag: el.tagName,
        className: el.className,
        qa: el.getAttribute('data-qa'),
        text: el.innerText.trim()
    }));

    const composer = document.querySelector('textarea, [contenteditable="true"], [data-qa*="composer"], [data-qa*="input"]');

    return JSON.stringify({
        url: location.href,
        title: document.title,
        bodySnippet: (document.body.innerText || '').slice(0, 600),
        messagesSample: messages.slice(0, 15),
        textBlocks: allTextBlocks.slice(0, 5),
        composer: composer ? {tag: composer.tagName, qa: composer.getAttribute('data-qa')} : null
    });
})()"""

raw = ev(js)
print("=== CONVERSATION 5585426482 DETAIL ===")
print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
