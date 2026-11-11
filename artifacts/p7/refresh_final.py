"""Legacy P7 refresh; disabled because it reads and writes unbounded plaintext data."""
raise SystemExit('Legacy P7 refresh disabled; use the policy-gated P6/P8 workflow')

import json
from pathlib import Path
from datetime import datetime,timezone
from ai_assistant.gig.sources.freelancer import FreelancerGigSource
from ai_assistant.gig.live_project import fetch_public_project
from ai_assistant.gig.pipeline import GigPipeline
from ai_assistant.gig.assessment import load_candidate_profile
from ai_assistant.gig.client import assess_client_risk

dest=Path('artifacts/p7'); records=json.loads((dest/'fresh_collection.json').read_text(encoding='utf8'))
ids=['fl_40750263', 'fl_40750305', 'fl_40750311', 'fl_40750525', 'fl_40749362']
source=FreelancerGigSource(delay_seconds=.5,max_requests=40,timeout_seconds=8); pipe=GigPipeline(profile=load_candidate_profile(),persist=False)
verified=[]
for ident in ids:
    raw=next(x for x in records if x['source_id']==ident)
    try:
        html=source._http_get(raw['url']); path=dest/f'verify_{ident}.html'; path.write_text(html,encoding='utf8')
        page=source.record_from_html(html,page_url=raw['url'],fetched_at=datetime.now(timezone.utc).isoformat(),found_via=raw['source_facts']['found_via'])
        live,api=fetch_public_project(page,source=source); gig=pipe.process_raw(live); risk=assess_client_risk(gig)
        verified.append({'id':ident,'raw_live':live,'api_result':api,'risk':risk.to_dict(),'html':str(path.resolve())})
        print(ident,live['source_status'],live['competition'],risk.level,risk.reasons,flush=True)
    except Exception as exc: print(ident,type(exc).__name__,str(exc),flush=True)
(dest/'final_observations.json').write_text(json.dumps(verified,ensure_ascii=False,indent=2),encoding='utf8')
