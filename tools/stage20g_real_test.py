"""Stage 20G real test: full safe prefill orchestration on the live HH form.

Per spec §8: use a fixture VALID package with >=2 validated operations
(CHECKBOX Claude Code + Cursor) against the already-open HH questionnaire.
Steps: read-only baseline -> execute -> read-only after-snapshot -> compare.
No submit, no navigation, no clicks (direct DOM mutations).
"""
import json, sys, io, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'C:\Users\Misha\Documents\job-search')

from ai_assistant.application_prep import ApplicationPackage, ResumeAdaptation
from ai_assistant.hh_extractor import (
    ApplicationForm, ApplicationQuestion, ApplicationType, QuestionType,
    QuestionSource, ApplicationAnswer,
)
from ai_assistant.prefill_orchestrate import prepare_and_execute_prefill
from ai_assistant.prefill_execute import make_cdp_evaluate
from tools.capture_manual_form import _list_targets, _inspect_target_via_cdp

CDP = 'http://127.0.0.1:9223'
URL_SUBSTR = 'applicant/vacancy_response'

# ---- fixture VALID package (2 validated CHECKBOX answers) ----
form = ApplicationForm(
    source='hh', vacancy_stable_id='hh:136591579',
    application_type=ApplicationType.screening_questions,
    questions=[
        ApplicationQuestion(id='hh__ctrl_task_384589151', label='Какие AI coding агенты?',
                            normalized_type=QuestionType.CHECKBOX, required=False,
                            options=['Claude Code', 'Cursor', 'Свой вариант'],
                            source=QuestionSource.SCREENING),
    ])
pkg = ApplicationPackage(
    vacancy_id='hh:136591579', vacancy_stable_id='hh:136591579',
    resume_adaptation_needed=False, resume_summary='s',
    tailored_skills=['python'], relevant_experience=['e'],
    cover_letter='Hello ' + ' '.join(['word'] * 130), application_strategy='st',
    warnings=[], generator_version='v1',
    adaptation=ResumeAdaptation(target_title='t', professional_summary='p',
                                prioritized_skills=['python'],
                                relevant_experience_points=['e']))
pkg.form = form
pkg.answers = [
    ApplicationAnswer(question_id='hh__ctrl_task_384589151', answer='Claude Code; Cursor',
                      answer_type=QuestionType.CHECKBOX, confidence=1.0,
                      requires_review=False, reason='confirmed from resume'),
]
pkg.validation_status = 'VALID'

# ---- live snapshot (from the open form) ----
targets = _list_targets(CDP)
tab = next(t for t in targets if t.get('type') == 'page' and URL_SUBSTR in (t.get('url') or ''))
live_snapshot = _inspect_target_via_cdp(tab)

# ---- read-only baseline: current checked state of the target group ----
def read_group_state():
    import asyncio
    import websockets
    async def _go():
        async with websockets.connect(tab['webSocketDebuggerUrl'], open_timeout=15, close_timeout=15) as ws:
            expr = ('JSON.stringify(Array.from(document.querySelectorAll("input[type=checkbox]"))'
                    '.map(e => ({name: e.name, label: (e.labels&&e.labels[0])?(e.labels[0].innerText||"").trim():null, checked: e.checked})))')
            await asyncio.wait_for(ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                'params': {'expression': expr, 'returnByValue': True}})), 15)
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if msg.get('id') == 1:
                    return json.loads(msg['result']['result']['value'])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(_go())

baseline = read_group_state()
print('=== BASELINE (read-only) ===')
for b in baseline:
    if b.get('label') in ('Claude Code', 'Cursor', 'Свой вариант'):
        print('  ', b['name'], '=', b['label'], 'checked:', b['checked'])

# ---- execute ----
evaluate_fn = make_cdp_evaluate(CDP, URL_SUBSTR)
rep = prepare_and_execute_prefill(
    pkg, form, live_snapshot, evaluate_fn,
    allowed_url_markers=['hh.ru'], required_url_markers=['applicant/vacancy_response'])

print()
print('=== ORCHESTRATION REPORT ===')
print('verdict:', rep.verdict, '| stop:', rep.stop_reason)
print('url:', (rep.url_before or '')[:70], '->', (rep.url_after or '')[:70])
print('planned:', rep.planned_operations, 'executed:', rep.executed_operations,
      'verified:', rep.verified_operations, 'skipped:', rep.skipped_operations,
      'failed:', rep.failed_operations)
print('instrumentation: nav=%d click=%d submit=%d fill=%d upload=%d' % (
    rep.navigation_count, rep.click_count, rep.submit_count, rep.fill_count, rep.upload_count))
for t in rep.operations:
    print('  ', t.question_id, '=', t.value, '->', t.status.value, '|', t.reason[:50])
for g in rep.group_checks:
    print('  group', g.group_name, 'expected:', g.expected_checked, 'actual:', g.actual_checked, 'ok:', g.ok)

# ---- read-only after-snapshot ----
after = read_group_state()
print()
print('=== AFTER (read-only) ===')
for b in after:
    if b.get('label') in ('Claude Code', 'Cursor', 'Свой вариант'):
        print('  ', b['name'], '=', b['label'], 'checked:', b['checked'])

# ---- acceptance ----
checked_after = {b['label']: b['checked'] for b in after if b.get('label') in ('Claude Code', 'Cursor', 'Свой вариант')}
ok = (rep.verdict == 'VERIFIED'
      and checked_after.get('Claude Code') is True
      and checked_after.get('Cursor') is True
      and checked_after.get('Свой вариант') is False
      and rep.navigation_count == 0 and rep.submit_count == 0 and rep.upload_count == 0
      and rep.url_before == rep.url_after)
print()
print('VERDICT:', 'ACCEPTED' if ok else 'CHECK MANUALLY')