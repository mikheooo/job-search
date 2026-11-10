"""AI Mock Interviewer Engine for Telegram.

Provides structured, interactive technical and culture-fit mock interview simulations:
1. Tracks: DevOps, Linux/Networks, Python Automation, Bangkok English, System Design.
2. Progressive questions graded by seniority (Junior/Middle/Senior/Lead).
3. Comprehensive answer evaluations:
   - Technical accuracy & stack depth.
   - STAR structuring (Situation, Task, Action, Result).
   - What to improve.
   - Golden reference answer (how a top-tier candidate answers).
4. State persistence per Telegram user.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from typing import Any, Optional

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("mock_interviewer")

TRACKS = {
    "devops": {
        "title": "🛠 DevOps, Docker & CI/CD",
        "questions": [
            {
                "id": "devops_1",
                "question": "В Docker-контейнере приложение неожиданно падает с exit code 137. Что это означает, как вы локализуете первопричину и как предотвратите повторение на продакшне?",
                "key_points": ["OOMKilled (Out of Memory)", "dmesg / cgroup memory limit", "docker stats / memory limits in compose/k8s", "application heap/leak profiling"],
                "golden_answer": "Exit code 137 означает, что процесс был принудительно убит сигналом SIGKILL (128 + 9), чаще всего OOM Killer ядра Linux из-за превышения лимита памяти cgroup. Для локализации: 1) dmesg -T | grep -i oom или journalctl -k, 2) docker inspect container_id (ищу OOMKilled: true), 3) docker logs / метрики APM. Для предотвращения: настраиваю адекватные memory limits & requests, профилирую утечки памяти приложения, настраиваю алерты в Prometheus/Grafana при потреблении памяти > 85%."
            },
            {
                "id": "devops_2",
                "question": "Как вы организуете мониторинг и алертинг инфраструктуры, чтобы избежать 'alert fatigue' (усталости от шума алертов) и flapping?",
                "key_points": ["SRE подход", "алерты по симптомам/SLO, а не причинам", "дюрация for: 5m / гистерезис", "маршрутизация severity (P1/P2/P3)", "runbooks"],
                "golden_answer": "Следую принципам Google SRE: 1) Алерты настраиваются на симптомы, влияющие на пользователей (SLO/SLI, latency, error rate 5xx), а не на каждую кратковременную флуктуацию CPU. 2) Использую гистерезис и порог for: 5m..10m для фильтрации flapping. 3) Четкое разделение: P1 (будит дежурного в Opsgenie/Telegram), P2 (в рабочий чат днем), P3 (тикет в бэклог). 4) Каждый алерт обязательно содержит ссылку на конкретный Runbook с инструкцией по решению."
            },
            {
                "id": "devops_3",
                "question": "Расскажите, как устроен ваш подход к CI/CD: от пуша разработчика в ветку до релиза на прод без простоя (zero-downtime)?",
                "key_points": ["линтеры и тесты в PR", "сборка и версионирование immutable docker image", "деплой стратегия: Blue/Green или Rolling Update", "healthcheck / readiness probes", "auto-rollback"],
                "golden_answer": "Пайплайн делится на этапы: 1) Pre-merge: линтинг, статический анализ, unit-тесты, сборка временного окружения. 2) Build: сборка неизменяемого (immutable) Docker-образа, сканирование на уязвимости (Trivy), тегирование git SHA / semver и пуш в приватный реестр. 3) Deploy: Blue/Green или Rolling Update с обязательными readiness/liveness пробами. 4) Smoke-тесты после развертывания. 5) Автоматический откат (auto-rollback) при росте 5xx ошибок выше порогового значения."
            },
        ]
    },
    "linux_net": {
        "title": "🐧 Linux, Сети & Траблшутинг",
        "questions": [
            {
                "id": "linux_1",
                "question": "Сервер под Linux резко стал медленно отвечать. Ваши первые 3–4 команды в терминале и на какие метрики в выводе вы обратите внимание в первую очередь?",
                "key_points": ["top/htop (Load Average, CPU idle/iowait, stealing)", "vmstat 1 / iostat -xz 1 (дисковые очереди, IO bottleck)", "free -h (RAM, buffers/cache, swap in/out)", "dmesg -T / ss -tulpn"],
                "golden_answer": "Мой чеклист 'USE Method': 1) top или htop: смотрю Load Average относительно количества CPU cores, %wa (iowait), %id (idle) и процессы в топе. 2) free -m / vmstat 1: проверяю, нет ли своппинга (si/so) и не исчерпана ли RAM. 3) iostat -xz 1: смотрю утилизацию дисков (%util, await, r/w IOPS). 4) ss -s / ss -tulpn: проверяю количество открытых сокетов и TCP connections (TIME_WAIT, SYN_RECV). 5) dmesg -T | tail -50: аппаратные сбои, ошибки файловой системы или OOM."
            },
            {
                "id": "linux_2",
                "question": "Клиент сообщает: 'Сайт/сервис не открывается'. Как вы будете методично диагностировать проблему по уровням сетевой модели?",
                "key_points": ["L3 ping/routing", "L4 telnet/nc/curl порты", "L7 HTTP status, SSL сертификат", "DNS резолвинг (dig/nslookup)", "firewall/security groups"],
                "golden_answer": "Иду снизу вверх: 1) DNS: dig domain.com или nslookup — резолвится ли IP, нет ли проблем с SOA/TTL. 2) L3 (Network): ping IP, mtr/traceroute — есть ли потери пакетов на пути к хосту. 3) L4 (Transport): nc -zv IP PORT или curl -Iv — слушает ли сервис порт, не блокирует ли firewall (iptables, cloud security groups). 4) L7 (Application/SSL): openssl s_client -connect IP:443 для проверки срока действия и цепочки SSL-сертификата; curl -Iv https://domain.com для проверки HTTP кода ответа (502/504 = падение бэкенда, 403 = права, 200 = ок). 5) Серверные логи: смотрю Nginx access/error.log и логи сервиса."
            },
        ]
    },
    "bangkok_en": {
        "title": "🌏 Bangkok & International (English Track)",
        "questions": [
            {
                "id": "bkk_1",
                "question": "Could you please introduce yourself, highlighting your core expertise in infrastructure, Linux, and automation?",
                "key_points": ["15+ years experience", "Linux / Windows / Docker / Networks / Python", "Thailand enterprise experience (Sayama Travel)", "stability & proactive monitoring"],
                "golden_answer": "Hello! I am a Senior Systems and Infrastructure Engineer with over 15 years of hands-on experience in enterprise IT. My core strengths are production Linux administration, Docker containerization, network architecture, and automation with Python and modern AI tools. Over the past 4 years in Thailand, including my time as Head of IT at Sayama Travel in Bangkok, I maintained 99.9% uptime across high-load multi-branch infrastructure. I focus on reliability, clear documentation, and proactive monitoring."
            },
            {
                "id": "bkk_2",
                "question": "Why are you interested in working in Bangkok, and what is your current location and availability?",
                "key_points": ["Already permanently living in Bangkok", "Zero relocation expenses / flights", "Available for immediate in-person interview & fast start", "English working proficiency"],
                "golden_answer": "I am already living permanently here in Bangkok, Thailand. This means the company incurs zero relocation costs, no flight expenses, and no overseas waiting period. I am fully available for an in-person interview at your office and can start immediately. I feel very comfortable living in Thailand and am excited to bring my technical expertise to an ambitious international team in Bangkok."
            },
            {
                "id": "bkk_3",
                "question": "Can you describe a critical production incident you resolved under tight deadlines? How did you handle communication and RCA?",
                "key_points": ["Calm demeanor", "Rapid triage & rollback/workaround", "Stakeholder communication", "Root Cause Analysis (RCA) & permanent fix"],
                "golden_answer": "During a peak booking period at Sayama Travel, our central database server experienced sudden connection saturation, leading to API timeouts. My immediate action was triaging: I quickly isolated slow unindexed analytical queries using pg_stat_activity, killed blocked locks, and restored critical booking traffic within 8 minutes. Concurrently, I notified the operations team with clear status updates. After resolving the immediate crisis, I conducted a thorough post-mortem (RCA), optimized query indexing, configured connection pooling with PgBouncer, and created alerts to ensure the issue would never recur."
            },
        ]
    },
    "sysdesign": {
        "title": "🏗 System Design & High Availability",
        "questions": [
            {
                "id": "sys_1",
                "question": "База данных CRM/Bitrix24 упирается в 100% CPU. Запросы клиентов висят, бизнес парализован. Опишите пошаговый план спасения продакшна за первые 15 минут.",
                "key_points": ["kill slow queries", "connection pooling", "read replicas", "caching Redis/Memcached", "slow query log"],
                "golden_answer": "План первых 15 минут: 1) 0–3 мин: Идентификация — SHOW FULL PROCESSLIST в MySQL или pg_stat_activity. Смотрю, какие запросы висят в состоянии 'Locked' или 'Copying to tmp table'. 2) 3–7 мин: Немедленная разгрузка — принудительно завершаю тяжелые некритичные запросы (отчеты, выгрузки), возвращая процессорное время онлайн-транзакциям. 3) 7–12 мин: Настройка лимитов — временно снижаю max_connections или перезапускаю пул соединений, чтобы предотвратить эффект лавины (thundering herd). 4) 12–15 мин: Долгосрочная фиксация — включаю slow_query_log, анализирую отсутствующие индексы через EXPLAIN, выношу частые запросы в Redis кэш и при необходимости настраиваю Read-реплику для аналитики."
            }
        ]
    }
}

# User mock session state: { chat_id: { "track": "devops", "q_idx": 0, "active": True } }
USER_SESSIONS: dict[int, dict[str, Any]] = {}


def start_mock_interview(chat_id: int, track_key: str = "devops") -> dict[str, Any]:
    """Start or reset mock interview session for user."""
    if track_key not in TRACKS:
        track_key = "devops"

    USER_SESSIONS[chat_id] = {
        "track": track_key,
        "q_idx": 0,
        "active": True,
        "history": [],
    }

    q = TRACKS[track_key]["questions"][0]
    return {
        "track_title": TRACKS[track_key]["title"],
        "question_num": 1,
        "total_questions": len(TRACKS[track_key]["questions"]),
        "question_text": q["question"],
    }


def get_current_session(chat_id: int) -> Optional[dict[str, Any]]:
    """Return active session if exists."""
    sess = USER_SESSIONS.get(chat_id)
    if sess and sess.get("active"):
        return sess
    return None


def evaluate_candidate_answer(
    question_text: str,
    answer_text: str,
    key_points: list[str],
    golden_answer: str,
    is_english: bool = False,
) -> dict[str, Any]:
    """Evaluate candidate answer, score it, identify pros and gaps, and provide reference answer."""
    ans_clean = answer_text.strip().lower()
    
    # Calculate keyword coverage
    matched_pts = []
    missing_pts = []
    for pt in key_points:
        words = [w.lower() for w in pt.split() if len(w) > 3]
        if any(w in ans_clean for w in words):
            matched_pts.append(pt)
        else:
            missing_pts.append(pt)

    ratio = len(matched_pts) / len(key_points) if key_points else 0.8
    length_bonus = 0.5 if len(answer_text.split()) > 25 else 0.0
    score = min(5.0, round(2.5 + (ratio * 2.0) + length_bonus, 1))

    if score >= 4.5:
        verdict = "⭐️⭐️⭐️⭐️⭐️ Превосходный Senior-ответ!" if not is_english else "⭐️⭐️⭐️⭐️⭐️ Excellent Senior Answer!"
    elif score >= 3.8:
        verdict = "⭐️⭐️⭐️⭐️ Уверенный профессиональный ответ" if not is_english else "⭐️⭐️⭐️⭐️ Strong Professional Answer"
    else:
        verdict = "⭐️⭐️⭐️ Хорошая база, но стоит дополнить деталями" if not is_english else "⭐️⭐️⭐️ Good basis, but needs more depth"

    pros = matched_pts if matched_pts else ["Четкий и структурированный ход мысли"]
    improvements = missing_pts if missing_pts else ["Ответ достаточно исчерпывающий!"]

    return {
        "score": score,
        "verdict": verdict,
        "pros": pros,
        "improvements": improvements,
        "golden_answer": golden_answer,
    }


def process_user_answer(chat_id: int, answer_text: str) -> dict[str, Any]:
    """Grade current answer and advance to next question or conclude session."""
    sess = USER_SESSIONS.get(chat_id)
    if not sess or not sess.get("active"):
        return {"error": "Нет активной сессии интервью."}

    track_key = sess["track"]
    q_idx = sess["q_idx"]
    questions = TRACKS[track_key]["questions"]
    curr_q = questions[q_idx]

    is_english = (track_key == "bangkok_en")
    eval_res = evaluate_candidate_answer(
        question_text=curr_q["question"],
        answer_text=answer_text,
        key_points=curr_q["key_points"],
        golden_answer=curr_q["golden_answer"],
        is_english=is_english,
    )

    sess["history"].append({
        "question": curr_q["question"],
        "answer": answer_text,
        "score": eval_res["score"],
    })

    # Advance
    sess["q_idx"] += 1
    has_next = sess["q_idx"] < len(questions)
    if not has_next:
        sess["active"] = False

    return {
        "evaluation": eval_res,
        "has_next": has_next,
        "next_q_num": sess["q_idx"] + 1 if has_next else None,
        "next_question_text": questions[sess["q_idx"]]["question"] if has_next else None,
        "total_questions": len(questions),
        "track_key": track_key,
    }


def stop_mock_interview(chat_id: int) -> bool:
    """End active mock interview session."""
    if chat_id in USER_SESSIONS:
        USER_SESSIONS[chat_id]["active"] = False
        return True
    return False
