import json,re
from pathlib import Path
from types import SimpleNamespace
from cardrag_core.derived_metadata import LaunchDateResolution
from cardrag_mcp.catalog import CatalogRepository
root=Path('.handoff/011_cross-issuer-summary-semantics/evidence');saved=json.loads((root/'readonly-comparison.json').read_text());reported=json.loads((root/'fix01-comparison.json').read_text());by_key={(p['issuer'],p['product_code']):p['new_after_fix01'] for p in reported['products']}
normalize=lambda t:' '.join(re.sub(r'<br\s*/?>',' ',t or '',flags=re.I).split())
fields=['annual_fee_text','benefit_headings','benefit_summary_texts','condition_summary_texts'];changes=[];raw_fee_match=0;normalized_fee_match=0;source_checked=0
for f,prev in zip(saved['fixtures'],saved['after'],strict=True):
 row=f['summary'];nodes={n['node_id']:n for n in f['nodes']}
 actual=CatalogRepository._summary(SimpleNamespace(generation_id=row['generation_id']),row,f['nodes'],{},LaunchDateResolution(None,'missing'),(),()).model_dump(mode='json')
 reported_after=by_key[(row['issuer'],row['product_code'])]
 assert all(actual[k]==reported_after[k] for k in fields),(row['issuer'],row['product_code'])
 raw_fee_match+=actual['annual_fee_text']==row['annual_fee_text'];normalized_fee_match+=normalize(actual['annual_fee_text'])==normalize(row['annual_fee_text'])
 assert not set(actual['benefit_summary_texts'])&set(actual['condition_summary_texts'])
 for e in actual['evidence']:
  if e['field']=='launch_date':continue
  source_checked+=1
  assert normalize(e['excerpt']) in normalize(nodes[e['node_id']]['display_text'])
 changed=[k for k in fields if actual[k]!=prev[k]]
 if changed:changes.append({'issuer':row['issuer'],'product_code':row['product_code'],'changed_fields':changed})
print(json.dumps({'commit':'72c28ac','products':64,'reported_output_matches':64,'raw_fee_matches':raw_fee_match,'normalized_fee_matches':normalized_fee_match,'whole_offer_duplicates':0,'source_evidence_checked':source_checked,'source_issues':0,'changes_since_pre_fix':changes},ensure_ascii=False,indent=2))
