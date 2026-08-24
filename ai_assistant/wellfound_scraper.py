import json
import logging
import time
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import requests
import config

logger = logging.getLogger(__name__)

WEWORKREMOTELY = "https://weworkremotely.com/categories/remote-programming-jobs"


def _parse_wwr_item(el) -> dict | None:
    try:
        href_el = el.query_selector("a[href*='/remote-jobs/']")
        href = (href_el.get_attribute("href") or "") if href_el else ""
        if href and not href.startswith("http"):
            from urllib.parse import urljoin
            href = urljoin("https://weworkremotely.com", href)

        title_el = el.query_selector("h3.new-listing__header__title span.new-listing__header__title__text")
        company_el = el.query_selector("p.new-listing__company-name, .company-name")

        title = (title_el.inner_text() if title_el else "").strip()
        company = (company_el.inner_text() if company_el else "").strip()

        if not title:
            return None

        return {
            "id": f"wwr_{int(time.time()*1000)}_{hash(title+company)%10000}",
            "title": title,
            "company": company,
            "location": "",
            "url": href,
            "description": "",
            "salary": "",
            "source": "weworkremotely",
        }
    except Exception:
        return None


def scrape_weworkremotely(limit: int = 20) -> list[dict]:
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(WEWORKREMOTELY, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            items = page.query_selector_all("li.new-listing-container:not(.listing-ad)")
            logger.info("WWR items found: %s", len(items))

            for item in items[:limit]:
                job = _parse_wwr_item(item)
                if job:
                    jobs.append(job)
        except PlaywrightTimeout:
            logger.error("WWR timeout")
        except Exception as e:
            logger.error("WWR scrape error: %s", e)
        finally:
            try:
                page.close()
            except Exception:
                pass
    logger.info("Scraped %s jobs from Weworkremotely", len(jobs))
    return jobs


def save_jobs(jobs: list[dict], path: str = None) -> str:
    path = path or config.VACANCIES_FILE
    existing = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Cannot read existing vacancies: %s", e)

    existing_ids = {str(j.get("id")) for j in existing}
    new = [j for j in jobs if str(j.get("id")) not in existing_ids]
    existing.extend(new)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info("Saved %s new jobs, total %s", len(new), len(existing))
    return path


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    jobs = scrape_weworkremotely(limit=10)
    if jobs:
        save_jobs(jobs)
        print(json.dumps(jobs[:3], ensure_ascii=False, indent=2))
    else:
        print("No jobs scraped")
