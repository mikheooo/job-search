import sys
sys.path.insert(0, r"c:\Users\Misha\Documents\job-search\.venv\Lib\site-packages")
import asyncio
import json
import urllib.request
import websockets

async def cdp_call(ws, method, params=None, msg_id=1):
    req = {"id": msg_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(req))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == msg_id:
            return resp

async def run():
    targets = json.loads(urllib.request.urlopen('http://127.0.0.1:9111/json').read())
    hh = next(t for t in targets if 'hh.ru' in t.get('url', '') and t.get('type') == 'page')
    async with websockets.connect(hh['webSocketDebuggerUrl']) as ws:
        # Find the first "Перейти в чат" button
        res = await cdp_call(ws, "Runtime.evaluate", {
            "expression": """
            (() => {
                let btns = [];
                document.querySelectorAll('button, a, div[role="button"]').forEach(el => {
                    if (el.innerText && el.innerText.trim() === 'Перейти в чат') {
                        let card = el.closest('div[data-qa*="negotiation"], [class*="negotiation"], [class*="card"], tr') || el.parentElement.parentElement.parentElement;
                        btns.push({
                            tagName: el.tagName,
                            outerHTML: el.outerHTML.slice(0, 300),
                            qa: el.getAttribute('data-qa'),
                            cardTitle: card ? (card.querySelector('a[href*="/vacancy/"]') ? card.querySelector('a[href*="/vacancy/"]').innerText.trim() : card.innerText.slice(0, 50)) : ''
                        });
                    }
                });
                return btns;
            })()
            """,
            "returnByValue": True
        }, msg_id=1)
        print("Buttons found:")
        print(json.dumps(res.get("result", {}).get("result", {}).get("value"), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(run())
