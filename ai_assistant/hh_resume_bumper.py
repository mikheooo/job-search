"""HeadHunter Resume Auto-Bump Engine.

Navigates to https://hh.ru/applicant/resumes via HeadlessBrowserSession,
detects active resumes, inspects the 'Поднять в поиске' button status,
and clicks it whenever HH cooldown permits (typically every 4 hours).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ai_assistant.hh_browser_provider import HeadlessBrowserSession

logger = logging.getLogger("hh_resume_bumper")


def bump_hh_resumes(session: HeadlessBrowserSession | None = None) -> dict[str, Any]:
    """Inspect and bump all eligible applicant resumes on hh.ru."""
    close_when_done = False
    if session is None:
        session = HeadlessBrowserSession(require_auth=True)
        info = session.start()
        if not info.get("ok"):
            err = info.get("message") or info.get("error") or info.get("status") or "unknown"
            logger.error(
                "Cannot start browser session for resume bumper: %s | cdp_url=%s profile=%s",
                err, session.cdp_url, session.profile_dir,
            )
            return {"status": "ERROR", "error": err, "bumped": 0, "resumes": []}
        close_when_done = True

    try:
        tab = session.ensure_work_tab()
        logger.info("Navigating to https://hh.ru/applicant/resumes...")
        session.navigate(tab, "https://hh.ru/applicant/resumes", timeout=25.0)
        time.sleep(3.5)

        # Execute inspection and click logic inside page context
        js_code = """(() => {
            const results = [];
            // Target resume cards on /applicant/resumes or /applicant/profile/me
            const cards = Array.from(document.querySelectorAll('[data-qa="resume"], [data-qa*="applicant-resume"], div[class*="resume-card"], div[data-qa*="resume-card"]'));
            const candidateElements = cards.length ? cards : Array.from(document.querySelectorAll('div[data-qa*="resume-item"], article'));

            // Also search all update buttons on the page
            const updateButtons = Array.from(document.querySelectorAll('[data-qa="resume-update-button"], [data-qa*="resume-update"], button[data-qa*="update"]'));

            let bumpedCount = 0;

            // Strategy 1: Iterate over explicit update buttons
            for (const btn of updateButtons) {
                const text = (btn.innerText || '').trim();
                const isDisabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('bloko-button_disabled');
                
                // Find parent title if possible
                let parentCard = btn.closest('[data-qa*="resume"]') || btn.parentElement;
                let titleEl = parentCard ? parentCard.querySelector('[data-qa*="title"], h3, h2, a[href*="/resume/"]') : null;
                let title = titleEl ? titleEl.innerText.trim() : "Резюме";

                if (!isDisabled && (text.includes("Поднять") || text.includes("Обновить") || text === "")) {
                    try {
                        btn.click();
                        bumpedCount++;
                        results.push({
                            title: title,
                            action: "BUMPED",
                            status: "Успешно поднято в поиске",
                            text: text
                        });
                    } catch (e) {
                        results.push({
                            title: title,
                            action: "ERROR",
                            status: String(e),
                            text: text
                        });
                    }
                } else {
                    results.push({
                        title: title,
                        action: "COOLDOWN",
                        status: text || "Кнопка временно неактивна (кулдаун 4ч)",
                        text: text
                    });
                }
            }

            // Strategy 2: If no buttons found via data-qa, search by text inside candidate elements
            if (updateButtons.length === 0) {
                const allButtons = Array.from(document.querySelectorAll('button, a.bloko-button'));
                for (const btn of allButtons) {
                    const txt = (btn.innerText || '').trim();
                    if (txt.includes("Поднять в поиске") || txt.includes("Обновить дату")) {
                        const isDisabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
                        if (!isDisabled) {
                            btn.click();
                            bumpedCount++;
                            results.push({
                                title: "Резюме",
                                action: "BUMPED",
                                status: "Успешно поднято в поиске",
                                text: txt
                            });
                        } else {
                            results.push({
                                title: "Резюме",
                                action: "COOLDOWN",
                                status: txt,
                                text: txt
                            });
                        }
                    }
                }
            }

            return {
                url: window.location.href,
                title: document.title,
                bumpedCount: bumpedCount,
                resumes: results
            };
        })()"""

        res_raw = session.evaluate(js_code)
        if isinstance(res_raw, str):
            try:
                res_data = json.loads(res_raw)
            except Exception:
                res_data = {}
        elif isinstance(res_raw, dict):
            res_data = res_raw
        else:
            res_data = {}

        bumped = res_data.get("bumpedCount", 0)
        resumes_info = res_data.get("resumes", [])
        logger.info(f"HH Resume Bumper completed: bumped={bumped}, details={resumes_info}")

        return {
            "status": "SUCCESS",
            "bumped": bumped,
            "resumes": resumes_info,
            "page_url": res_data.get("url"),
        }
    except Exception as e:
        logger.error(f"Error while bumping HH resumes: {e}", exc_info=True)
        return {"status": "ERROR", "error": str(e), "bumped": 0, "resumes": []}
    finally:
        if close_when_done:
            session.stop()


if __name__ == "__main__":
    result = bump_hh_resumes()
    print(json.dumps(result, indent=2, ensure_ascii=False))
