# FIX_08 — ZIP 오류의 재개 계승·복구 유지·실제 재확인 준비 경로

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 현재 미커밋 017/FIX_07 구현.

## 판정과 범위

**수정 필요 3건(P2).** FIX_07의 실제 adapter ZIP 목록 관측, 최초 collect→process 오류 수, CLI 노출, 만료 시 원본 호출은 기본 회귀에서 개선됐다. 그러나 명시 요구한 재개와 복구 유지·실제 다운로드 준비 경계가 아래와 같이 미완료다. ZIP을 열지 않는 사용자 방침과 FIX_05 철회는 유지한다.

PLAN/ANALYSIS/REPORT, FIX_01–07 및 존재하는 REPORT, REVIEW.md, 현재 Git status/diff, 실제 adapter와 pipeline/partial 경로·회귀 테스트를 확인했다. 이번에는 제품 코드·정식 테스트·운영 상태를 수정하지 않았다. 새 실제 카드사 요청도 수행하지 않았다. 아래 재현은 mock fixture와 임시 SQLite/파일을 이용했다.

## A1. [P2] 자체 seal 재개에서도 ZIP 오류 identity·count/degraded를 복원

위치: `pipeline.py:3562-3575`의 retained seal 우선 분기, `partial_execution.py:414` 이하의 오류 복원, `pipeline.py:2859-2865` execution-result 기록.

**재현:** 정상 PDF+ZIP을 collect하고 process를 완료하면 count=1/degraded다. 해당 generation을 remote current로 놓고 로컬 run 상태를 interrupted로 만들어 게시 완료 후 로컬 완료 기록 누락을 모사한다. 새 WorkerPipeline 객체로 같은 process run을 재개하면 **count=0/complete**로 바뀐다.

FIX_07은 replay_pdf_source에만 복원을 넣었다. 자체 seal이 있는 재개는 그 함수를 호출하지 않고 바로 _publish_sealed로 가므로 default 0을 사용한다. 일반 ReuseSource replay에 추가한 execution-result.json 조회도 해결 근거가 되지 못한다. 현재 execution-result writer가 unsupported_attachment_count를 직렬화하지 않아 reader의 get(...,0)은 여전히 0이다. 이미 보고된 오류가 복구 중 사라져 정상 수집으로 표시되는 문제다. 이번 재현에서 serving 삭제는 확인되지 않았다.

수정: 첫 publish 전에 검증 가능한 source별 첨부 오류 증거·count를 durable하게 고정하고, 자체 seal/게시 전후 재개와 publication-only resumer가 동일 상태를 복원하게 한다. 오류별 이력은 보존하되 이미 정상 PDF로 해소된 source는 제외해야 한다. execution-result를 사용하는 경우 writer/reader 계약을 함께 맞추고, 파일이 아직 생성되지 않은 crash 시점도 처리한다. _publish_sealed 우선 처리와 seal/contract/hash 검증·최신 pointer 보호를 유지한다.

인수: 최초 process, pointer 전/후 중단, 새 객체로 같은 run 재개, publication-only 재개, frozen source replay에서 동일 ZIP error identity/사유/count=1/degraded 유지. 정상 문서/전체 serving 보존 및 추가 origin/OCR/embedding 호출 0. CLI의 새 오류 수에도 같은 값이 노출돼야 한다.

## A2. [P2] 같은 URL에서 정상 PDF로 복구된 문서의 상태를 다음 배치에도 유지

위치: `pipeline.py:3914-3951` 명시 ZIP 조기 분기와 원본 검증 후 negative cache 해제 경계.

**재현:** 명시 same.zip source를 처음 관측 → 100일 후 해당 URL이 정상 PDF를 반환 → due 재확인에서 strict PDF 취득 성공, ZIP 오류 0. 그 결과를 current로 놓고 같은 metadata의 다음 배치를 실행하면 **오류 1/degraded로 되돌아간다.**

성공 취득 시 origin_negative가 삭제되는데, 다음 명시 ZIP 분기는 negative도 이전 negative row도 없으므로 이를 최초 ZIP 관측으로 해석하고 다시 90일 negative를 만든다. 실제로 검증한 positive PDF cache/binding을 먼저 보지 않는다. 복구 사실을 유지하지 못하고 잘못된 오류를 반복 생성한다.

수정: URL/파일명의 ZIP 표시는 최초 분류 근거로 사용하되 이미 해당 identity의 원본을 strict PDF로 검증한 사실을 보존·우선 적용한다. 정상 cache의 사용과 다음 원본 감사 due는 일반 PDF 정책으로 이어가고, metadata 변경·로컬 손상·원본이 다시 ZIP이 된 경우만 실제 근거에 따라 재분류한다. 확장자만 보고 영구 정상 허용하거나 PDF 검증을 약화하지 않는다. 실제 ZIP은 계속 unsupported이며 열지 않는다.

인수: ZIP → 같은 URL의 검증된 PDF → 다음 날/다음 선택일 배치에서도 오류 0/정상 자료 보존. 정상 cache·due 전이면 추가 원본 body 요청 0. 이후 실제 ZIP 재등장, metadata 변경, cache 손상, 감사 만료도 확인한다. 복구 후 다음 배치까지 연속 실행하는 테스트를 정식으로 추가한다.

## A3. [P2] 우리카드의 만료 ZIP 재확인이 실제 prepare_download에서 거부됨

위치: `issuers/woori.py:141-145,250-257`.

FIX_07의 우리카드 회귀는 parse_detail_records가 ZIP SourceRecord를 만든다는 것만 검사한다. 하지만 negative 만료 후 acquire가 실제 WooriAdapter.prepare_download를 호출하면 raonk_plaintext가 여전히 .pdf만 허용하여 **ValueError('invalid RAONK file name')**를 발생시킨다. 정상적인 ZIP source로 재현했다. ZIP 첫 관측의 skip은 통과하지만 90일 이후 실제 원본 유형 확인을 할 수 없다. 공통 acquisition의 오류 분류를 통해 issuer-level 실패/quarantine으로 전파될 가능성도 있다. 이번 직접 재현은 실제 prepare 단계의 ValueError까지이며 issuer 전체 취소 자체를 별도로 주입한 것은 아니다.

수정: 실제 adapter handshake/다운로드 준비 경로에서도 검증된 source ZIP의 제한된 원본 유형 재확인이 가능하도록 조사·보완한다. 허용 origin/path/파일명 및 RAONK 인자 보호를 유지한다. guard를 삭제하거나 이 ValueError를 모두 무시하지 않는다. 원본이 여전히 ZIP이면 문서 오류로 격리하며 sibling은 계속한다. 다른 adapter의 prepare identity/확장자 제약도 같은 경계를 점검한다.

인수: 실제 우리 adapter를 사용해 metadata ZIP 최초 skip → 만료 → prepare/제한된 취득 → ZIP 유지 또는 정상 PDF 교체를 확인하고, 정상 sibling이 중단되지 않아야 한다. no-unzip spy와 request/byte 예산 유지. 현재 fake _Adapter만 사용하는 100일 검사를 실제 adapter handshake 회귀로 보강한다.

## Reviewer 직접 검증

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q --tb=short` → **52 passed, 8.04초**.
- `uv run --no-sync ruff check apps/cardrag-worker/` → 통과.
- `uv run --no-sync ruff format --check apps/cardrag-worker/` → 123 files already formatted.
- `git diff --check` → 통과.
- 추가 재현 `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_zip_second_review.py -q --tb=short` → **3 failed, 1.92초**. 위 A1–A3를 정상 기대값으로 검사한 결과다. 코드 부록 참조.
- 실제 운영 이미지·원격 카드사/WebDAV·8사 실데이터·백업 장애·90일 실시간 검증은 수행하지 않았다. 테스트 통과를 ZIP 운영 정상화 완료로 확대하지 않는다.

다음 Executor는 위 세 항목을 수정하고 FIX_08_REPORT.md를 새로 작성한다. 기존 handoff는 덮어쓰지 않는다. 이전 FIX_07 기본 회귀와 신규 lifecycle/실제 adapter 검증, Worker 및 공통 계약 영향 전체 suite, ruff/diff를 수행한다. 이번 단계에서도 ZIP 해제·내부 PDF 선택·특정 카드/DOCID/파일명 예외를 도입하지 않는다.

## 부록: 독립 재현 코드

아래를 `/tmp/test_zip_second_review.py`로 저장한다. source/URL은 테스트용이며 실제 origin·운영 state를 사용하지 않는다.

```python
import json
from dataclasses import replace
from datetime import datetime, UTC, timedelta
import pytest
from test_partial_execution import corpus
from test_pipeline_v5 import _Adapter
from test_017_scope import _activate_remote, _make_snapshot
async def test_zip_error_survives_own_seal_resume(corpus, monkeypatch):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.partial_execution import ExecutionPlan
    from cardrag_worker.pipeline import WorkerPipeline

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
    process_res = await process_pipe.run()
    assert process_res.status == "succeeded"
    assert process_res.document_count == 1
    assert (process_res.unsupported_attachment_count, process_res.collection_status) == (1, "degraded")
    from cardrag_worker.partial_execution import ReuseSource
    published = ReuseSource(prior.root, process_res.run_id, state.path)
    _activate_remote(dav, published)
    state.finish_run(process_res.run_id, 'interrupted', error='review: committed before local completion')
    resumed = WorkerPipeline(state=state, state_dir=prior.root, adapters=[adapter], ocr=runner.ocr, embeddings=runner.embeddings, webdav=dav, maximum_attempts=1, execution_plan=ExecutionPlan(frozenset({'pdf'}), collect_res.run_id, phase='process'), reuse_source=acq_source)
    result = await resumed.run(resume_run_id=process_res.run_id)
    assert (result.unsupported_attachment_count, result.collection_status) == (1, 'degraded')


async def test_recovered_pdf_does_not_revert_to_zip_error(corpus, monkeypatch):
    import cardrag_worker.pipeline as module
    from cardrag_worker.pipeline import WorkerPipeline

    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    zipped = replace(
        base,
        product_code="zip-later-pdf",
        source_post_id="zip-later-pdf",
        file_name="same.zip",
        source_url="https://cards.example/same.zip",
        metadata={"attachment_type": "zip"},
    )
    adapter = _Adapter(base)

    async def discover(client):
        return _make_snapshot(adapter, [base, zipped])

    monkeypatch.setattr(adapter, "discover_current", discover)

    def runner():
        return WorkerPipeline(
            state=state,
            state_dir=prior.root,
            adapters=[adapter],
            ocr=ocr,
            embeddings=initial.embeddings,
            webdav=dav,
            maximum_attempts=1,
        )

    recovered = await runner().run()
    before = len(pdf_calls)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=100)

    monkeypatch.setattr(module, "datetime", Later)
    # Origin mock now serves a valid PDF even at the unchanged .zip URL.
    recovered = await runner().run()
    assert any("same.zip" in url for url in pdf_calls[before:]), (
        "No bounded origin recheck even after 100 days"
    )
    from cardrag_worker.partial_execution import ReuseSource
    assert recovered.unsupported_attachment_count == 0
    published = ReuseSource(prior.root, recovered.run_id, state.path)
    _activate_remote(dav, published)
    again = await runner().run()
    assert (again.unsupported_attachment_count, again.collection_status) == (0, 'complete')



async def test_woori_zip_recheck_download_preparation():
    from cardrag_worker.issuers.woori import WooriAdapter, parse_detail_records
    record = parse_detail_records({'filePath':'/upload/changed.zip', 'fileNm':'changed.zip', 'beginDt':'20261001', 'gdccVer':'v1'}, product_code='P1', product_name='Card', discovered_at=datetime.now(UTC))[0]
    request = await WooriAdapter().prepare_download(None, record)
    assert request.method == 'POST'

```

## 전체 workspace 재검증

Reviewer 직접 실행 `uv run --no-sync pytest -q --tb=short` → **2,602 passed, 10 warnings, 77.93초**. 기존 OCR fallback/native cache 경고다. 추가 독립 재현 3건은 기존 suite 밖의 경계에서 실패했다.
