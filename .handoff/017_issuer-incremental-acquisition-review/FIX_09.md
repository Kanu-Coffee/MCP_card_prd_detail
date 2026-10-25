# FIX_09 — publication-only 재개의 ZIP 오류 증거를 게시 전에 고정

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4` 및 미커밋 017/FIX_08 구현.

## 판정

**수정 필요 1건(P2), FIX_08 A1의 미완료 부분.** FIX_08의 PDF 복구 유지와 우리 ZIP 다운로드 준비 보완은 확인했다. 최초 process의 오류 수와 이미 execution-result.json이 남은 재개도 통과한다. 그러나 결과 파일이 아직 없는 실제 중단 시점과 일반 run의 publication-only 재개는 여전히 ZIP 오류를 0/complete로 바꾼다.

PLAN/ANALYSIS/REPORT, FIX_01–08와 존재하는 각 REPORT, 기존 REVIEW.md, 현재 Git status/diff 및 수정 코드·정식 테스트를 대조했다. 이번 Reviewer는 제품/정식 테스트/운영 설정/서비스/배포를 수정하지 않았고 실제 origin을 호출하지 않았다. ZIP 자체 오류 처리·압축 해제 금지 방침은 유지한다.

## A1. [P2] 성공 후 결과 파일에 의존하지 말고 오류 증거를 첫 게시 전에 영속화할 것

위치: `pipeline.py:SealedPublicationResumer._resume_publication_locked`의 8524–8554, `pipeline.py:2858-2869` 결과 파일 writer, publication seal 생성 경로.

현재 publication-only resumer는 자기 run 디렉터리의 acquisition.json과 execution-result.json만 읽고 없으면 오류 수 기본값 0으로 게시한다. 두 파일 모두 없는 유효한 실행 상태가 있다.

1. **실제 process 완료 직전 중단:** 정상 PDF+ZIP collect 후 process에서 finish_run(status=succeeded) 호출에 전용 BaseException을 주입했다. remote pointer가 새 generation을 가리키고 로컬 run은 running이며 execution-result.json은 아직 없음을 확인했다. 이 상태를 SealedPublicationResumer로 복구하면 count=0/complete다. collect receipt는 별도의 collect run 디렉터리에 있으므로 현재 자기 디렉터리 조회로는 읽히지 않는다.
2. **일반 all-stage run:** PDF+ZIP 수집 결과는 count=1/degraded다. 이 run에는 execution_plan이 없어 execution-result.json 자체를 생성하지 않는다. remote current를 그 결과로 맞추고 run 완료 기록 누락을 모사한 뒤 publication-only 재개하면 역시 count=0/complete다.

두 독립 재현 모두 완료 기대값 count=1/degraded 검사에서 실패했다. serving 삭제나 추가 ZIP 해제가 확인된 것은 아니며, 재개 결과가 남아 있는 첨부 오류를 정상으로 오보고하는 문제다.

현재 정식 테스트 test_zip_error_survives_own_seal_resume는 process를 끝까지 성공시켜 결과 파일을 만든 뒤 interrupted로 바꾼다. 따라서 파일 생성 전 중단은 검사하지 못한다. FIX_08의 “첫 publish 전에 durable한 오류 증거 고정” 요구는 아직 구현되지 않았으며, 결과 파일 writer에 필드를 추가하는 것만으로 충족되지 않는다.

### 수정 방향

- source별 오류 identity·사유 및 count/degraded 상태를 **첫 remote pointer 게시 이전**에 durable하게 고정한다. 일반 run, collect/process, 자체 seal 재개, publication-only 재개가 같은 검증된 증거를 재사용하도록 한다.
- 기존 seal의 검증된 metadata/기존 run checkpoint 또는 hash가 seal에 연결되는 별도 error artifact 등 실제 구조에 맞는 방법을 선택한다. 결과 파일 생성 여부가 오류 존재 여부를 결정하지 않아야 한다.
- process source receipt에 의존한다면 persisted execution plan의 정확한 source_run_id 및 검증된 acquisition receipt와 연결한다. 다른 run의 오류 파일을 임의로 탐색해 가져오지 않는다. 일반 run에도 같은 공통 영속 경로가 필요하다.
- 예상되는 오류 증거가 손상·유실됐는데 catch-all 후 조용히 0/complete로 처리하지 않는다. 명시적인 복구 실패/불명 상태 등 정책을 정의하고 기존 seal 소비 호환성을 유지한다.
- 최신 pointer fence, seal/contract/object 검증, 오류 해소된 source만 제거하는 정책, 선택 범위·백업·GC pin, provider-free 재개는 유지한다. ZIP 압축 해제나 특정 상품 예외를 추가하지 않는다.

### 인수 및 회귀

- 아래 두 재현을 정식 테스트로 편입해 count=1/degraded 및 source별 오류 identity/사유를 검사한다.
- 특히 finish_run 직전 또는 pointer commit 직후 실제 경계에 중단을 주입한다. 완료 후 상태만 바꾸는 시험으로 대체하지 않는다. execution-result.json이 없는 상태를 assertion으로 고정한다.
- CLI resume-publication 및 일반/선택/순회가 사용하는 동일 resumer 경로의 결과와 운영 출력도 대조한다. 정상 serving 보존, 추가 origin/OCR/embedding 호출 0, zipfile 호출 0.
- 기존 최초 process, 결과 파일 있는 재개, 새 객체 own-seal 재개, PDF 복구 유지, 실제 우리 prepare, 최신 pointer 충돌/손상 seal 회귀도 유지한다.

## Reviewer 직접 검증

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q --tb=short` → **55 passed, 8.66초**.
- `uv run --no-sync ruff check apps/cardrag-worker/` → 통과.
- `uv run --no-sync ruff format --check apps/cardrag-worker/` → 123 files already formatted.
- `git diff --check` → 통과.
- 추가 재현 `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_zip_crash_review.py -q --tb=short` → **2 failed, 1.79초**. 두 경우 모두 실제 결과 (0, complete), 기대 (1, degraded).
- 실제 운영 이미지/카드사망/원격 WebDAV/백업 및 실데이터 대조는 수행하지 않았다. 위 재현은 임시 SQLite·mock HTTP/게시 transport이며, 실제 OS kill 시험은 아니다.

Executor는 A1을 수정한 후 FIX_09_REPORT.md를 새로 작성한다. 기존 handoff는 덮어쓰지 않는다. 관련 lifecycle 회귀·Worker 전체 및 공통 계약 변경 시 관련 전체 suite와 ruff/diff 검증을 수행하고 실제/미수행을 구분한다. 운영 배포 완료나 live 정상화로 기록하지 않는다.

## 부록: 독립 재현

아래를 `/tmp/test_zip_crash_review.py`로 저장한다. 첫 검사는 finish_run에 BaseException을 주입해 일반 예외 처리/완료 정리를 건너뛰며 실제 누락 상태를 만든다. 두 번째는 일반 run에 결과 파일이 없다는 경계를 검증한다.

```python
import json
from dataclasses import replace
import pytest
from test_partial_execution import corpus
from test_pipeline_v5 import _Adapter
from test_017_scope import _activate_remote, _make_snapshot
async def test_publication_only_resume_before_execution_result_written(corpus, monkeypatch):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
    from cardrag_worker.pipeline import SealedPublicationResumer, WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    card_pdf = replace(
        base_rec,
        product_code="CARD_COLLECT_PDF",
        source_post_id="post_col_pdf",
        file_name="doc.pdf",
        source_url="https://cards.example/doc.pdf",
    )
    card_zip = replace(
        base_rec,
        product_code="CARD_COLLECT_ZIP",
        source_post_id="post_col_zip",
        file_name="doc.zip",
        source_url="https://cards.example/doc.zip",
        metadata={"attachment_type": "zip"},
    )

    adapter = _Adapter(card_pdf)

    async def mock_discover(client):
        return _make_snapshot(adapter, [card_pdf, card_zip])

    monkeypatch.setattr(adapter, "discover_current", mock_discover)

    # Phase 1: Collect
    collect_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(phase="collect"),
    )
    collect_res = await collect_pipe.run()
    assert collect_res.status == "collected"
    assert collect_res.unsupported_attachment_count == 1
    assert collect_res.collection_status == "degraded"

    # Verify acquisition.json
    acq_path = prior.root / "runs" / collect_res.run_id / "acquisition.json"
    assert acq_path.exists()
    acq_data = json.loads(acq_path.read_text())
    assert len(acq_data["unsupported_attachments"]) == 1
    assert acq_data["unsupported_attachments"][0]["product_code"] == "CARD_COLLECT_ZIP"
    assert acq_data["unsupported_attachments"][0]["format"] == "zip"

    # Phase 2: Process using AcquisitionSource
    acq_source = AcquisitionSource(prior.root, collect_res.run_id, state.path)
    assert len(acq_source.unsupported_attachments) == 1

    process_pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
        execution_plan=ExecutionPlan(frozenset({"pdf"}), collect_res.run_id, phase="process"),
        reuse_source=acq_source,
    )

    class ProcessExit(BaseException):
        pass
    original_finish = state.finish_run
    def interrupt_finish(run_id, status, **kwargs):
        if status == 'succeeded':
            raise ProcessExit()
        return original_finish(run_id, status, **kwargs)
    monkeypatch.setattr(state, 'finish_run', interrupt_finish)
    with pytest.raises(ProcessExit):
        await process_pipe.run()
    monkeypatch.setattr(state, 'finish_run', original_finish)
    run_id = state.connection.execute('SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1').fetchone()[0]
    assert state.run_status(run_id) == 'running'
    assert not (prior.root / 'runs' / run_id / 'execution-result.json').exists()
    from cardrag_core import GenerationManifest
    from types import SimpleNamespace
    seal = json.loads((prior.root / 'runs' / run_id / 'sealed/publish.json').read_text())
    manifest = GenerationManifest.model_validate_json(json.dumps(seal['manifest']))
    assert json.loads(dav.objects[str(dav.pointer_path)])['generation_id'] == manifest.generation_id
    _activate_remote(dav, SimpleNamespace(manifest=manifest))
    resumer = SealedPublicationResumer(state=state, state_dir=prior.root, webdav=dav)
    result = await resumer._resume_publication_locked(run_id)
    assert (result.unsupported_attachment_count, result.collection_status) == (1, 'degraded')



async def test_normal_run_publication_resume_keeps_zip_error(corpus, monkeypatch):
    import zipfile

    from cardrag_worker.pipeline import WorkerPipeline

    def forbidden_zipfile(*args, **kwargs):
        raise AssertionError("zipfile was invoked! ZIP archive must NEVER be unzipped.")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_zipfile)

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    runner = factory()
    base_adapter = runner.adapters[0]
    base_rec = base_adapter.record

    # Card 1: Normal PDF
    card1 = replace(
        base_rec,
        product_code="CARD_NORMAL",
        source_post_id="post_normal",
        file_name="normal.pdf",
        source_url="https://cards.example/normal.pdf",
    )
    # Card 2: Explicit ZIP
    card2 = replace(
        base_rec,
        product_code="CARD_ZIP",
        source_post_id="post_zip",
        file_name="manual.zip",
        source_url="https://cards.example/manual.zip",
        metadata={"attachment_type": "zip"},
    )

    mixed_adapter = _Adapter(card1)
    snapshot_records = [card1, card2]

    async def mock_discover(client):
        return _make_snapshot(mixed_adapter, snapshot_records)

    monkeypatch.setattr(mixed_adapter, "discover_current", mock_discover)

    pipe = WorkerPipeline(
        state=state,
        state_dir=prior.root,
        adapters=[mixed_adapter],
        ocr=runner.ocr,
        embeddings=runner.embeddings,
        webdav=runner.webdav,
        maximum_attempts=1,
    )

    pdf_requests_before = len(pdf_calls)

    result = await pipe.run()

    # Verify Card 2 (ZIP) had 0 body GETs, 0 OCR, 0 embeddings
    zip_gets = [url for url in pdf_calls[pdf_requests_before:] if "manual.zip" in url]
    assert len(zip_gets) == 0, "Explicit ZIP must skip body download"
    assert result.unsupported_attachment_count == 1
    assert result.collection_status == "degraded"
    assert result.status in {"succeeded", "no_change"}

    # Card 1 (PDF) succeeded and is published in manifest
    current_remote = await dav.validated_current_generation()
    assert current_remote is not None
    assert result.document_count == 1
    from cardrag_worker.partial_execution import ReuseSource
    from cardrag_worker.pipeline import SealedPublicationResumer
    published = ReuseSource(prior.root, result.run_id, state.path)
    _activate_remote(dav, published)
    assert not (prior.root / 'runs' / result.run_id / 'execution-result.json').exists()
    state.finish_run(result.run_id, 'interrupted', error='review: publication acknowledgement lost')
    resumer = SealedPublicationResumer(state=state, state_dir=prior.root, webdav=dav)
    resumed = await resumer._resume_publication_locked(result.run_id)
    assert (resumed.unsupported_attachment_count, resumed.collection_status) == (1, 'degraded')


```

## Worker 전체 재검증

Reviewer 직접 실행 `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` → **1,368 passed, 10 warnings, 50.40초**. 기존 OCR cache/fallback 경고다. 이번에는 전체 workspace suite와 gitleaks는 재실행하지 않았다. 위 독립 재현 2건은 기존 suite 밖에서 실패했다.
