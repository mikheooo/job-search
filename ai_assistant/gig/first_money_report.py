"""Render the reviewed prepare-only queue. Unreviewed candidates stay in BACKUP."""
from __future__ import annotations

from datetime import datetime, timezone


def render_report(run: dict, reviews: dict[str, dict], metrics: dict) -> str:
    candidates = run.get("candidates", [])
    passed = [e for e in candidates if reviews.get(e["project_id"], {}).get("verdict") == "PASS"]
    backups = run.get("backup", []) + [e for e in candidates if e not in passed]
    lines = ["| # | first_money_score | project | budget | live bids | age | capability | client risk | decision |",
             "|---:|---:|---|---|---:|---|---|---|---|"]
    current = datetime.fromisoformat(run["generated_at"].replace("Z", "+00:00"))
    for index, e in enumerate(passed, 1):
        try:
            age = (current - datetime.fromisoformat(e["published_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            age_text = f"{age:.1f} h"
        except (ValueError, TypeError):
            age_text = "unknown"
        lines.append(f"| {index} | {e['score']['first_money_score']} | [{e['title']}]({e['project_url']}) | {e['budget']} | {e['bids_live']} | {age_text} | {e['capability']['fit'].upper()} | {e['client_risk']['level']} | APPLY_FIRST |")
    lines += ["", "# GIG-SEARCH P6 — First Money Run", "",
              f"Проверено: {run['generated_at']}. Свежий сбор: {run['fresh_collection_count']} проектов. APPLY_FIRST: {len(passed)}; BACKUP: {min(10, len(backups))}.", "",
              "Заявки не отправлялись. До подачи откройте страницу ещё раз: статус, бюджет и bids могут измениться. Цена и срок — рекомендации, а не факты объявления.", "",
              "Ручная QA проводится отдельным чтением живой публичной страницы после генерации отклика. Используются текст объявления и публичные данные проекта; авторизация и возможность отправить заявку с вашего аккаунта не проверены.", ""]
    if len(passed) < 3:
        lines += ["**P6 НЕ ЗАВЕРШЁН:** меньше трёх кандидатов прошли все требования и ручную QA. Список не дополнен неподтверждёнными заказами.", ""]
    for index, e in enumerate(passed, 1):
        p = e["proposal_pack"]
        r = reviews[e["project_id"]]
        lines += [f"## {index}. PROJECT — {e['title']}", "",
                  f"**LIVE STATUS:** {e['project_status']} · verified_at: {e['verified_at']}", "",
                  f"**PROJECT URL:** [{e['title']}]({e['project_url']})", "",
                  "**WHY THIS ONE:** " + r["why"], "", "**ACTUAL REQUIREMENTS:**", ""]
        lines += [f"- {text}" for text in r["requirements"]]
        lines += ["", f"**CAPABILITY FIT:** {e['capability']['fit'].upper()}. {r['capability_evidence']}", "",
                  "**CLIENT INFO:** " + r["client_info"], "",
                  f"**MONEY:** бюджет {e['budget']}; оценка объёма P6 {e['estimated_effort_hours']} ч; эффективная ставка середины бюджета ${e['effective_rate_usd']}/ч. " + r["economics"], "",
                  f"**LIVE COMPETITION:** discovery={e['bids_at_discovery']}; live={e['bids_live']}; delta={e['bids_delta']}. QA: {r['checked_at']}", "",
                  "**RISKS:**", ""]
        lines += [f"- {text}" for text in p["RISKS"]]
        bid = p["BID"]
        lines += ["", f"**RECOMMENDED BID:** {bid['recommended_bid']} {bid.get('currency')}" + ("/hour" if bid.get("hourly") else "") + ". " + bid["basis"], "",
                  f"**DELIVERY TIME:** {bid['delivery_time']} после согласования объёма и получения исходных данных/доступов.", ""]
        lines += [f"- {m}" for m in bid["milestones"]]
        lines += ["", "**TECHNICAL APPROACH:**", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(p["TECHNICAL_APPROACH"], 1)]
        lines += ["", "**PROPOSAL:**", "", "```text", p["PROPOSAL"], "```", "",
                  "**CLARIFYING QUESTION:** " + p["CLARIFYING_QUESTION"], "",
                  "**PROOF IDEA:** " + p["PROOF_IDEA"], "",
                  "**QA: PASS.** " + r["evidence"], "",
                  "**Score breakdown:**", ""]
        lines += [f"- {name}: {value:.2f} × {e['score']['weights'][name]:g}; {e['score']['notes'].get(name, '')}" for name, value in e["score"]["components"].items()]
        lines += ["", f"Client multiplier: {e['score']['notes'].get('client_risk', e['client_risk']['level'])}", ""]
    lines += ["## BACKUP", ""]
    for e in backups[:10]:
        reason = "; ".join(e.get("reasons") or []) or "below APPLY_FIRST rank / manual QA pending"
        lines.append(f"- [{e['title']}]({e.get('project_url', '')}) — {e['score']['first_money_score']}; {e['capability']['fit'].upper()}; {reason}")
    if not backups:
        lines.append("Нет подтверждённых резервных кандидатов.")
    lines += ["", "## SKIP", ""]
    for e in run.get("skip", []):
        lines.append(f"- {e['title']} — {'; '.join(e.get('reasons') or ['below threshold'])}")
    lines += ["", "## Money Funnel Metrics", "", "| Metric | Value |", "|---|---:|"]
    lines += [f"| {key} | {value if value is not None else 'undefined (no submissions)'} |" for key, value in metrics.items()]
    lines += ["", "Выручка означает записанный реальный платёж, а не awarded budget. Нулевые события воронки не заменяются прогнозом.", ""]
    return "\n".join(lines)
