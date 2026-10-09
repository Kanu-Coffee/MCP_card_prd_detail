import asyncio,hashlib,json,tempfile,sys
from pathlib import Path
sys.path.insert(0,'apps/cardrag-worker/tests')
from test_ocr import FakeProvider,PDF_SHA
from cardrag_core import OCRInput,content_addressed_ocr_reuse_key
from cardrag_worker.content_cache import ContentOCRVariantStore
from cardrag_worker.ocr import OCRResolver,PriorLocalNativeSource
from cardrag_worker.state import WorkerState
async def main():
 with tempfile.TemporaryDirectory() as tmp:
  root=Path(tmp);doc='doc_sample';prior=root/'runs/old';d=prior/'documents'/doc/'ocr';d.mkdir(parents=True)
  body='## Page 1\n\n혜택 안내입니다.\n'.encode();sha=hashlib.sha256(body).hexdigest();(d/'ocr.md').write_bytes(body)
  src=OCRInput(pdf_sha256=PDF_SHA,pdf_size_bytes=1000,page_count=1);key=content_addressed_ocr_reuse_key(src,cache_epoch=0)
  orig_variant='c'*64
  manifest={'generation_id':'g-old','documents':[{'document_id':doc,'issuer':'hana','availability':'available','page_count':1,'pdf':{'sha256':PDF_SHA,'size_bytes':1000},'ocr':{'sha256':sha,'size_bytes':len(body)},'ocr_cache_kind':'content','ocr_reuse_key':key,'ocr_variant_id':orig_variant}]}
  (prior/'sealed').mkdir();(prior/'sealed/publish.json').write_text(json.dumps({'manifest':manifest}))
  state=WorkerState(root/'state.sqlite3');provider=FakeProvider();resolver=OCRResolver(state=state,provider=provider,webdav=None,chunk_pages=1)
  p=PriorLocalNativeSource(runs_root=root/'runs',run_id='old',generation_id='g-old',corpus_sha256='a'*64,contract_sha256='b'*64,document_id=doc,pdf_sha256=PDF_SHA,pdf_size_bytes=1000,page_count=1,ocr_sha256=sha,ocr_size_bytes=len(body),cache_kind='content',reuse_key=key,variant_id=orig_variant,provider='local-paddleocr',model='PaddleOCR-VL-1.6')
  result=await resolver.resolve(run_id='new',document_id=doc,pdf_path=root/'sample.pdf',pdf_sha256=PDF_SHA,pdf_size_bytes=1000,page_count=1,output_dir=root/'out',prior_local_native=p)
  store=ContentOCRVariantStore(state_root=root,webdav=None)
  epoch_hit=await store.lookup(run_id='epoch-new',source=src,cache_epoch=1,document_id=doc)
  again=await resolver.resolve(run_id='new',document_id=doc,pdf_path=root/'sample.pdf',pdf_sha256=PDF_SHA,pdf_size_bytes=1000,page_count=1,output_dir=root/'out',prior_local_native=p)
  print(json.dumps({'provider_calls':len(provider.calls),'cache_hit':result.cache_reused,'expected_variant':orig_variant,'returned_variant':result.cache_variant_id,'expected_provider':p.provider,'returned_provider':result.provider,'expected_model':p.model,'returned_model':result.model,'same_input_variant_stable':result.cache_variant_id==again.cache_variant_id,'epoch1_hit_of_epoch0':epoch_hit is not None,'epoch1_returned_key':epoch_hit.manifest.reuse_key if epoch_hit else None,'epoch1_expected_key':content_addressed_ocr_reuse_key(src,cache_epoch=1)},ensure_ascii=False,indent=2))
  state.close()
asyncio.run(main())
