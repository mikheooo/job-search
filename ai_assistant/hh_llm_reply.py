"""Contextual HH reply drafts. No sending and no template fallback."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from . import config


def antigravity_complete(*, messages: list[dict[str, str]], **_kwargs: Any) -> Any:
    """Use the installed CLI in text-only mode, with all tool categories denied."""
    if os.environ.get("JOB_SEARCH_TEST_NETWORK_BLOCKED") == "1":
        # Disallow real external agy.exe process under unit tests if subprocess.run is unmocked
        if getattr(subprocess.run, "__module__", "") == "subprocess":
            raise RuntimeError("Unmocked live LLM CLI subprocess blocked under unit test environment")
    agent = Path(config.PROJECT_ROOT) / '.agents' / 'agents' / 'hh-response-only.md'
    if not agent.is_file():
        raise RuntimeError('text-only Antigravity agent is missing')
    settings = {
        'toolPermission': 'strict', 'allowNonWorkspaceAccess': False,
        'historySize': 0,
        'permissions': {'deny': ['read_file(*)', 'write_file(*)', 'read_url(*)',
                                  'execute_url(*)', 'command(*)', 'mcp(*)']},
    }
    payload = '\n\n'.join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
    request = json.dumps({'event': 'user', 'message': {'role': 'user', 'content': payload}},
                         ensure_ascii=False) + '\n'
    executable = Path(os.path.expanduser('~')) / 'AppData' / 'Local' / 'agy' / 'bin' / 'agy.exe'
    args = [str(executable), '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--model', 'gemini-3.8-flash-low', '--agent', 'hh-response-only',
            '--settings', json.dumps(settings), '--sandbox', '--print-timeout', '45s', '--print=']
    proc = subprocess.run(args, input=request, capture_output=True, encoding='utf-8',
                          errors='replace', timeout=55, cwd=config.PROJECT_ROOT,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    final = None
    init_ok = False
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event.get('event') == 'init':
            info = event.get('init') or {}
            init_ok = info.get('agent') == 'hh-response-only' and info.get('permission_mode') == 'strict'
        if event.get('event') == 'step_update':
            step = event.get('step_update') or {}
            if 'tool' in str(step.get('step_type', '')).lower():
                raise RuntimeError('unexpected tool attempt')
        if event.get('event') == 'result':
            final = event.get('result') or {}
    if proc.returncode != 0 or not init_ok or not final or final.get('status') != 'SUCCESS':
        raise RuntimeError('Antigravity did not return a completed response')
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=final.get('response') or ''), finish_reason='stop')])


def generate_contextual_draft(dialog: Any, complete: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Generate from recent conversation and the local KB; fail closed on model errors."""
    messages = list(getattr(dialog, 'messages', []) or [])
    if not messages or messages[-1].sender != 'employer' or not (messages[-1].text or '').strip():
        return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'no pending employer message'}
    kb_path = Path(config.PROJECT_ROOT) / 'candidate_kb.json'
    try:
        kb = json.loads(kb_path.read_text(encoding='utf-8'))
        claims = kb.get('claims', {})
        values = list(claims.values()) if isinstance(claims, dict) else claims
        facts = [str(c.get('value', ''))[:1200] for c in values
                 if isinstance(c, dict) and c.get('status') == 'CONFIRMED_BY_USER' and c.get('value')]
    except (OSError, ValueError, TypeError):
        return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'candidate knowledge base unavailable'}
    if not facts:
        return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'no candidate facts available'}
    history = [{'role': 'assistant' if m.sender == 'candidate' else 'user',
                'content': (m.text or '')[:2500]}
               for m in messages[-12:] if m.sender in ('candidate', 'employer') and (m.text or '').strip()]
    prompt = ('Ты помогаешь Михаилу вести переписку о работе. Ответь на последнее сообщение '
              'по существу, учитывая историю и профиль. Пиши кратко, естественно, без '
              'повторного приветствия и без общих шаблонов. Отвечай уверенно и '
              'позитивно, показывая применимость смежного опыта и готовность быстро '
              'разобраться с конкретной технологией. Если прямой опыт не указан, '
              'пользователь просит описывать знакомство с технологией уверенно и '
              'умеренно, без категоричных отказов и без утверждений о крупных '
              'внедрениях. Не придумывай названия компаний, '
              'конкретные сроки, масштаб проектов или количественные достижения. '
              'Профиль может содержать '
              'противоречия: Speedcast/Germsat март 2018 — март 2021; с мая 2026 '
              'собственные проекты без оплачиваемого фриланса. '
              'По зарплатным ожиданиям называй целевой ориентир 130 000 – 180 000 руб. на руки '
              '(открыт к обсуждению под конкретный пул задач), а не нижний порог фильтра 100 000. '
              'Выведи только завершённый текст ответа.\nПрофиль:\n' + '\n'.join(facts)[:20000]
              + '\nВакансия: ' + str(getattr(dialog, 'vacancy_title', ''))[:500]
              + '\nКомпания: ' + str(getattr(dialog, 'employer', ''))[:300])
    try:
        if complete is None:
            complete = antigravity_complete
        response = complete(model=config.LLM_MODEL,
                            messages=[{'role': 'system', 'content': prompt}, *history],
                            max_tokens=300, temperature=0.3)
        choice = response.choices[0]
        text = (choice.message.content or '').strip().strip('"«»')
        if getattr(choice, 'finish_reason', None) != 'stop' or len(text) < 20 or len(text) > 2000:
            return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'model output incomplete or invalid'}
        if text[-1] not in '.!?…»:':
            return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': 'model output appears truncated'}
        return {'status': 'DRAFT', 'reply': text, 'reason': 'LLM draft; user review required'}
    except Exception as exc:
        return {'status': 'NEEDS_REVIEW', 'reply': '', 'reason': f'LLM unavailable: {type(exc).__name__}'}
