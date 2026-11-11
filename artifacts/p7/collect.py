"""Legacy P7 collector; disabled because it stores provider data as plaintext."""
raise SystemExit('Legacy P7 collector disabled; use the policy-gated P6/P8 workflow')

import json
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from ai_assistant.gig.sources.freelancer import FreelancerGigSource
from ai_assistant.gig.live_project import fetch_public_project, overlay_public_project
from ai_assistant.gig.pipeline import GigPipeline
from ai_assistant.gig.assessment import load_candidate_profile, skill_confidences
from ai_assistant.gig.grounded import analyze
from ai_assistant.gig.capability import assess_capability
from ai_assistant.gig.client import assess_client_risk
from ai_assistant.gig.first_money import first_money_score_of

dest=Path('artifacts/p7'); source=FreelancerGigSource(delay_seconds=.5,max_requests=120,timeout_seconds=8)
old=json.loads(Path('artifacts/p6/final_run.json').read_text(encoding='utf8'))
old_checks=[]
for e in old['candidates']:
    try:
        html=source._http_get(e['project_url']); (dest/f"old_{e['project_id']}.html").write_text(html,encoding='utf8')
        page=source.record_from_html(html,page_url=e['project_url'],fetched_at=datetime.now(timezone.utc).isoformat())
        fresh,api=fetch_public_project(page,source=source)
        prior=next(o['raw_live'] for o in old['observations'] if o['source_id']==e['project_id'])
        old_checks.append({'project_id':e['project_id'],'raw_live':fresh,'api_result':api,'scope_unchanged':fresh['description']==prior['description']})
        print('OLD',e['project_id'],fresh['source_status'],fresh['competition'],flush=True)
    except Exception as exc:
        old_checks.append({'project_id':e['project_id'],'error':str(exc)})
(dest/'old_ready_recheck.json').write_text(json.dumps(old_checks,ensure_ascii=False,indent=2),encoding='utf8')
records={}; requests=[]
for term in ['python','scraping','automation','n8n','excel','api']:
    params=urllib.parse.urlencode({'query':term,'limit':50,'full_description':'true','job_details':'true','sort_field':'time_submitted'})
    url='https://www.freelancer.com/api/projects/0.1/projects/active/?'+params
    assert source._robots_allows(url)
    with urllib.request.urlopen(url,timeout=20) as response: payload=json.load(response)
    assert payload['status']=='success'
    when=datetime.now(timezone.utc).isoformat()
    (dest/f'collection_{term}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    requests.append({'query':term,'url':url,'observed_at':when,'count':len(payload['result']['projects'])})
    for project in payload['result']['projects']:
        ident='fl_'+str(project['id']); slug=project.get('seo_url')
        if not slug: continue
        base={'source':'freelancer','source_id':ident,'url':'https://www.freelancer.com/projects/'+slug,
              'source_mode':'live','source_scope':'public_jobs','source_facts':{'found_via':[]}}
        raw=overlay_public_project(base,project,observed_at=when,api_url=url)
        if ident in records:
            raw['source_facts']['found_via']=records[ident]['source_facts']['found_via']
        raw['source_facts']['found_via'].append(term)
        records[ident]=raw
    print('COLLECT',term,len(records),flush=True)
profile=load_candidate_profile(); pipe=GigPipeline(profile=profile,persist=False); now=datetime.now(timezone.utc)
ranking=[]
for raw in records.values():
    gig=pipe.process_raw(raw); analysis=analyze(gig.description,gig.title)
    cap=assess_capability(analysis,skill_confidences(profile),profile_skills=profile['skill_confidence'])
    risk=assess_client_risk(gig,now=now); score=first_money_score_of(gig,analysis,cap,risk,now=now)
    age=(now-datetime.fromisoformat(raw['published_at'])).total_seconds()/3600 if raw['published_at'] else None
    ranking.append({'id':raw['source_id'],'title':gig.title,'age_h':age,'bids':gig.competition,'budget':gig.budget_text,
        'fit':cap.fit,'missing':cap.missing,'score':score.score,'effort':gig.estimated_effort_hours,'url':gig.url})
ranking.sort(key=lambda e:(e['fit'] not in ['proven','strong'], e['bids'] is None or e['bids']>50, e['age_h'] is None or e['age_h']>6,-e['score']))
for name,payload in [('fresh_collection.json',list(records.values())),('collection_requests.json',requests),('ranking.json',ranking)]:
    (dest/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(ranking[:30],ensure_ascii=False,indent=2))
