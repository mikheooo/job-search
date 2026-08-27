"""Stage 20F: real HH execution - ONE validated answer (Claude Code checkbox).

Safety:
- Only the single validated checkbox operation from the real snapshot.
- URL guard: hh.ru + applicant/vacancy_response (fail closed).
- Direct DOM property mutation (el.checked), NO click, NO submit.
- Read-only verification after mutation.
"""
import json, sys, io, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'C:\Users\Misha\Documents\job-search')

from ai_assistant.prefill_plan import PrefillPlan, PrefillOperation, PrefillTarget
from ai_assistant.prefill_execute import execute_prefill_plan, make_cdp_evaluate

CDP = 'http://127.0.0.1:9223'
URL_SUBSTR = 'applicant/vacancy_response'

# 1. Build plan: ONLY the validated Claude Code checkbox
op = PrefillOperation(
    question_id='hh__ctrl_task_384589151',
    target=PrefillTarget(tag='INPUT', type='checkbox', name='task_384589151', label='Claude Code'),
    value='Claude Code', source_answer='Claude Code', confidence=1.0,
    reason='Validated CHECKBOX answer (truth source: resume mentions Claude Code)')
plan = PrefillPlan(vacancy_stable_id='hh:136591579', status='NEEDS_REVIEW', operations=[op])

# 2. Bind evaluate_fn to the already-open tab
evaluate_fn = make_cdp_evaluate(CDP, URL_SUBSTR)

# 3. Execute with URL guard
rep = execute_prefill_plan(
    plan, evaluate_fn,
    allowed_url_markers=['hh.ru'],
    required_url_markers=['applicant/vacancy_response'])

print('=== EXECUTION REPORT ===')
print('verdict:', rep.verdict)
print('url_before:', (rep.url_before or '')[:90])
print('url_after: ', (rep.url_after or '')[:90])
print('mutations:')
for m in rep.mutations:
    print('  ', json.dumps(m.model_dump(), ensure_ascii=False))
print('verification:')
for v in rep.verification:
    print('  ', json.dumps(v, ensure_ascii=False))
print('instrumentation:')
print('  navigation_count:', rep.navigation_count)
print('  click_count:', rep.click_count)
print('  submit_count:', rep.submit_count)
print('  fill_count:', rep.fill_count)
print('  upload_count:', rep.upload_count)
print('  successful_mutations:', rep.successful_mutations)
print('  failed_mutations:', rep.failed_mutations)
print('errors:', rep.errors)

# 4. Independent read-only verification via capture tool
print()
print('=== INDEPENDENT READ-ONLY VERIFICATION ===')
from tools.capture_manual_form import _list_targets, _inspect_target_via_cdp
targets = _list_targets(CDP)
tab = next(t for t in targets if t.get('type') == 'page' and URL_SUBSTR in (t.get('url') or ''))
snap = _inspect_target_via_cdp(tab)
cc = [c for c in snap['controls'] if c.get('label') == 'Claude Code']
if cc:
    print('Claude Code checkbox in fresh DOM: checked =', cc[0].get('checked', 'N/A (attr not captured)'))
# count checked boxes in fresh DOM
checked_count = snap['extraction_meta'].get('visible_controls')
print('visible controls:', checked_count)
# direct read of all checkbox states
import asyncio
from tools.capture_manual_form import _INSPECTION_JS

async def _read_checked():
    import websockets
    async with websockets.connect(tab['webSocketDebuggerUrl'], open_timeout=15, close_timeout=15) as ws:
        expr = 'JSON.stringify(Array.from(document.querySelectorAll("input[type=checkbox]")).map(e => ({name: e.name, label: (e.labels&&e.labels[0])?(e.labels[0].innerText||"").trim():null, checked: e.checked})))'
        await asyncio.wait_for(ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
            'params': {'expression': expr, 'returnByValue': True}})), 15)
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get('id') == 1:
                return json.loads(msg['result']['result']['value'])

checked_boxes = asyncio.get_event_loop().run_until_complete(_read_checked()) if False else None
# use sync runner
def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

all_boxes = _run(_read_checked())
checked = [b for b in all_boxes if b.get('checked')]
print('checkboxes total:', len(all_boxes), '| checked:', len(checked))
for b in checked:
    print('  CHECKED:', b['name'], '=', b.get('label'))

print()
print('VERDICT:', 'PASS' if (rep.verdict == 'VERIFIED' and len(checked) == 1 and checked[0].get('label') == 'Claude Code') else 'CHECK MANUALLY')
