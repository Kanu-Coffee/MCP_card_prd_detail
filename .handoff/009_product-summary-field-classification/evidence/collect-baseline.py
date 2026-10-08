import json,sqlite3
from pathlib import Path
from types import SimpleNamespace
from cardrag_mcp.catalog import CatalogRepository, _CURRENT_PRODUCTS
from cardrag_mcp.metadata_cache import MetadataCache
from cardrag_mcp.models import ProductSummaryRequest
root=Path('/var/lib/cardrag-mcp');gid=json.loads((root/'current.json').read_text())['generation_id'];db=root/'generations'/gid/'index.sqlite3'
def connect():
 c=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row;return c
with connect() as c:
 schema='cardrag.serving-db.v6' if c.execute("SELECT 1 FROM sqlite_master WHERE name='derived_field_evidence'").fetchone() else 'cardrag.serving-db.v5'
 rows=c.execute(_CURRENT_PRODUCTS+' ORDER BY pl.issuer,cr.effective_date DESC,pl.product_code').fetchall()
 selected=[r for r in rows if r['issuer']=='woori' and r['product_code']=='500107']
 for issuer in sorted({r['issuer'] for r in rows}):
  choices=[r for r in rows if r['issuer']==issuer and r not in selected]
  selected.extend(choices[:8 if issuer=='woori' else 3])
handle=SimpleNamespace(generation_id=gid,metadata=SimpleNamespace(schema_id=schema),connect=connect)
requests=[ProductSummaryRequest(issuer=r['issuer'],identifier=r['contract_revision_id']) for r in selected]
summaries=CatalogRepository(None,MetadataCache()).summaries(handle,requests)
target=next(s for s in summaries.items if s.issuer=='woori' and s.product_code=='500107')
with connect() as c:
 nodes=[dict(r) for r in c.execute('SELECT node_id,parent_id,node_type,major_class,raw_heading,ordinal,display_text,table_role,table_headers_json,table_cells_json FROM structure_nodes WHERE contract_revision_id=? ORDER BY ordinal',(target.contract_revision_id,))]
 spans=[dict(r) for r in c.execute('SELECT node_id,page,source_start,source_end FROM node_spans WHERE contract_revision_id=? ORDER BY node_id,page',(target.contract_revision_id,))]
 pages=[dict(r) for r in c.execute('SELECT page,text FROM document_pages WHERE contract_revision_id=? ORDER BY page',(target.contract_revision_id,))]
print(json.dumps({'generation_id':gid,'schema_id':schema,'method':'Running MCP CatalogRepository.summaries; readonly immutable serving DB, no vector loading, no OCR or LLM','sample_selection':'500107 plus 8 other latest-effective-date Woori revisions and 3 per other issuer; exploratory sample, not exhaustive quality evaluation','sample_count':len(selected),'summaries':[s.model_dump(mode='json') for s in summaries.items],'target_nodes':nodes,'target_spans':spans,'target_pages':pages},ensure_ascii=False,indent=2))
