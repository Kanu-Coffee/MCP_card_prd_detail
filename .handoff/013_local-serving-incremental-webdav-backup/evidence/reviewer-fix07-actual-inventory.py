import sys,asyncio,json,tempfile
from pathlib import Path
sys.path[:0]=['/workspace/packages/cardrag-core/src','/workspace/apps/cardrag-worker/src']
from cardrag_core import NativeOCRContract
from cardrag_worker.ocr import OCRResolver,PriorLocalNativeSource
from cardrag_worker.state import WorkerState
class NoCalls:
 provider='opencode';model='alibaba-token-plan/qwen3.8-flash';reasoning_effort='medium'
 def __init__(self):self.calls=0
 async def recognize(self,*args,**kwargs):self.calls+=1;raise RuntimeError('provider forbidden')
async def main():
 root=Path('/state');run_id='025ce35739944d8facfdda4c99961f0c';run=root/'runs'/run_id
 seal=json.loads((run/'sealed/publish.json').read_bytes());m=seal['manifest']
 ledger=json.loads(next((root/'audit-reports/state-seed').glob('*.json')).read_bytes());seed={x['document_id'] for x in ledger['ocr_documents']}
 candidates=[d for d in m['documents'] if d['document_id'] not in seed]
 provider=NoCalls();rows=[]
 with tempfile.TemporaryDirectory() as tmp:
  tmp=Path(tmp);state=WorkerState(tmp/'state.sqlite3');resolver=OCRResolver(state=state,provider=provider,webdav=None,chunk_pages=2)
  contracts=[NativeOCRContract.model_validate_json(json.dumps(json.loads(f.read_bytes())['contract'])) for f in (run/'documents').glob('*/ocr/native-manifest.json')]
  resolver.set_compatible_contracts(contracts)
  for d in candidates:
   doc=d['document_id'];pdf=d['pdf'];ocr=d['ocr'];native=(run/'documents'/doc/'ocr/native-manifest.json').is_file()
   prior=PriorLocalNativeSource(runs_root=root/'runs',run_id=run_id,generation_id=m['generation_id'],corpus_sha256=m['corpus_sha256'],contract_sha256=m['contract_sha256'],document_id=doc,pdf_sha256=pdf['sha256'],pdf_size_bytes=pdf['size_bytes'],page_count=d['page_count'],ocr_sha256=ocr['sha256'],ocr_size_bytes=ocr['size_bytes'],cache_kind=d['ocr_cache_kind'],reuse_key=d['ocr_reuse_key'],variant_id=d['ocr_variant_id'])
   try:
    result=await resolver.resolve(run_id='independent-review',document_id=doc,pdf_path=tmp/'unused.pdf',pdf_sha256=pdf['sha256'],pdf_size_bytes=pdf['size_bytes'],page_count=d['page_count'],output_dir=tmp/'out'/doc,prior_local_native=prior)
    rows.append({'document_id':doc,'native':native,'cache_hit':result.cache_reused,'provider_called':result.provider_called,'same_ocr':result.ocr_sha256==ocr['sha256'],'same_variant':result.cache_variant_id==d['ocr_variant_id'],'cache_kind':result.cache_kind,'provider':result.provider,'model':result.model})
   except Exception as e:rows.append({'document_id':doc,'native':native,'error':type(e).__name__})
  state.close()
 print(json.dumps({'count':len(rows),'content_count':sum(not r['native'] for r in rows),'native_count':sum(r['native'] for r in rows),'calls':provider.calls,'hits':sum(r.get('cache_hit',False) for r in rows),'same_ocr':sum(r.get('same_ocr',False) for r in rows),'variant_matches':sum(r.get('same_variant',False) for r in rows),'exceptions':[r for r in rows if 'error' in r],'native_results':[r for r in rows if r['native']],'content_variant_mismatches':[r for r in rows if not r['native'] and not r.get('same_variant',False)]},indent=2))
asyncio.run(main())
