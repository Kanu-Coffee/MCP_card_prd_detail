"""Focused review: fee regression and negative table offers, no runtime mutation."""
import json
from pathlib import Path
from types import SimpleNamespace

from cardrag_core.derived_metadata import LaunchDateResolution
from cardrag_mcp.catalog import CatalogRepository
from cardrag_mcp.summary_fields import summary_candidates

root=Path(__file__).resolve().parent
saved=json.loads((root/'readonly-comparison.json').read_text())
results=[]
for code in ['00917','00549','00157']:
    fixture=next(f for f in saved['fixtures'] if f['summary']['product_code']==code)
    row=fixture['summary']
    actual=CatalogRepository._summary(SimpleNamespace(generation_id=row['generation_id']),row,fixture['nodes'],{},LaunchDateResolution(None,'missing'),(),())
    results.append({'case':'fee_regression','issuer':row['issuer'],'product_code':code,'before':row['annual_fee_text'],'after':actual.annual_fee_text,'revision':row['contract_revision_id']})
for label,value in [('온라인','5% 포인트 미적립'),('마일리지 적립','×')]:
    row={'node_id':'review-table','node_type':'TABLE_ROW','parent_id':None,'raw_heading':None,'display_text':f'| {label} | {value} |','table_role':'BODY','table_headers_json':json.dumps(['구분','혜택']),'table_cells_json':json.dumps([label,value],ensure_ascii=False)}
    results.append({'case':'negative_offer','source':row,'returned':[{'field':c.field,'heading':c.heading,'text':c.text} for c in summary_candidates([row])]})
print(json.dumps(results,ensure_ascii=False,indent=2))
