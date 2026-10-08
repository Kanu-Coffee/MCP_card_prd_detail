"""Reviewer probes assert the CURRENT bugs, not the desired fixed behavior.
Run with PYTHONPATH=apps/cardrag-worker/tests .venv/bin/pytest -c pyproject.toml -o asyncio_mode=auto -q <this-file>.
All providers, publication and Worker state are synthetic.
"""
import json
from pathlib import Path
import pytest
from test_partial_execution import corpus
from cardrag_mcp.summary_fields import summary_candidates
from cardrag_worker.partial_execution import ExecutionPlan
from cardrag_worker.webdav import RemoteGenerationIdentity
from cardrag_core import channel_pointer_path


def n(text, kind='PARAGRAPH', node_id='n', parent=None, heading=None):
    return dict(node_id=node_id,node_type=kind,display_text=text,parent_id=parent,raw_heading=heading,table_role=None)


def test_exclusion_currently_appears_as_benefit():
    rows=summary_candidates([n('1.2% 할인 대상에서 제외')])
    assert {r.field for r in rows} == {'benefit','condition'}


def test_percentage_only_table_currently_lost():
    rows=summary_candidates([n('국내외 할인', 'MAJOR_SECTION','h',heading='국내외 할인'),
        n('| 국내 | 1.2% |','TABLE_ROW',parent='h')])
    assert not any(r.node_id=='n' for r in rows)


async def test_stable_source_change_before_export_is_not_fenced(corpus, monkeypatch):
    pipeline,source,state,_,_,_,dav=corpus
    runner=pipeline(ExecutionPlan(frozenset({'pdf','ocr','embedding'}),source.run_id,channel='stable'),source)
    dav.channel='stable'
    dav.pointer_path=channel_pointer_path('stable')
    dav.stable_publication_approved=True
    runner.stable_publication_approved=True
    dav.current=RemoteGenerationIdentity(generation_id=source.manifest.generation_id,
        corpus_sha256=source.manifest.corpus_sha256,contract_sha256=source.manifest.contract_sha256,
        generation_schema=source.manifest.schema_version,serving_schema=source.manifest.serving_schema)
    # This is the exact source match that run_partial checks at startup.
    assert (await dav.validated_current_generation()).generation_id==source.manifest.generation_id
    original=runner.exporter_v5.export
    def export_after_other_writer(*args,**kwargs):
        value=original(*args,**kwargs)
        dav.install_other_candidate_head()
        return value
    monkeypatch.setattr(runner.exporter_v5,'export',export_after_other_writer)
    result=await runner.run()
    seal=json.loads((source.root/'runs'/result.run_id/'sealed/publish.json').read_text())
    assert seal['manifest']['previous_generation_id']=='g-other-candidate-head'
    assert result.status=='succeeded'
    assert seal['manifest']['corpus_sha256']==source.manifest.corpus_sha256

async def test_failed_document_is_carried_even_when_ocr_is_selected(corpus):
    from types import SimpleNamespace
    from cardrag_worker.partial_execution import replay_pdf_source
    pipeline,source,state,_,_,ocr,_=corpus
    original=next(iter(source.documents.values()))
    source.documents={original.document_id:original.model_copy(update={'availability':'ocr_failed','ocr':None})}
    source.revisions={}
    source.failed=[dict(document_id=original.document_id,issuer='testbank',product_code='test-001',name='테스트 카드',title='테스트 카드',reason_code='ocr_provider_failed',reason='fixture failure',attempts=1)]
    captures={}
    async def build(**kwargs):
        captures.update(kwargs)
        return SimpleNamespace(status='local_only')
    runner=SimpleNamespace(reuse_source=source,execution_plan=ExecutionPlan(frozenset({'pdf','webdav'}),source.run_id),
        v5_profile=pipeline().v5_profile,state=state,state_dir=source.root,adapters=pipeline().adapters,
        _active_ocr_request=None,ocr=ocr,_build_v5_generation=build,contract_sha256=source.manifest.contract_sha256)
    before=ocr.calls
    await replay_pdf_source(runner,'review-control-flow')
    assert ocr.calls==before and not captures['processed'] and len(captures['failed_documents'])==1
