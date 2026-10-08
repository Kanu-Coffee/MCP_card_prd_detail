import json,sqlite3
from pathlib import Path
from types import SimpleNamespace
from cardrag_mcp.catalog import CatalogRepository,_CURRENT_PRODUCTS
from cardrag_mcp.metadata_cache import MetadataCache
from cardrag_mcp.models import ProductSummaryRequest
root=Path('/var/lib/cardrag-mcp');gid=json.loads((root/'current.json').read_text())['generation_id'];db=root/'generations'/gid/'index.sqlite3'
def connect():
 c=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row;return c
handle=SimpleNamespace(generation_id=gid,metadata=SimpleNamespace(schema_id='cardrag.serving-db.v6'),connect=connect)
products=[('shinhan','01208'),('kb','00917'),('bc','BD001'),('kb','09063')]
results=[]
for issuer,code in products:
 summary=CatalogRepository(None,MetadataCache()).summaries(handle,[ProductSummaryRequest(issuer=issuer,identifier=code)]).items[0]
 if summary is None:results.append({'issuer':issuer,'product_code':code,'missing':True});continue
 with connect() as c:
  nodes=[dict(r) for r in c.execute('SELECT node_id,parent_id,node_type,major_class,raw_heading,ordinal,display_text,table_role,table_headers_json,table_cells_json FROM structure_nodes WHERE contract_revision_id=? ORDER BY ordinal',(summary.contract_revision_id,))]
  spans=[dict(r) for r in c.execute('SELECT node_id,page,source_start,source_end FROM node_spans WHERE contract_revision_id=?',(summary.contract_revision_id,))]
 results.append({'summary':summary.model_dump(mode='json'),'nodes':nodes,'spans':spans})
print(json.dumps({'generation_id':gid,'method':'current MCP readonly immutable DB and actual CatalogRepository; no OCR/embedding/LLM','products':results},ensure_ascii=False,indent=2))
