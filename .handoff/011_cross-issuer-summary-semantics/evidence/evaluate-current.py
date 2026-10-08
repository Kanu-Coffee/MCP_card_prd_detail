"""Run old and candidate catalog on the same read-only generation, without deployment."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
receiver = r'''
import json,random,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
from cardrag_mcp.catalog import CatalogRepository,_CURRENT_PRODUCTS
from cardrag_mcp.metadata_cache import MetadataCache
from cardrag_mcp.models import ProductSummaryRequest
import cardrag_mcp.catalog as catalog
import cardrag_mcp.summary_fields as fields
payload=json.load(sys.stdin)
root=Path('/var/lib/cardrag-mcp');gid=json.loads((root/'current.json').read_text())['generation_id'];db=root/'generations'/gid/'index.sqlite3'
def connect():
 c=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row;return c
handle=SimpleNamespace(generation_id=gid,metadata=SimpleNamespace(schema_id='cardrag.serving-db.v6'),connect=connect)
with connect() as c:rows=[dict(r) for r in c.execute(_CURRENT_PRODUCTS+' ORDER BY pl.issuer,pl.product_code,cr.contract_revision_id')]
excluded={(x['issuer'],x.get('product_code',x['identifier'])) for x in payload['manifest']['requests']}
unique={}
for row in rows:unique.setdefault((row['issuer'],row['product_code']),row)
pool=[r for key,r in unique.items() if key not in excluded]; rng=random.Random(20261008); selected=[]
for issuer in sorted({r['issuer'] for r in pool}):
 choices=[r for r in pool if r['issuer']==issuer];selected.extend(rng.sample(choices,min(2,len(choices))))
selected.extend(rng.sample([r for r in pool if r not in selected],30-len(selected)))
requests=[ProductSummaryRequest(issuer=x['issuer'],identifier=x['identifier']) for x in payload['manifest']['requests']]
requests.extend(ProductSummaryRequest(issuer=r['issuer'],identifier=r['contract_revision_id']) for r in selected)
before=[item for start in range(0,len(requests),50) for item in CatalogRepository(None,MetadataCache()).summaries(handle,requests[start:start+50]).items]
fixtures=[]
with connect() as c:
 for req,summary in zip(requests,before,strict=True):
  if summary is None:raise RuntimeError('Selected product missing')
  nodes=[dict(r) for r in c.execute('SELECT node_id,parent_id,node_type,major_class,raw_heading,ordinal,display_text,table_role,table_headers_json,table_cells_json FROM structure_nodes WHERE contract_revision_id=? ORDER BY ordinal',(summary.contract_revision_id,))]
  fixtures.append({'request':req.model_dump(mode='json'),'summary':summary.model_dump(mode='json'),'nodes':nodes})
exec(compile(payload['fields'],'candidate-summary-fields','exec'),fields.__dict__)
exec(compile(payload['catalog'],'candidate-catalog','exec'),catalog.__dict__)
after=[item for start in range(0,len(requests),50) for item in catalog.CatalogRepository(None,MetadataCache()).summaries(handle,requests[start:start+50]).items]
print(json.dumps({'generation_id':gid,'seed':20261008,'selection':'two per issuer then remainder random; exclude old34 by issuer/product_code, no output filtering','population_count':len(unique),'random_count':len(selected),'fixtures':fixtures,'after':[s.model_dump(mode='json') for s in after]},ensure_ascii=False,indent=2))
'''
repository = root.parents[2]
payload = {'manifest': json.loads((root/'sample-manifest.json').read_text()),
           'fields': (repository/'apps/cardrag-mcp/src/cardrag_mcp/summary_fields.py').read_text(),
           'catalog': (repository/'apps/cardrag-mcp/src/cardrag_mcp/catalog.py').read_text()}
result = subprocess.run(['docker','exec','-i','cardrag-mcp','python','-c',receiver],input=json.dumps(payload),text=True,capture_output=True,check=False)
if result.returncode:
    raise RuntimeError(result.stderr)
Path('/tmp/cardrag011-evaluation.json').write_text(result.stdout)
print('Read-only baseline/candidate evaluation saved to /tmp/cardrag011-evaluation.json')
