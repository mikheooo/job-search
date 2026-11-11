"""Measure how automated a browser looks, from the page's own point of view.

Answers one question: if hh.ru ran bot-detection on this browser right now,
what would it see? Run it against every browser candidate and compare, rather
than trusting marketing claims about "human-like fingerprints".

Usage:
    python tools/browser_fingerprint_probe.py                 # auto-detect
    python tools/browser_fingerprint_probe.py --cdp 9110 BrowserOS
    python tools/browser_fingerprint_probe.py --playwright-headless
    python tools/browser_fingerprint_probe.py --playwright-headful

Read-only: opens about:blank, reads navigator/window, closes nothing it opened
when given an existing CDP endpoint.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

# The signals that actually matter for detection, in rough order of how loudly
# they scream "automation":
#   webdriver=true      -> dead giveaway, set by --enable-automation
#   HeadlessChrome in UA-> obviously headless
#   no window.chrome    -> Playwright/Selenium builds strip it
#   notifications=denied+prompt -> inconsistent permission state
#   zero-size window    -> headless giveaway
PROBE_JS = """
(async () => {
  const nav = navigator;
  const out = {};
  out.webdriver = nav.webdriver;
  out.userAgent = nav.userAgent;
  out.headless_ua = /HeadlessChrome/.test(nav.userAgent);
  out.platform = nav.platform;
  out.vendor = nav.vendor;
  out.languages = nav.languages ? Array.from(nav.languages) : null;
  out.plugins = nav.plugins ? nav.plugins.length : null;
  out.mimeTypes = nav.mimeTypes ? nav.mimeTypes.length : null;
  out.hardwareConcurrency = nav.hardwareConcurrency;
  out.deviceMemory = nav.deviceMemory === undefined ? null : nav.deviceMemory;
  out.chrome_runtime = !!(window.chrome && window.chrome.runtime);
  out.chrome_loadTimes = !!(window.chrome && window.chrome.loadTimes);
  out.outer = [window.outerWidth, window.outerHeight];
  out.inner = [window.innerWidth, window.innerHeight];
  out.maxTouchPoints = nav.maxTouchPoints;
  out.doNotTrack = nav.doNotTrack === undefined ? null : nav.doNotTrack;
  try {
    const p = await nav.permissions.query({name: 'notifications'});
    out.notifications = p.state;
  } catch (e) { out.notifications = 'unavailable'; }
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) { out.webgl = null; }
    else {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      out.webgl = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                      : gl.getParameter(gl.RENDERER);
    }
  } catch (e) { out.webgl = 'error: ' + e; }
  return JSON.stringify(out);
})()
"""

RISK_KEYS = ["webdriver", "headless_ua", "chrome_runtime", "notifications", "outer"]


def _opener():
    """Always talk to CDP directly, never through an HTTP proxy.

    urlopen() honours http_proxy/HTTP_PROXY. A local CDP port going through a
    proxy comes back as "502 Bad Gateway" instead of "connection refused",
    which makes a dead browser look like a broken endpoint and vice versa.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str):
    with _opener().open(url, timeout=3) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


async def _cdp_eval(ws_url: str, expr: str, await_promise: bool = True):
    import websockets

    async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.enable",
        }))
        await ws.recv()
        await ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": await_promise,
                       "returnByValue": True},
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if msg.get("id") == 2:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(str(res["exceptionDetails"]))
                return res.get("result", {}).get("value")


def probe_cdp(port: int, label: str) -> dict | None:
    base = f"http://127.0.0.1:{port}"
    try:
        version = _http_json(f"{base}/json/version")
    except Exception as e:
        print(f"  [skip] {label} ({base}): {type(e).__name__}: {e}")
        return None

    try:
        targets = _http_json(f"{base}/json/list")
    except Exception:
        targets = []
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        # Open a throwaway tab rather than hijacking whatever the user has open.
        req = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                page = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            print(f"  [skip] {label}: cannot open tab ({type(e).__name__})")
            return None

    try:
        raw = asyncio.run(_cdp_eval(page["webSocketDebuggerUrl"], PROBE_JS))
        data = json.loads(raw)
    except Exception as e:
        print(f"  [skip] {label}: eval failed ({type(e).__name__}: {e})")
        return None

    return {
        "label": label,
        "browser": version.get("Browser", "?"),
        "protocol": version.get("Protocol-Version", "?"),
        "data": data,
    }


def probe_playwright(headless: bool, channel: str | None = None) -> dict | None:
    label = f"playwright {'headless' if headless else 'headful'}" + (
        f" [{channel}]" if channel else "")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  [skip] {label}: {type(e).__name__}")
        return None

    try:
        with sync_playwright() as p:
            kwargs = {"headless": headless}
            if channel:
                kwargs["channel"] = channel
            browser = p.chromium.launch(**kwargs)
            page = browser.new_page()
            raw = page.evaluate(PROBE_JS)
            data = json.loads(raw)
            ver = browser.version
            browser.close()
    except Exception as e:
        print(f"  [skip] {label}: {type(e).__name__}: {e}")
        return None

    return {"label": label, "browser": f"chromium {ver}", "protocol": "-", "data": data}


def verdict(d: dict) -> tuple[str, list[str]]:
    """Return (risk level, reasons). Deliberately blunt."""
    reasons = []
    score = 0
    if d.get("webdriver") is True:
        reasons.append("navigator.webdriver = true")
        score += 3
    if d.get("headless_ua"):
        reasons.append("HeadlessChrome in User-Agent")
        score += 3
    # chrome.runtime is NOT a usable signal: real Chrome does not expose it on
    # about:blank, so it reads as missing on genuine browsers too. chrome.loadTimes
    # does survive and is present in Chrome/BrowserOS but absent in Playwright.
    if not d.get("chrome_loadTimes"):
        reasons.append("window.chrome.loadTimes missing")
        score += 1
    if d.get("notifications") == "denied":
        reasons.append("notifications permission = denied")
        score += 1
    outer = d.get("outer") or [0, 0]
    if outer[0] == 0 or outer[1] == 0:
        reasons.append(f"outer window size {outer}")
        score += 2
    if not d.get("plugins"):
        reasons.append("no navigator.plugins")
        score += 1
    # A software rasteriser means no GPU, which means a container or headless.
    renderer = str(d.get("webgl") or "")
    if any(s in renderer for s in ("SwiftShader", "llvmpipe", "Software", "Mesa OffScreen")):
        reasons.append(f"software WebGL renderer: {renderer[:60]}")
        score += 2

    level = "CLEAN" if score == 0 else ("SUSPICIOUS" if score <= 2 else "DETECTED")
    return level, reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", nargs=2, action="append", metavar=("PORT", "LABEL"))
    ap.add_argument("--playwright-headless", action="store_true")
    ap.add_argument("--playwright-headful", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = []
    jobs: list[tuple[int, str]] = [(int(p), l) for p, l in (args.cdp or [])]
    if not jobs:
        jobs = [(9222, "Chrome"), (9110, "BrowserOS"), (9010, "alt-9010"), (9210, "alt-9210")]

    print("Scanning CDP endpoints...")
    for port, label in jobs:
        r = probe_cdp(port, label)
        if r:
            results.append(r)

    if args.playwright_headless or not (args.cdp or args.playwright_headful):
        print("Scanning Playwright (this is what form extraction uses)...")
        r = probe_playwright(True)
        if r:
            results.append(r)
    if args.playwright_headful:
        r = probe_playwright(False)
        if r:
            results.append(r)

    if not results:
        print("\nNo browser reachable. Start one, then re-run.")
        return 1

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    print("\n" + "=" * 78)
    print(f"{'TARGET':<34}{'VERDICT':<12}WEBDRIVER")
    print("=" * 78)
    for r in results:
        lvl, _ = verdict(r["data"])
        print(f"{r['label']:<34}{lvl:<12}{r['data'].get('webdriver')}")
    print("=" * 78)

    for r in results:
        lvl, reasons = verdict(r["data"])
        print(f"\n--- {r['label']} --- {r['browser']}")
        print(f"    verdict: {lvl}")
        for reason in reasons:
            print(f"      ! {reason}")
        if not reasons:
            print("      no automation markers found")
        d = r["data"]
        print(f"    UA        : {d.get('userAgent')}")
        print(f"    window    : outer={d.get('outer')} inner={d.get('inner')}")
        print(f"    chrome    : runtime={d.get('chrome_runtime')} plugins={d.get('plugins')}")
        print(f"    notif     : {d.get('notifications')}   cores={d.get('hardwareConcurrency')}")
        print(f"    webgl     : {d.get('webgl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
