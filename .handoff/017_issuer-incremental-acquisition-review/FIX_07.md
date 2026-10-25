# FIX_07 — ZIP 공통 처리의 실제 adapter 적용·오류 상태 계승·재확인 보완

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 미커밋 017/FIX_06 구현.

## 판정과 검토 범위

**수정 필요 3건(P2).** ZIP 자체를 오류로 보고 열지 않으며 정상 sibling을 계속 처리하는 기본 경로는 확인했다. 그러나 실제 adapter의 공통 적용, collect→process 오류 상태 계승, 명시 ZIP의 주기적 재확인은 미완료다. FIX_06_REPORT의 “all requirements fully implemented”는 아래 보완 전 인수하지 않는다.

PLAN/ANALYSIS/REPORT와 FIX_01–06 및 존재하는 각 REPORT, 기존 REVIEW.md, 현재 Git status/diff와 변경 코드·시험을 검토했다. FIX_05의 ZIP 해제 요구는 철회 상태이며 본 수정에서도 되살리지 않는다. 기존 REVIEW.md 승인은 ZIP 정책 추가 전 범위의 이력이다. 이번 Reviewer는 제품 코드·정식 테스트·운영 설정·배포를 변경하지 않았고 실제 origin을 호출하지 않았다.

## A1. [P2] 공통 정책에 도달하기 전 ZIP을 거부/누락하는 실제 adapter 수정

위치: `issuers/hyundai.py:148-149`, `issuers/woori.py:114-115`; 관련 `issuers/kb.py:94` 등 실제 discovery 경로.

- 현대의 정상 상세 payload에서 최신 첨부 이름만 changed.zip으로 바꾸면 parse_detail이 IssuerMarkupChanged를 발생시킨다. 기존 discovery 오류 경로로 이어져 ZIP 한 상품 때문에 issuer 전체 수집이 실패할 수 있다.
- 우리 상세 payload의 파일명/경로가 ZIP이면 parse_detail_records가 그 행을 조용히 continue하여 빈 결과를 반환한다. 상품이 공통 unsupported 정책과 발견 lineage에 도달하지 못한다. 다른 상품이 충분하면 목록에서 누락될 수 있다.
- KB 목록도 PDF 전용 selector를 유지한다. 이는 정적 확인이며 이번 별도 실행 재현은 현대/우리 두 가지다.
- 기존 cross-issuer ZIP 테스트는 SourceRecord/helper 또는 공통 fake adapter를 사용한다. issuer 문자열을 바꾼 테스트만으로 실제 회사별 discovery의 지원을 입증하지 못한다.

수정: 각 실제 adapter의 첨부 파싱을 조사해 상품/출처 identity가 검증된 ZIP을 관측된 unsupported source로 공통 경로에 전달한다. 정상 PDF identity/출시일/최신 첨부 선택/페이지 완전성/허용 origin 보호는 유지한다. 알 수 없는 markup/schema를 무조건 유효 source로 바꾸지 않는다. ZIP을 관측한 상품은 inventory에 남기고 단종으로 판단하지 않는다. 이를 위해 해당 ZIP을 내려받거나 열 필요는 없다.

인수: 실제 현대/우리/KB 및 나머지 adapter별 ZIP 도달 여부를 검증한다. PDF+ZIP 혼합에서 ZIP 상품이 오류로 남고 정상 상품 수집이 계속되며 issuer 전체 quarantine·조용한 누락이 없다. 신규 파일명/DOCID에 의존하지 않는다. 실제 adapter fixture 회귀를 추가한다.

## A2. [P2] process·재개·운영 출력에 첨부 오류를 계승

위치: `partial_execution.py:replay_pdf_source`의 AcquisitionSource 분기, `acquisition.py:33`, `pipeline.py:2795-2806`; CLI `cli.py:_pipeline_result_payload`.

기존 collect/process 테스트에 최종 오류 상태 assertion을 추가해 재현했다. collect는 ZIP 1건을 acquisition.json에 기록하고 degraded를 반환하지만, process 결과는 **unsupported_attachment_count=0, collection_status=complete**다. AcquisitionSource가 receipt.unsupported_attachments를 읽어 보관할 뿐 replay가 pipeline의 오류 상태/카운터를 복원하지 않아 실행 말미 기본값으로 덮인다. 기존 테스트는 process 성공과 정상 문서 수만 검사해 이를 놓친다.

또한 CLI 결과 payload에는 새 unsupported_attachment_count 항목이 없어, 일반 run에서도 운영자가 첨부 오류 수를 직접 확인할 수 없다. publication-only resumer에도 해당 카운터 기본값 0이 있으므로 자체 seal 재개와도 함께 검증해야 한다. 이 마지막 재개 변형은 이번 별도 재현 결과가 아니라 확인이 필요한 동일 상태 계승 경계다.

수정: 검증된 acquisition receipt 및 publication의 증거/이력에서 오류 identity·이유·수를 계승하여 collect/process/일반 run/no_change/새 객체 resume 결과가 일치하도록 한다. CLI에 첨부 오류 수를 노출하고 오류가 남았는데 complete로 표시하지 않는다. 집계만 맞추고 문서별 오류를 버리지 않는다. PDF 취득/DRM/미취득 ZIP을 구분한다. 새 상태를 seal에 연결할 경우 기존 seal 소비 호환성과 해시 검증을 유지한다.

인수: collect→process에서 같은 오류 source/사유와 count=1/degraded 유지, 원래 run 및 게시 전후 재개의 정상 PDF 수/전체 serving/실패 이력 일치, CLI 출력의 첨부 오류 수 확인. 오류가 정상 PDF 교체로 해소되면 해당 source의 오류만 제거한다. 비선택 문서의 처리를 새로 실행하지 않는다.

## A3. [P2] 명시 ZIP의 영구 제외를 만료되는 재확인으로 변경

위치: `pipeline.py:3914-3928`.

명시 ZIP은 cache/due 검사 이전에 매번 _UnsupportedAttachment를 반환한다. 이 분기는 오류 cache나 audit due를 기록/조회하지 않으며 원본 유형 확인 경로로 들어가지 않는다. 따라서 metadata/URL/파일명이 그대로이고 서버 내용만 정상 PDF로 바뀐 경우 영구히 발견하지 못한다. 이름이 terms.pdf로 바뀌는 기존 테스트와 별개의 문제다.

별도 재현: 명시 ZIP과 정상 PDF를 수집 → pipeline의 시계를 100일 이후로 이동 → 같은 ZIP URL에 정상 PDF를 반환하는 mock origin 유지 → 재실행. 100일 이후에도 해당 원본 요청은 0건이다. FIX_06 §7–8 및 90일 분산 재확인 요구를 충족하지 못한다.

수정: source/fingerprint에 문서 유형 오류·관측 시각·next_check를 영속 기록하고 일반 배치에는 body 요청 0을 유지한다. metadata 변경은 즉시 재검토한다. 최대 90일 안의 분산 due에는 예산 내 원본 유형을 제한적으로 재확인할 수 있게 한다. 여전히 ZIP이면 열거나 멤버를 선택하지 않고 오류를 유지한다. 원본이 실제 정상 PDF로 교체됐으면 strict 검증을 거쳐 복구할 수 있게 한다. 확인하지 않은 원본의 freshness를 갱신하거나 cache hit마다 due를 미루지 않는다. HEAD/Range 지원을 확인하지 않고 의존하지 않는다.

인수: 동일 ZIP 다음 배치 body 요청 0, metadata 변경의 즉시 복구, 동일 URL 원본 교체의 만료 재확인, 재확인 후 ZIP 유지/정상 PDF 복구, 순회 비선택 0 및 budget deferred/overdue를 날짜 시뮬레이션으로 확인한다. 재확인도 archive 해제 0이다.

## 검증 근거

- Reviewer 직접 실행: `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q --tb=short` → **42 passed, 7.62초**.
- `uv run --no-sync ruff check apps/cardrag-worker/` → 통과.
- `uv run --no-sync ruff format --check apps/cardrag-worker/` → 123 files already formatted.
- `git diff --check` → 통과.
- 추가 정상 기대값 재현: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_zip_review.py -q --tb=short` → **4 failed, 1.75초**: A2 1건, A3 1건, A1 현대/우리 각 1건. 기존 suite에 없는 경계다. 아래에 동일 코드를 보존한다.
- 실제 운영 이미지·카드사 origin·운영 데이터/백업 장애·90일 실시간 평가는 수행하지 않았다. 앞선 ZIP 단건 조사는 사실 근거이며 이번 리뷰에서 반복 다운로드하지 않았다.

다음 Executor는 FIX_07을 수정하고 FIX_07_REPORT.md를 새로 작성한다. 기존 문서는 덮어쓰지 않는다. 관련 실제 adapter 회귀 및 Worker/공통 계약 영향 범위의 전체 suite와 ruff/diff 검증을 수행한다. 코드·운영 검증을 구분하며 구현 완료 전 운영 정상화로 표시하지 않는다. ZIP 자체 오류·압축 해제 금지 방침을 유지한다.

## 부록: 독립 재현

아래를 `/tmp/test_zip_review.py`에 저장하고 위 명령으로 실행한다. HTTP/OCR/게시 transport는 기존 테스트 fixture이며 실제 원본이나 운영 state를 사용하지 않는다.

```python
import json
from dataclasses import replace
import pytest
from test_partial_execution import corpus
from test_pipeline_v5 import _Adapter
from test_017_scope import _activate_remote, _make_snapshot
async def test_process_retains_zip_error_status(corpus, monkeypatch):
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



async def test_explicit_zip_rechecked_after_audit_window(corpus, monkeypatch):
    from datetime import datetime, UTC, timedelta
    import cardrag_worker.pipeline as module
    from cardrag_worker.pipeline import WorkerPipeline
    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    _activate_remote(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    zipped = replace(base, product_code="zip-later-pdf", source_post_id="zip-later-pdf", file_name="same.zip", source_url="https://cards.example/same.zip", metadata={"attachment_type":"zip"})
    adapter = _Adapter(base)
    async def discover(client):
        return _make_snapshot(adapter, [base, zipped])
    monkeypatch.setattr(adapter, "discover_current", discover)
    def runner():
        return WorkerPipeline(state=state, state_dir=prior.root, adapters=[adapter], ocr=ocr, embeddings=initial.embeddings, webdav=dav, maximum_attempts=1)
    await runner().run()
    before = len(pdf_calls)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=100)
    monkeypatch.setattr(module, "datetime", Later)
    # Origin mock now serves a valid PDF even at the unchanged .zip URL.
    await runner().run()
    assert any("same.zip" in url for url in pdf_calls[before:]), "No bounded origin recheck even after 100 days"


def test_real_hyundai_zip_reaches_common_policy():
    from test_issuers_hyundai_hana import PRODUCT, NOW, attachment, hyundai_payload
    from cardrag_worker.issuers.hyundai import parse_detail
    result = parse_detail(hyundai_payload(attachment('changed.zip')), product=PRODUCT, discovered_at=NOW)
    assert result.is_zip


def test_real_woori_zip_reaches_common_policy():
    from datetime import datetime, UTC
    from cardrag_worker.issuers.woori import parse_detail_records
    result = parse_detail_records({'filePath':'/upload/changed.zip', 'fileNm':'changed.zip', 'beginDt':'20261001', 'gdccVer':'v1'}, product_code='P1', product_name='Card', discovered_at=datetime.now(UTC))
    assert len(result) == 1 and result[0].is_zip

```

## 전체 workspace 재검증

Reviewer 직접 실행 `uv run --no-sync pytest -q --tb=short` → **2,592 passed, 10 warnings, 77.81초**. 기존 OCR fallback/native cache 경고다. 위 추가 재현 4건은 기존 suite 밖에서 확인한 미충족 요구이며, 기존 suite 통과만으로 해소되지 않는다.
