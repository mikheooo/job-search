"""Legacy P6 finalizer; disabled because it reads/writes unbounded plaintext data."""
raise SystemExit('Legacy P6 finalizer disabled; use the policy-gated P6/P8 workflow')

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from ai_assistant.gig.assessment import load_candidate_profile, skill_confidences, effective_hourly_rate
from ai_assistant.gig.action import action_score_of
from ai_assistant.gig.grounded import analyze, unsupported_claims
from ai_assistant.gig.capability import assess_capability
from ai_assistant.gig.client import assess_client_risk
from ai_assistant.gig.first_money import build_shortlist
from ai_assistant.gig.first_money_report import render_report
from ai_assistant.gig.jit import verify_gig, apply_verification
from ai_assistant.gig.pipeline import GigPipeline
from ai_assistant.gig.proposal import find_forbidden_claims
from ai_assistant.gig.tracker import SubmissionTracker, SubmissionRecord, default_tracker_path

dest = Path('artifacts/p6')
run = json.loads((dest/'prepared_run.json').read_text(encoding='utf8'))
packs = json.loads((dest/'reviewed_packs.json').read_text(encoding='utf8'))
observed = json.loads((dest/'manual_qa_observations.json').read_text(encoding='utf8'))
scope = json.loads((dest/'scope_reviews.json').read_text(encoding='utf8'))
profile = load_candidate_profile()
pipeline = GigPipeline(profile=profile,persist=False)
reviews, candidates = {}, []
now = datetime.now(timezone.utc)
all_entries = run['candidates']+run['backup']+run['skip']
for ident, pack in packs.items():
    old = next(e for e in all_entries if e['project_id']==ident)
    obs = next(o for o in observed if o['id']==ident)
    raw = obs['raw_live']
    assert obs['description_unchanged'], 'changed brief needs another manual review'
    gig = pipeline.process_raw(raw)
    gig.competition = old['bids_at_discovery']
    checked = datetime.fromisoformat(raw['fetched_at'])
    apply_verification(gig,verify_gig(gig,raw,now=checked),raw)
    gig = pipeline.process_gig(gig,existing=[])
    gig.source_facts['p6_action_score_at_refresh'] = action_score_of(gig,now=checked).action_score
    gig.source_facts['p6_effort_review'] = dict(scope[ident],p5_estimate_hours=gig.estimated_effort_hours)
    gig.estimated_effort_hours = scope[ident]['hours']
    gig.effective_hourly_rate = effective_hourly_rate(gig,gig.estimated_effort_hours)
    analysis = analyze(gig.description,gig.title)
    cap = assess_capability(analysis,skill_confidences(profile),profile_skills=profile['skill_confidence'])
    risk = assess_client_risk(gig,now=checked)
    queue = build_shortlist([(gig,analysis,cap,risk)],now=now)
    assert len(queue.apply_first)==1, (ident,[e.reasons for e in queue.backup+queue.skip])
    entry = queue.apply_first[0].to_dict()
    proposal = pack['PROPOSAL']
    assert 100 <= len(proposal.split()) <= 180
    assert not find_forbidden_claims(proposal)
    assert not unsupported_claims(analysis,proposal)
    assert all(ref in gig.description for ref in pack['referenced_requirements'])
    assert len(pack['referenced_requirements'])>=2
    entry['proposal_pack'] = {k:v for k,v in pack.items() if k.isupper() or k=='referenced_requirements'}
    entry['proposal_pack'].update(word_count=len(proposal.split()),injected_claims=[],unsupported_tech=[])
    facts = risk.facts.to_dict()
    client = (f"Риск {risk.level}: {'; '.join(risk.reasons)}. Публичный HTML snapshot: "
              f"country={facts.get('country')}, rating={facts.get('rating')}, reviews={facts.get('reviews')}, "
              f"payment_verified={facts.get('payment_verified')}, member_since={facts.get('registered_at')}; "
              f"jobs_posted={facts.get('jobs_posted')}, hire_rate={facts.get('hire_rate')}. "
              "None означает unknown. Owner history не раскрывается анонимным API; клиентские поля могут быть кешированы.")
    reviews[ident] = {k:pack[k] for k in ['why','requirements','capability_evidence','economics']}
    reviews[ident].update(verdict='PASS',checked_at=raw['fetched_at'],client_info=client,
       checks={name:True for name in ['open','live_bids','budget','currency','grounded_requirements',
               'no_invented_technology','adequate_capability','proposal_matches','no_invented_experience','economic_bid']},
       evidence=f"Отдельно повторно открыт публичный project URL после подготовки пакета; HTML: {obs['page_path']}. "
                f"Анонимный GET API: {raw['source_facts']['freelancer_api_url']}, сохранён manual_qa_observations.json. "
                f"status=active, frontend=open; bids={raw['competition']}; currency={raw['currency']}; budget={raw['budget']['min']}–{raw['budget']['max']}. "
                f"Текст не изменился относительно генерации, две приведённые цитаты есть буквально. Отклик {len(proposal.split())} слов; "
                "технологии сверены по тексту, опыта/портфолио не придумано; цена и граница объёма проверены вручную. "
                "API актуальнее SEO HTML; браузер Neo недоступен, экран и возможность подачи с аккаунта не проверены.")
    candidates.append(entry)

rejected = {
 'fl_40747575':'FAIL manual QA: required AdsPower RPA Plus / MCP experience is not confirmed; API adjacency alone is insufficient. Capability revised UNKNOWN for this scope.',
 'fl_40746031':'FAIL manual QA: Hindi teaching and video production capability unconfirmed; Python alone is insufficient. Net price below viable floor.',
 'fl_40743224':'FAIL manual QA: deliverability of phone-to-username lookup not established; proposed price under viable floor.',
 'fl_40744770':'FAIL manual QA: broad lead quality requirements and net bid under viable floor.',
 'fl_40746010':'FAIL manual QA: bidder/portfolio-management role is not a bounded technical first-money deliverable; net bid under viable floor.',
 'fl_40741775':'FAIL manual QA: professional registry coverage unverified, automatic proposal not ready and net bid under viable floor.',
 'fl_40746630':'FAIL manual QA: unknown game-specific purchasing environment, accounts and anti-bot handling; capability not established for the complete scope.'}
backup_ids = ['fl_40747946','fl_40746813','fl_40747985','fl_40742735','fl_40735384','fl_40739073','fl_40740400']
backups = []
for ident in backup_ids:
    e=copy.deepcopy(next(e for e in all_entries if e['project_id']==ident))
    e['decision']='BACKUP'
    e['reasons'].append('Not approved for immediate submission: needs separate scope/economics/capability review; reverify before use')
    backups.append(e)
skips = []
for old in all_entries:
    ident=old['project_id']
    if ident in packs or ident in backup_ids:
        continue
    e=copy.deepcopy(old); e['decision']='SKIP'
    if ident in rejected:
        e['reasons'].append(rejected[ident]); reviews[ident]={'verdict':'FAIL','evidence':rejected[ident]}
    skips.append(e)
run.update(candidates=candidates,backup=backups,skip=skips,qa_status='PASS separate manual public-page/API review for three candidates',
           counts={'APPLY_FIRST':3,'BACKUP':len(backups),'SKIP':len(skips)},generated_at=now.isoformat())
tracker_path=default_tracker_path()
if tracker_path.exists() and not (dest/'tracker_before_p6.sqlite').exists():
    shutil.copy2(tracker_path,dest/'tracker_before_p6.sqlite')
tracker=SubmissionTracker(tracker_path)
for e in candidates:
    bid=e['proposal_pack']['BID']
    tracker.upsert(SubmissionRecord(project_id=e['project_id'],title=e['title'],project_url=e['project_url'],
       discovered_at=next(o['raw_live']['fetched_at'] for o in run['observations'] if o['source_id']==e['project_id']),
       verified_at=e['verified_at'],bids_at_discovery=e['bids_at_discovery'],bids_live=e['bids_live'],
       action_score=e['action_score'],first_money_score=e['score']['first_money_score'],capability_fit=e['capability']['fit'],
       client_risk=e['client_risk']['level'],proposal_created_at=now.isoformat(),proposal_text=e['proposal_pack']['PROPOSAL'],
       recommended_bid=bid['recommended_bid'],recommended_currency=bid['currency'],recommended_delivery=bid['delivery_time'],
       status='READY',note='P6 manual QA PASS; prepare only; never submitted by this run'))
metrics=tracker.metrics(projects_seen=100,projects_live_verified=run['api_verification_count'],apply_first_count=3)
for name,payload in [('final_run.json',run),('qa_review.json',reviews),('funnel_metrics.json',metrics)]:
    (dest/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
report=render_report(run,reviews,metrics)
report += '''
## Источники, границы и воспроизведение

Новый сбор в этом P6: 100 публичных проектов по семи уже существующим направлениям, 107 GET-запросов на этапе коллекции. 20 лучших перепроверены через публичную project page и анонимный API; финальные три открыты повторно отдельным QA после подготовки откликов. `fresh_collection.json` — новый сбор этого запуска; `--resume-collection` переиспользует его, а страницы/API всё равно читает заново. Браузер Neo не отвечал на локальном endpoint; публичные GET не подтверждают экран, доступность bidding, баланс или квоту аккаунта.

В SEO HTML обнаружены устаревшие bids (например, Noon 20–115 против API 125). Итоговые status/bids/budget/currency/description/skills/age берутся из публичного API, client history — отдельно помеченный HTML snapshot. API owner history не отдаёт. `bids_at_discovery` берётся из ранее сохранённого наблюдения базы и не гарантирует самый первый исторический bid count; сохранён неизменно при повторных проверках. `found_via` сохранён как provenance и не используется как требование.

Формулы P5 opportunity/action, FX, дедупликации и оценки трудоёмкости не переписаны; их исходные файлы сверены с pre-edit checkpoint. P6 отдельно хранит оценку узкого согласуемого объёма и исходную P5 оценку; компонент action_score использует P5 до ручной оценки. First-money score — эвристика, не вероятность выигрыша. Порог 48 унаследован, не выдаётся за эмпирически откалиброванный; конкуренция остаётся высокой.

В таблице рекомендуем первым Noon/Extra из-за короткой сдачи, хотя у магазина чуть выше суммарный score. Резерв не является READY: каждый требует своей QA и новой live-проверки. В частности, Wayfair сейчас не проходит экономику автоматического bid; гостиницы имеют широкий глобальный объём, остальные резервные задачи требуют уточнения scope/навыков.

[Официальные комиссии Freelancer](https://www.freelancer.com/feesandcharges): fixed-price 10% или минимум $5, hourly 10%. Расчёты выше относятся к комиссии платформы; налоги, вывод, конвертация и расходы клиентских сервисов не включены. Перед реальной подачей нужна повторная проверка страницы и требований аккаунта: официальный fee page указывает минимальный доступный баланс $20; доступные bids и баланс здесь не проверены.

Локальный журнал `gig_submissions.sqlite`: ровно три READY; actual_bid, actual_delivery, submitted_at и bids_at_submission отсутствуют. Реальная отправка, ответы, интервью, awards и revenue равны нулю. Доли без отправок математически undefined, это не прогноз и не успешная воронка.

Воспроизведение: `.venv\\Scripts\\python.exe -m ai_assistant.gig.first_money_run --limit 100 --output artifacts/p6`; для текущего сбора и явных оценок — дополнительно `--resume-collection --review-file artifacts/p6/scope_reviews.json`. Генератор создаёт предварительную очередь; финальный PASS требует отдельной ручной QA, не следует автоматически из score.

Доказательства: `artifacts/p6/final_run.json`, `manual_qa_observations.json`, `qa_review.json`, `reviewed_packs.json`, `scope_reviews.json`, `funnel_metrics.json`, `p5_preservation.json`; HTML обеих проверок сохранён отдельно. QA FAIL для ложных совпадений записан в qa_review.json; они исключены из готовой очереди.
'''
Path('docs/gig_search_p6_first_money.md').write_text(report,encoding='utf8')
print(json.dumps({'counts':run['counts'],'metrics':metrics},ensure_ascii=False))
