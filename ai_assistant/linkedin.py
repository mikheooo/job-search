import os
import logging
import time
import requests
import config

logger = logging.getLogger(__name__)


def _headers() -> dict:
    key = config.UNIPILE_API_KEY
    if not key:
        raise RuntimeError("UNIPILE_API_KEY is not set")
    headers = {
        "accept": "application/json",
        "X-API-KEY": key,
    }
    if config.UNIPILE_DSN:
        headers["X-DSN"] = config.UNIPILE_DSN
    return headers


def list_accounts() -> list[dict]:
    url = f"{config.UNIPILE_BASE_URL}/accounts"
    resp = requests.get(url, headers=_headers(), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("items") or data.get("data") or []
    if isinstance(data, list):
        return data
    return []


def find_linkedin_account_id() -> str | None:
    accounts = list_accounts()
    for acc in accounts:
        provider = (acc.get("provider") or acc.get("provider_name") or "").lower()
        if provider == "linkedin":
            return str(acc.get("id") or acc.get("account_id") or "")
    # fallback: first account
    if accounts:
        return str(accounts[0].get("id") or accounts[0].get("account_id") or "")
    return None


def send_connection_request(account_id: str, linkedin_user_id: str, note: str) -> dict:
    url = f"{config.UNIPILE_BASE_URL}/accounts/{account_id}/invitations"
    payload = {
        "provider": "LINKEDIN",
        "recipient_id": linkedin_user_id,
        "message": note,
    }
    logger.info("POST %s payload=%s", url, payload)
    resp = requests.post(url, headers=_headers(), json=payload, timeout=20)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        logger.warning("Rate limited, sleeping %s", retry_after)
        time.sleep(retry_after)
        return send_connection_request(account_id, linkedin_user_id, note)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


def send_connection_request_by_public_identifier(account_id: str, public_identifier: str, note: str) -> dict:
    url = f"{config.UNIPILE_BASE_URL}/accounts/{account_id}/invitations"
    payload = {
        "provider": "LINKEDIN",
        "public_identifier": public_identifier,
        "message": note,
    }
    logger.info("POST %s payload=%s", url, payload)
    resp = requests.post(url, headers=_headers(), json=payload, timeout=20)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        logger.warning("Rate limited, sleeping %s", retry_after)
        time.sleep(retry_after)
        return send_connection_request_by_public_identifier(account_id, public_identifier, note)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


def resolve_decision_maker(company_name: str, titles: list[str] | None = None) -> dict | None:
    account_id = find_linkedin_account_id()
    if not account_id:
        logger.error("No LinkedIn account connected in Unipile")
        return None

    titles = titles or ["CTO", "Head of Engineering", "VP Engineering", "Director of Operations", "Talent Acquisition", "Hiring Manager"]
    for title in titles:
        params = {
            "account_id": account_id,
            "q": title,
            "company_name": company_name,
        }
        url = f"{config.UNIPILE_BASE_URL}/users/search"
        logger.info("Searching LinkedIn: %s at %s", title, company_name)
        resp = requests.get(url, headers=_headers(), params=params, timeout=20)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            time.sleep(retry_after)
            continue
        if resp.status_code != 200:
            logger.warning("Search failed %s: %s", resp.status_code, resp.text[:200])
            continue

        try:
            data = resp.json()
        except Exception:
            continue

        identifier = data.get("public_identifier") or ""
        if identifier and identifier.lower() != "search":
            return {
                "public_identifier": identifier,
                "provider_id": data.get("provider_id") or "",
                "first_name": data.get("first_name", ""),
                "last_name": data.get("last_name", ""),
                "headline": data.get("headline", ""),
                "title_used": title,
            }

    logger.warning("No decision maker found for %s", company_name)
    return None
