from datetime import UTC, datetime
import pytest
from test_partial_execution import corpus, failed_corpus
from cardrag_worker.partial_execution import ExecutionPlan
from cardrag_worker.ocr_requests import OCRReprocessRequest, OCRRequestTarget, queue_reprocess_requests
from cardrag_worker.webdav import RemoteGenerationIdentity

async def test_failed_pending_target_must_not_be_acknowledged(failed_corpus, corpus):
    factory, source, ocr, _ = failed_corpus
    dav = corpus[-1]
    document = source.documents[source.failed[0]['document_id']]
    request = OCRReprocessRequest(request_id='ocr-review-pending-failure', source_generation_id=source.manifest.generation_id, created_at=datetime.now(UTC), targets=(OCRRequestTarget(document_id=document.document_id, pdf_sha256=document.pdf.sha256, pdf_size_bytes=document.pdf.size_bytes, page_count=document.page_count),))
    queue_reprocess_requests(source.root, (request,))
    runner = factory({'pdf'})
    runner.execution_plan = ExecutionPlan(frozenset({'pdf'}), source.run_id)
    runner.webdav = dav
    dav.current = RemoteGenerationIdentity(generation_id=source.manifest.generation_id, corpus_sha256=source.manifest.corpus_sha256, contract_sha256=source.manifest.contract_sha256, generation_schema=source.manifest.schema_version, serving_schema=source.manifest.serving_schema, ocr_failed_document_count=1)
    result = await runner.run()
    assert result.status == 'succeeded'
    assert document.document_id in ocr.failed_ids
    assert not (source.root/'ocr-requests/completed/ocr-review-pending-failure.json').exists(), 'failed OCR target was incorrectly acknowledged as completed'
