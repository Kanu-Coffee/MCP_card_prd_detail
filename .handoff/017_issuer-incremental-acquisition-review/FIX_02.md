# FIX_02 — 실행 범위 복구·설정 축소·OCR 큐 경계 재검토

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`의 현재 미커밋 017 구현과 FIX_01 반영분.

## 판정

**수정 필요.** 기존 Worker 테스트는 통과하지만 아래 네 경계 조건에서 PLAN의 재개·전체 serving 보존·OCR 요청 격리 인수 기준을 충족하지 않는다. 모두 P2이며 이 수정 라운드를 마친 뒤 재리뷰한다. 현재까지 데이터 삭제나 비선택 OCR provider 실호출이 발생했다고 판정한 것은 아니다.

PLAN.md, ANALYSIS.md, REPORT.md, FIX_01.md, FIX_01_REPORT.md, 현재 Git status/diff 및 신규 모듈·관련 테스트를 확인했다. FIX_01의 예산 계산, transient 재시도, 비DRM negative cache 처리와 문서 보완은 현재 코드에서 확인했다. 이미 보고된 수정을 다시 요구하지 않는다. 이번 리뷰는 제품 코드·운영 설정·서비스·배포를 변경하지 않았다.

## A. 필수 수정

### A1. [P2] process의 추론된 선택 범위를 정규화한 뒤 resume 계획을 비교할 것

위치: `apps/cardrag-worker/src/cardrag_worker/partial_cli.py:32-56`, `cli.py:1951-1953` 부근.

- 최초 `run --phase process --reuse-from-run COLLECTION`은 issuer를 생략하면 acquisition receipt에서 선택 범위를 추론하고, 그 tuple을 execution-plan.json에 저장한다.
- 같은 명령에 `--resume PROCESS_RUN`만 추가하면 CLI는 phase가 명시적으로 process일 때 저장된 selected_issuers를 복원하지 않는다. run_partial은 receipt에서 범위를 추론하기 **전** selected_issuers=None인 plan을 저장 계획과 비교한다.
- 재현 결과: 원래와 동일한 요청인데 `skip_artifact_incompatible`, stage=`plan`으로 종료한다. bare `run --resume PROCESS_RUN`이 동작하는 것과 별개의 CLI 결함이다.

수정: 생략한 인자는 저장된 실행 계약/검증된 receipt로 정규화하고 동일성 검사한다. 명시적으로 다른 issuer/source/channel/phase를 준 경우는 계속 origin/provider 초기화 전에 거부한다. process뿐 아니라 collect의 생략·명시 재개 조합도 확인한다. 단순히 비교를 제거해서 scope 변경을 허용하지 않는다.

인수: issuer 생략 최초 process → 중단 → 원래 명령+resume, bare resume, 명시 동일 issuer 재개가 같은 run/source/scope를 사용한다. 다른 범위는 사전 거부한다.

### A2. [P2] 당일 자동 순회 재개도 저장된 pool과 scope를 복원할 것

위치: `apps/cardrag-worker/src/cardrag_worker/rotation.py:65-77,218-227`.

- Rotation.claim은 같은 날 기존 run_id를 반환하면서 IssuerScope.pool은 현재 설정으로 다시 만든다.
- 저장된 issuer-scope.json을 복원하는 코드는 explicit resume에만 적용된다. 자동 재개는 resume=None이므로 빠진다.
- 미완료 slot을 (`shinhan`, `lotte`)로 만들고 같은 날 pool에 `kb`를 추가해 자동 재개하면 같은 run_id라도 freeze_scope에서 `issuer_scope_changed`가 발생한다. 실제 CLI는 provider 초기화 후 pipeline에 도달하므로 범위 불일치가 조기에 걸러지는 것도 아니다.

수정: 바인딩된 미완료 day slot의 자동 재개는 persisted scope를 provider 초기화 전에 읽고 검증한다. 명시 재개와 같은 복원 함수를 재사용한다. 현재 pool 변경의 다음 cycle 반영·제거 정책과 저장 scope의 계속 사용/명시 중단 정책을 구분하고, 선택 issuer가 그대로인 단순 pool 추가로 당일 작업을 terminal failure 처리하지 않는다.

인수: 같은 날 중단 후 비선택 issuer 추가/제거에도 같은 run과 원래 scope·stage attempt·누적 HTTP 예산을 유지한다. 같은 날 다른 issuer 추가 배정은 0건이다. 선택 issuer 자체 제거 시 정책도 회귀 테스트로 고정한다.

### A3. [P2] process에서도 configured pool 밖 prior issuer의 source를 보존할 것

위치: `apps/cardrag-worker/src/cardrag_worker/partial_execution.py:427-430`, `issuer_scope.py:114-116,172-174`. 비교: 일반 pipeline의 `pipeline.py:3553-3558`.

- 일반 수집 경로는 현재 serving의 issuer가 configured adapters에 없으면 보존용 adapter를 추가한 뒤 source snapshot을 복원한다.
- process replay는 그 절차 없이 preserve_inputs를 호출한다. 이 함수는 pipeline.adapters에 있는 issuer의 snapshot만 읽는다.
- 2사 정상 baseline → 한 카드사 collect receipt → 비선택 카드를 configured pool에서 제외 → 동일 baseline에 process를 재현했다. receipt/PDF/기준선이 정상이어도 비선택 source를 못 찾아 `issuer_scope_source_missing`으로 종료했다.
- 기존 serving을 삭제하지는 않았지만 정상 process 게시가 막힌다. REPORT의 configured pool에서 빠진 issuer도 유지한다는 보장은 이 경로에서 충족하지 못한다.

수정: publication 보존 범위를 work scope와 분리해 일반 수집/process 양쪽에서 같은 prior source 복원 절차를 사용한다. 보존용 issuer 추가가 selected work scope나 원래 scope receipt를 확대하지 않도록 한다. 비선택 adapter HTTP, OCR, 구조/view 생성, embedding은 0건이어야 한다.

인수: 8사 baseline에서 설정을 1사로 축소한 상태로 collect/process를 완료하고, 나머지 7사의 현재·과거 개정/실패/DRM/vector 참조 및 전체 issuer_codes를 보존한다. source/참조가 실제 손상된 경우의 fail closed는 유지한다.

### A4. [P2] OCR 요청 범위 제한을 selected == configured pool인 경우에도 적용할 것

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:2596-2622`.

- eligible_document_ids 계산과 mixed 요청 분할이 selected_issuers와 현재 adapters의 집합이 다를 때만 실행된다.
- 설정 pool을 줄여 그 안의 전체 issuer를 실행해도 retained serving/전역 OCR 큐에는 pool 밖 issuer가 남을 수 있다. 이 경우 eligible=None이 전달돼 비선택 요청까지 현재 run에 영속 배정된다.
- retained testbank 문서의 pending 요청을 두고 configured/selected pool을 otherbank 한 곳으로 구성한 실제 pipeline 선택 경로를 검사했다. 수집 진입 직전 `_active_ocr_request`가 비선택 testbank 요청이었다.
- **확인 범위:** 잘못된 요청 배정까지 재현했다. 비선택 OCR provider 호출이나 완료 처리까지 발생했다고 주장하지 않는다. process replay에는 receipt에 없는 target을 거부하는 검사도 있어 잘못 배정된 요청이 해당 처리를 중단시키는 경로가 있다. 선택 요청이 먼저 처리되지 않는 위험도 남는다.

수정: 보존된 issuer와 전역 queue를 고려해 모든 실행의 selected scope로 eligible을 계산하고 immutable mixed parent/child 정책을 적용한다. configured pool 전체라는 사실을 전역 queue 전체 처리 허가로 해석하지 않는다. unknown/미선택 target은 pending으로 보존한다. 이미 저장된 잘못된 selection의 명시적 복구 정책도 정한다.

인수: pool 밖 pending 요청이 큐 앞에 있어도 선택 요청을 수행하고 비선택 selection/provider/proof/completion은 0건이다. 혼합 parent는 선택 child만 처리하며 전체/선택/process/resume 모두 같은 범위 규칙을 사용한다.

## B. 검증 근거와 다음 Executor 요구

- 직접 재실행: `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` → **1,335 passed, 10 warnings, 44.55초**. 기존 OCR cache/fallback 경고다.
- `git diff --check` → 통과.
- 추가 임시 재현: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_017_reviewer.py -q --tb=short` → **4 failed**. 기존 suite에 없는 경계를 정상 기대값으로 검사했으며 각각 위 A1–A4에 대응한다. 아래 코드를 임시 파일로 저장하면 재현 가능하다. 실제 origin/provider/운영 state는 사용하지 않는다.
- 다음 Executor는 재현을 정식 회귀 테스트로 정리하고, A1–A4 수정 후 관련 suite·Worker 전체·ruff/diff 검증을 수행한다. `FIX_02_REPORT.md`에 변경·명령·결과·미검증을 기록한다. 기존 문서는 덮어쓰지 않는다.
- 운영 이미지/네트워크의 신규 코드 실행, 8사 실데이터 소비 대조, 실제 proxy 304/redirect 검증은 이번 리뷰에서 수행하지 않았다. 테스트 통과를 운영 인수 완료로 확대하지 않는다.

## C. 기존 미해결 사항과 범위

롯데의 정상 PDF 다운로드 성공과 ZIP 포함 complete 목록 파싱 실패는 별개다. REPORT 및 FIX_01_REPORT의 DOCID 1893 ZIP 문제는 해결된 것으로 판정하지 않으며 기존 보존·실패 승계 조건을 유지한다. 이번에 origin 호출을 반복하지 않았다. 실제 proxy/VPN/SSH 값이나 키를 추가하지 않았다. 디스크 정리·배포·운영 순회 활성화는 이번 리뷰 범위가 아니다.

## 부록: 재현 코드

아래를 `/tmp/test_017_reviewer.py`로 저장하고 B의 명령으로 실행한다. A4는 수집 전 요청 배정을 관찰한 뒤 의도적으로 중단하므로 외부 처리를 하지 않는다.

```python
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from test_partial_execution import corpus  # noqa
from test_pipeline_v5 import _Adapter

from cardrag_worker.contracts import canonical_json_bytes
from cardrag_worker.issuer_scope import freeze_scope
from cardrag_worker.partial_execution import ExecutionPlan, ReuseSource
from cardrag_worker.rotation import scope_session
from cardrag_worker.state import WorkerState
from cardrag_worker.webdav import RemoteGenerationIdentity


def activate(dav, source):
    m = source.manifest
    dav.current = RemoteGenerationIdentity(m.generation_id, m.corpus_sha256, m.contract_sha256, m.schema_version, m.serving_schema)


async def test_automatic_resume_keeps_original_roster(tmp_path):
    with WorkerState(tmp_path / 'state.sqlite3') as state:
        opts = dict(selected=None, mode='round_robin', channel='stable', resume=None)
        async with scope_session(state, tmp_path, ('shinhan', 'lotte'), **opts) as (assignment, bind):
            run_id = state.start_run()
            bind(run_id)
            freeze_scope(tmp_path, run_id, assignment.scope)
        async with scope_session(state, tmp_path, ('shinhan', 'lotte', 'kb'), **opts) as (assignment, bind):
            assert assignment.run_id == run_id
            freeze_scope(tmp_path, run_id, assignment.scope)


def test_process_explicit_resume_restores_inferred_scope(tmp_path, monkeypatch):
    from cardrag_worker import cli, partial_cli
    from cardrag_worker.settings import WorkerSettings
    monkeypatch.setenv('CARDRAG_ENABLED_ISSUERS', 'shinhan')
    monkeypatch.delenv('CARDRAG_ISSUER_EXECUTION_MODE', raising=False)
    monkeypatch.setattr(WorkerSettings, 'from_env', lambda **kw: SimpleNamespace(state_dir=tmp_path, channel='stable', lock_file=tmp_path/'lock'))
    saved = ExecutionPlan(frozenset({'pdf'}), 'collection-1', channel='stable', phase='process', selected_issuers=('shinhan',))
    directory = tmp_path/'runs'/'process-1'
    directory.mkdir(parents=True)
    (directory/'execution-plan.json').write_bytes(canonical_json_bytes(saved.identity()))
    actual = partial_cli.run_partial
    # The real preflight must accept the same command that originally inferred its scope.
    async def invoke(plan, resume):
        return await actual(plan, resume)
    monkeypatch.setattr(partial_cli, 'run_partial', invoke)
    result = CliRunner().invoke(cli.app, ['run', '--phase', 'process', '--reuse-from-run', 'collection-1', '--resume', 'process-1'])
    assert '"stage": "plan"' not in result.output, result.output


async def test_process_preserves_issuer_removed_from_pool(corpus):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.pipeline import WorkerPipeline
    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    activate(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(replace(base, issuer='otherbank', product_code='other', source_post_id='other', source_url='https://cards.example/other.pdf'))
    other.spec = replace(other.spec, code='otherbank', sort_order=2)
    def runner(adapters, plan=None, source=None, selected=('testbank',)):
        return WorkerPipeline(state=state, state_dir=prior.root, adapters=adapters, selected_issuers=selected, ocr=ocr, embeddings=initial.embeddings, webdav=dav, maximum_attempts=1, retry_cap_seconds=0, execution_plan=plan, reuse_source=source)
    full_runner = runner([initial.adapters[0], other], selected=('testbank', 'otherbank'))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    activate(dav, baseline)
    collected = await runner([initial.adapters[0], other], ExecutionPlan(phase='collect', selected_issuers=('testbank',))).run()
    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    plan = ExecutionPlan(frozenset({'pdf'}), collected.run_id, phase='process', selected_issuers=('testbank',))
    result = await runner([initial.adapters[0]], plan, source).run()
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {'testbank', 'otherbank'}


async def test_ocr_queue_excludes_retained_issuer_outside_configured_pool(corpus, monkeypatch):
    from cardrag_worker.ocr_requests import OCRReprocessRequest, OCRRequestTarget, queue_reprocess_requests
    from datetime import UTC, datetime
    factory, baseline, state, _, _, _, _ = corpus
    runner = factory()
    old = next(iter(baseline.documents.values()))
    request = OCRReprocessRequest(request_id='ocr-foreign', source_generation_id=baseline.manifest.generation_id, created_at=datetime.now(UTC), targets=(OCRRequestTarget(document_id=old.document_id, pdf_sha256=old.pdf.sha256, pdf_size_bytes=old.pdf.size_bytes, page_count=old.page_count),))
    queue_reprocess_requests(baseline.root, (request,))
    # Configured work pool changes, but the old issuer remains in the retained serving generation.
    new_adapter = _Adapter(replace(runner.adapters[0].record, issuer='otherbank'))
    new_adapter.spec = replace(new_adapter.spec, code='otherbank')
    from cardrag_worker.issuer_scope import IssuerScope
    from cardrag_worker.pipeline import WorkerPipeline
    runner = WorkerPipeline(state=state, state_dir=baseline.root, adapters=[new_adapter], ocr=runner.ocr, embeddings=runner.embeddings, webdav=runner.webdav, maximum_attempts=1)
    captured = []
    async def inspect_selection(*args, **kwargs):
        captured.append(runner._active_ocr_request)
        from cardrag_worker.partial_execution import PartialExecutionError
        raise PartialExecutionError('review_selection_observed', 'ocr')
    monkeypatch.setattr(runner, '_run_locked', inspect_selection)
    from cardrag_worker.partial_execution import PartialExecutionError
    with pytest.raises(PartialExecutionError, match='review_selection_observed'):
        await runner.run()
    assert captured == [None], 'nonselected OCR request was bound before collection'

```
