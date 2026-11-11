"""Legacy P6 refresh; disabled because it reads and writes unbounded plaintext data."""
raise SystemExit('Legacy P6 refresh disabled; use the policy-gated P6/P8 workflow')

import json
from datetime import datetime, timezone
from pathlib import Path
from ai_assistant.gig.sources.freelancer import FreelancerGigSource
from ai_assistant.gig.live_project import fetch_public_project

dest = Path('artifacts/p6')
prepared = json.loads((dest/'prepared_run.json').read_text(encoding='utf8'))
packs = json.loads((dest/'reviewed_packs.json').read_text(encoding='utf8'))
source = FreelancerGigSource(target_skills=[], max_requests=20, timeout_seconds=8)
rows = []
for ident in packs:
    prior = next(e for e in prepared['candidates'] if e['project_id'] == ident)
    html = source._http_get(prior['project_url'])
    path = dest/f'qa_{ident}.html'
    path.write_text(html,encoding='utf8')
    page = source.record_from_html(html,page_url=prior['project_url'],fetched_at=datetime.now(timezone.utc).isoformat())
    assert page is not None
    fresh, api = fetch_public_project(page,source=source)
    old = next(o for o in prepared['observations'] if o['source_id']==ident)
    rows.append({'id':ident,'raw_live':fresh,'api_result':api,'page_path':str(path.resolve()),
                 'description_unchanged':fresh['description']==old['raw_live']['description'],
                 'prior_bids':prior['bids_live'],'current_bids':fresh['competition']})
    print(ident, fresh['source_status'],fresh['competition'],fresh['currency'],fresh['budget'],flush=True)
(dest/'manual_qa_observations.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
