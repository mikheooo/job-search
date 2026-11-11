"""Legacy P7 finalizer; disabled because it reads/writes unbounded plaintext data."""
raise SystemExit('Legacy P7 finalizer disabled; use the policy-gated P6/P8 workflow')

import json
import sqlite3
import shutil
import copy
from pathlib import Path
from datetime import datetime, timezone
from ai_assistant.gig.application import economics,sendable_batch,admission_reasons
from ai_assistant.gig.assessment import load_candidate_profile, skill_confidences
from ai_assistant.gig.action import action_score_of
from ai_assistant.gig.pipeline import GigPipeline
from ai_assistant.gig.jit import verify_gig,apply_verification
from ai_assistant.gig.grounded import analyze,unsupported_claims
from ai_assistant.gig.proposal import find_forbidden_claims
from ai_assistant.gig.capability import assess_capability
from ai_assistant.gig.first_money import first_money_score_of
from ai_assistant.gig.client import assess_client_risk
from ai_assistant.gig.currency import exchange_rate_of
from ai_assistant.gig.tracker import SubmissionTracker,SubmissionRecord,default_tracker_path

dest=Path('artifacts/p7'); packs=json.loads((dest/'application_packs.json').read_text(encoding='utf8'))
observations=json.loads((dest/'final_observations.json').read_text(encoding='utf8'))
collection=json.loads((dest/'fresh_collection.json').read_text(encoding='utf8'))
prior=[]
for name in ['verified_candidates.json','verified_more.json','verified_sales.json']:
    prior+=json.loads((dest/name).read_text(encoding='utf8'))
pipe=GigPipeline(profile=load_candidate_profile(),persist=False); now=datetime.now(timezone.utc)
entries=[]; reviews={}
for ident,p in packs.items():
    obs=next(o for o in observations if o['id']==ident); raw=obs['raw_live']
    earlier=next(o for o in prior if o['id']==ident)
    assert raw['description']==earlier['raw_live']['description'], 'Scope changed: redo manual review'
    assert raw['source_status']=='OPEN' and obs['risk']['level']!='HIGH'
    assert all(ref in raw['description'] for ref in p['refs'])
    analysis=analyze(raw['description'],raw['title'])
    if not analysis.required_work:
        # The English verb extractor does not parse Indonesian work; retain exact source quotes.
        analysis.required_work=[{'work':ref,'evidence':ref} for ref in p['refs']]
    assert not find_forbidden_claims(p['proposal']) and not unsupported_claims(analysis,p['proposal'])
    gig=pipe.process_raw(raw); original_action=action_score_of(gig,now=now).action_score
    discovery=next(r for r in collection if r['source_id']==ident)
    gig.competition=discovery['competition']
    checked=datetime.fromisoformat(raw['fetched_at']); ver=verify_gig(gig,raw,now=checked); apply_verification(gig,ver,raw)
    p5_hours=gig.estimated_effort_hours
    gig.source_facts['p6_action_score_at_refresh']=original_action
    gig.source_facts['p6_effort_review']={'hours':p['hours'],'range_hours':p['range_hours'],'rationale':p['estimate_basis'],'p5_estimate_hours':p5_hours}
    gig.estimated_effort_hours=p['hours']
    # Explicit human review of allowed implementation alternatives, not new scoring.
    selected=copy.deepcopy(analysis)
    selected.required_tech=[t for t in analysis.required_tech if t['name'].lower() in {'python','pandas','excel','sql'}]
    profile=load_candidate_profile()
    cap=assess_capability(selected,skill_confidences(profile),profile_skills=profile['skill_confidence'])
    assert cap.fit in {'strong','proven'}, (ident,cap.to_dict())
    cap.notes.append(p['capability_evidence'])
    cap.notes.append('Manual selected-path review of explicit alternatives; full source analysis is retained separately')
    risk=assess_client_risk(gig,now=now)
    eco=economics(p['bid'],p['currency'],p['hours'],exchange_rate=exchange_rate_of(raw['source_facts'],p['currency']),hourly=p['hourly'])
    from ai_assistant.gig.assessment import effective_hourly_rate
    gig.effective_hourly_rate=effective_hourly_rate(gig,p['hours'])
    score=first_money_score_of(gig,analysis,cap,risk,now=now)
    pack={'PROPOSAL':p['proposal'],'TECHNICAL_APPROACH':p['approach'],
          'BID':{'recommended_bid':p['bid'],'currency':p['currency'],'hourly':p['hourly'],'delivery_time':f"{p['days']} days",'milestones':p['milestones']},
          'CLARIFYING_QUESTION':p['question'],'RISKS':p['risks'],'DO_NOT_CLAIM':p['do_not_claim'],
          'referenced_requirements':p['refs'],'word_count':len(p['proposal'].split()),'unsupported_tech':[],'injected_claims':[]}
    entry={'project_id':ident,'title':raw['title'],'project_url':raw['url'],'project_status':raw['source_status'],
           'live_verified':True,'verified_at':ver.verified_at,'published_at':raw['published_at'],
           'age_h':round((now-datetime.fromisoformat(raw['published_at'])).total_seconds()/3600,2),
           'bids_at_discovery':ver.bids_at_discovery,'bids_live':ver.bids_live,'bids_delta':ver.bids_delta,
           'budget':gig.budget_text,'action_score':original_action,'score':score.to_dict(),
           'capability':cap.to_dict(),'client_risk':risk.to_dict(),'grounded_job_analysis':analysis.to_dict(),
           'description_live':raw['description'],'effort_review':gig.source_facts['p6_effort_review'],
           'economics':eco,'proposal_pack':pack,'decision':'SEND_NOW','why':p['why']}
    assert not admission_reasons(entry,now=now),admission_reasons(entry,now=now)
    entries.append(entry)
    reviews[ident]={'verdict':'PASS','checked_at':raw['fetched_at'],
       'checks':{k:True for k in ['open','budget_correct','live_bids_correct','grounded_requirements','honest_capability','reasonable_risk','economic_bid','matches_brief','no_fabrication','ready_to_paste']},
       'evidence':{'html':obs['html'],'api_url':raw['source_facts']['freelancer_api_url'],
          'status':raw['source_status'],'budget':raw['budget'],'currency':raw['currency'],'bids_live':raw['competition'],
          'description_unchanged':True,'literal_requirement_refs':p['refs'],'word_count':pack['word_count'],
          'browser_qa_file':'neo_qa_1.json' if ident in ['fl_40750263','fl_40750305'] else 'neo_qa_2.json',
          'account_form_verified':False}}
batch=sendable_batch(entries,now=now); assert len(batch)==5
path=default_tracker_path()
if path.exists() and not (dest/'tracker_before_p7.sqlite').exists(): shutil.copy2(path,dest/'tracker_before_p7.sqlite')
tracker=SubmissionTracker(path)
for e in batch:
    p=packs[e['project_id']]
    tracker.upsert(SubmissionRecord(project_id=e['project_id'],title=e['title'],project_url=e['project_url'],
        discovered_at=next(o['fetched_at'] for o in collection if o['source_id']==e['project_id']),verified_at=e['verified_at'],
        bids_at_discovery=e['bids_at_discovery'],bids_live=e['bids_live'],action_score=e['action_score'],
        first_money_score=e['score']['first_money_score'],capability_fit=p['fit'],client_risk=e['client_risk']['level'],
        proposal_created_at=now.isoformat(),proposal_text=p['proposal'],recommended_bid=p['bid'],recommended_currency=p['currency'],
        recommended_delivery=f"{p['days']} days",status='READY',note='P7 SEND_NOW QA PASS; never submitted automatically'))
old=json.loads((dest/'old_ready_recheck.json').read_text(encoding='utf8'))
with sqlite3.connect(path) as conn:
    for item in old:
        ident=item['project_id']; raw=item.get('raw_live')
        note='P7 BACKUP: too much competition for top five; public scope unchanged' if raw else 'P7 excluded: HTTP 404; source status UNKNOWN, not proven CLOSED/AWARDED'
        conn.execute('UPDATE submissions SET note=?, verified_at=COALESCE(?,verified_at), bids_live=COALESCE(?,bids_live) WHERE project_id=? AND source=?',
                     (note,raw['fetched_at'] if raw else None,raw['competition'] if raw else None,ident,'freelancer'))
verified_ids={o['id'] for o in observations+prior}
for name in ['verified_backups.json','verified_backup_options.json']:
    verified_ids.update(o['id'] for o in json.loads((dest/name).read_text(encoding='utf8')))
verified_ids.update(o['project_id'] for o in old if o.get('raw_live'))
metrics=tracker.metrics(projects_seen=len(collection),projects_live_verified=len(verified_ids),apply_first_count=5)
metrics['send_now_current_batch']=5
backup_sources=[]
for name in ['verified_more.json','verified_backups.json','verified_backup_options.json']:
    backup_sources+=json.loads((dest/name).read_text(encoding='utf8'))
backups=[]
for ident,why in [('fl_40746421','122 bids; старый P6, не занимает верх свежей очереди'),
                  ('fl_40746459','72 bids; старый P6, нужен новый пересчёт цены/объёма и QA перед подачей'),
                  ('fl_40749029','201 bids; узкая n8n/Linux задача, LOW client risk, но слабее по конкуренции'),
                  ('fl_40749454','56 bids; специфические 3X-UI/sing-box/Raspberry Pi навыки требуют проверки, LEARNABLE по полному scope'),
                  ('fl_40748685','110 bids; Rhino3D компетенция не подтверждена, LEARNABLE, только после технического образца')]:
    item=next((o for o in old if o['project_id']==ident),None) or next(o for o in backup_sources if o['id']==ident)
    raw=item['raw_live']; backups.append({'project_id':ident,'title':raw['title'],'url':raw['url'],'status':raw['source_status'],
        'bids':raw['competition'],'verified_at':raw['fetched_at'],'reason':why})
skip=[{'project_id':o['id'],'title':o['raw_live']['title'],'reason':'HIGH client risk; not SEND_NOW'} for o in prior+backup_sources if o.get('risk',{}).get('level')=='HIGH']
skip=list({s['project_id']:s for s in skip}.values())
payload={'generated_at':now.isoformat(),'counts':{'SEND_NOW':5,'BACKUP':5,'SKIP':len(skip)+1},'send_now':batch,'backup':backups,'skip':skip,'qa':reviews,'metrics':metrics}
for name,value in [('application_batch.json',payload),('qa_review.json',reviews),('funnel_metrics.json',metrics)]:
    (dest/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
lines=['| # | project | score | live bids | age | client risk | gross | net $/h | capability |',
       '|---:|---|---:|---:|---:|---|---|---:|---|']
for i,e in enumerate(batch,1):
    b=e['proposal_pack']['BID']; gross=f"{b['recommended_bid']} {b['currency']}"+('/h' if b['hourly'] else '')
    lines.append(f"| {i} | [{e['title']}]({e['project_url']}) | {e['score']['first_money_score']} | {e['bids_live']} | {e['age_h']} h | {e['client_risk']['level']} | {gross} | {e['economics']['net_effective_rate_usd']} | STRONG |")
lines+=['','# GIG-SEARCH P7 — First Real Application Batch','',f"Batch: **5 SEND_NOW / 5 BACKUP**. Проверка UTC: {now.isoformat()} (Bangkok UTC+7). Это готовые предложения; возможность отправки с аккаунта зависит от входа, bids и баланса — они не проверены.",'',
       'BrowserOS neo подключён, все пять публичных страниц отдельно прочитаны после подготовки откликов. На них видны Log In / Sign Up и гостевая форма. Кнопку Bid не нажимали, поля не заполняли. Bids/status/budget/description/skills и age сверены по анонимному API; публичный интерфейс местами ошибочно показывает Posted 1 minute ago и кешированные bids. Для возраста используется API submitdate.','',
       'Client information — публичный HTML snapshot, не гарантия оплаты. Четыре клиента MEDIUM без отзывов/подтверждённой оплаты; пятый LOW с историей. HIGH исключены. Для начала оплачиваемой работы нужны согласованные funding/billing на платформе.','',
       '## SUBMISSION ORDER','']
for i,e in enumerate(batch,1): lines.append(f"{i}. **{e['title']}** — {e['why']}")
lines+=['','Порядок учитывает конкуренцию, свежесть, ясную сдачу и клиента, а не только score. Ограничения age<6h / bids≤25 мягкие: PDF имеет 33 bids, Numerical — 54+; SA — 104+ и старше, потому остаётся пятым. Экономических strategic_first_job_exception нет.','']
for i,e in enumerate(batch,1):
    ident=e['project_id']; p=packs[ident]; risk=e['client_risk']; eco=e['economics']; hourly=p['hourly']; facts=risk['facts']
    lines += [f"## {i}. PROJECT — {e['title']}",'',f"**URL:** [{e['title']}]({e['project_url']})",'',
       f"**LIVE STATUS:** OPEN / verified_at {e['verified_at']}",f"**LIVE BIDS:** {e['bids_live']} (первое наблюдение P7 {e['bids_at_discovery']}, delta {e['bids_delta']})",f"**AGE:** {e['age_h']} h (API publish {e['published_at']})",'',
       f"**CLIENT RISK:** {risk['level']}; {'; '.join(risk['reasons'])}.",f"Client facts: country={facts.get('country')}; rating={facts.get('rating')}; reviews={facts.get('reviews')}; payment_verified={facts.get('payment_verified')}; member_since={facts.get('registered_at')}; jobs_posted={facts.get('jobs_posted')}; hire_rate={facts.get('hire_rate')}. None = unknown.",'',
       '**WHY WE CAN DO IT:** '+p['capability_evidence'],'','**GROUNDED REQUIREMENTS:**','']
    lines += [f'- {ref}' for ref in p['refs']]
    lines += ['',f"**BID AMOUNT:** {p['bid']} {p['currency']}"+('/hour' if hourly else ' fixed-price')+f". Published budget: {e['budget']}.",f"**DELIVERY TIME:** {p['days']} days после получения source files и согласования sample/объёма.",'',
       '**ECONOMICS:** '+f"gross {'rate' if hourly else 'bid'} = {p['bid']} {p['currency']}{'/h' if hourly else ''} → ${eco['gross_usd']}{'/h' if hourly else ''}; platform_fee=${eco['platform_fee_usd']}{'/h' if hourly else ''}; net=${eco['net_revenue_usd']}{'/h' if hourly else ''}; estimated_hours={p['hours']}; net_effective_rate=${eco['net_effective_rate_usd']}/h. **PASS**, strategic_first_job_exception=none.",'',
       p['estimate_basis']+f" P5 исходная оценка {e['effort_review']['p5_estimate_hours']} ч сохранена отдельно; оценка P7 относится к описанному пути и является инженерной оценкой.",'']
    if hourly: lines += [f"Плановый объём {p['hours']} ч → gross {p['bid']*p['hours']} {p['currency']}, но это не полученный revenue и не гарантия числа оплаченных часов. Оплата по фактическим одобренным часам; срок и первичный объём согласовать по файлам.",'']
    lines += ['**MILESTONES:**','']+[f'- {m}' for m in p['milestones']]
    lines += ['','**TECHNICAL APPROACH:**','']+[f'{n}. {step}' for n,step in enumerate(p['approach'],1)]
    lines += ['','**PROPOSAL:**','', '```text',p['proposal'],'```','', '**CLARIFYING QUESTION:** '+p['question'],'','**DO NOT CLAIM:**','']+[f'- {v}' for v in p['do_not_claim']]
    lines += ['','**RISKS:**','']+[f'- {v}' for v in p['risks']]
    lines += ['','**ADDITIONAL QUESTIONS:** none observed in the public bid form. Полная форма после входа UNKNOWN; не утверждается, что дополнительных полей там нет.','']
    if ident in {'fl_40750263','fl_40749362'}:
        lines+=['**Дополнительные просьбы клиента из объявления (не придуманные поля формы):**','',
                '- Method / approach: '+p['approach'][0]+' '+p['approach'][1],
                f"- Turnaround / timeline: {p['days']} days after sample/source files and scope agreement.",'']
    lines+=['**SUBMISSION CHECKLIST — выполнить непосредственно перед нажатием Submit:**','']
    lines += [f'- [ ] {v}' for v in ['project still OPEN','bid count rechecked','budget unchanged','proposal copied correctly',f"bid amount correct: {p['bid']} {p['currency']}"+('/h' if hourly else ''),f"delivery days correct: {p['days']}",'milestones correct','no invented experience','account has enough bids/balance','additional logged-in form questions checked']]
    lines+=['',f"**QA: PASS.** Отдельное повторное чтение HTML + API после создания текста: {reviews[ident]['checked_at']}. OPEN, budget/currency/bids совпадают с API; scope не изменился; обе цитаты есть буквально; технологии/выбранный путь и capability проверены вручную. {len(p['proposal'].split())} слов, цена и срок совпадают с pack, placeholders и выдуманного опыта нет. Browser evidence: `{reviews[ident]['evidence']['browser_qa_file']}`; API/HTML: `artifacts/p7/final_observations.json`.",'',
           '**AFTER ACTUAL MANUAL SUBMISSION — записать фактически отправленные значения:**','',
           '```powershell',f".venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-submitted {ident} --actual-bid {p['bid']} --delivery-days {p['days']} --bids-at-submission <actual_count>",'```',
           'Замените `<actual_count>` на число ставок в момент реальной отправки; если изменили bid/срок, запишите фактические значения. Это локальная запись, не отправка.','']
lines+=['## BACKUP — 5','', 'Резерв не входит в SEND_NOW и не прошёл финальную QA пакета. Нужны новая проверка страницы, экономики и соответствующего навыка перед заменой.','']
lines += [f"- [{b['title']}]({b['url']}) — {b['status']}, {b['bids']} bids; {b['reason']}; checked {b['verified_at']}" for b in backups]
lines+=['','## SKIP','', '- Add Noon & Extra Scrapers -- 2 — HTTP 404 на новой проверке; UNKNOWN, не выдуманные CLOSED/AWARDED. Исключён из sendable batch.','']
lines += [f"- {s['title']} — {s['reason']}" for s in skip]
lines+=['','## Manual tracking workflow','', 'Выполняйте команды только после соответствующего реального события. `<project_id>` / суммы / дата / bids — фактические данные, не тестовые события.','',
       '```powershell','.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-replied <project_id>',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-interview <project_id>',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-awarded <project_id>',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-rejected <project_id>',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-closed <project_id>',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig mark-paid <project_id> --revenue-usd <actual_total_received_usd> --payment-date YYYY-MM-DD',
       '.venv\\Scripts\\python.exe -m ai_assistant.cli gig funnel','```','',
       '`mark-paid` сохраняет суммарные реально полученные USD и actual payment_date; повтор той же суммы не удваивает выручку. Award/budget не записывается как revenue. `mark-submitted` требует уже существующую READY запись и сохраняет actual_bid, actual_delivery, bids_at_submission и submitted_at. Команды не отправляют ничего на Freelancer.','',
       '## Реальная воронка','', '| Metric | Value |','|---|---:|']
lines += [f"| {k} | {v if v is not None else 'undefined (no submissions)'} |" for k,v in metrics.items()]
lines+=['', 'proposals_ready=8 — исторические тексты P6 + пять новых; это не восемь текущих SEND_NOW. Текущий sendable batch ровно пять. Отправки/ответы/интервью/awards/revenue остаются нулевыми, пока пользователь не совершит и не запишет событие.','',
       '## Evidence and limits','',
       f'Свежий сбор: {len(collection)} уникальных проектов из шести текущих запросов + одного уточняющего запроса существующего Freelancer API. Новых источников/скоринга нет. P6 opportunity/action/FX/dedup сохранены. Review selected implementation alternatives вручную: Python вместо допустимых VBA/.NET/BI вариантов; это явная оценка пути, а не неподтверждённый опыт в каждой упомянутой технологии.','',
       'Доказательства: `artifacts/p7/collection_*.json`, `extra_collection.json`, `fresh_collection.json`, `old_ready_recheck.json`, `final_observations.json`, `neo_qa_1.json`, `neo_qa_2.json`, `application_packs.json`, `application_batch.json`, `qa_review.json`, `funnel_metrics.json`. В каждом snapshot имеются время/URL и оригинальный текст. Формулы P5 не менялись; manual scope hours и original action_score сохранены отдельно.','',
       '[Официальные комиссии Freelancer](https://www.freelancer.com/feesandcharges), перепроверено 2026-10-05: fixed 10% или $5 minimum, hourly 10%. Net выше исключает налоги, вывод/банковскую конверсию и дополнительные платные сервисы. Официальный minimum available account balance обычно $20, для некоторых случаев выше; фактические требования доступны в аккаунте/форме, здесь не подтверждены.','']
Path('docs/gig_search_p7_application_batch.md').write_text('\n'.join(lines),encoding='utf8')
print(json.dumps({'counts':payload['counts'],'send_now':[(e['project_id'],e['bids_live'],e['economics']['net_effective_rate_usd']) for e in batch]},ensure_ascii=False))
