# FIX_04 — 게시 완료 후 process 재개의 자체 seal 복구 순서

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 현재 미커밋 017/FIX_03 구현.

## 판정

**수정 필요 1건(P2).** FIX_03에서 지적한 게시 직전 실패의 계약 복원은 통과하고 CLI 재개 성공 검증도 보강됐다. 롯데 HTTP 500의 근거 없는 설명 철회와 complete collection 운영 인수 보류 정정도 확인했다. 그러나 pointer가 이미 해당 process 세대로 전환된 뒤 로컬 완료 기록이 남지 않은 상황에서, 재개가 자체 seal 복구에 도달하지 못한다. FIX_03 A1의 exact committed seal 재개 인수 기준이 아직 충족되지 않는다.

PLAN/ANALYSIS, REPORT, FIX_01–03 및 각 REPORT, 현재 Git status/diff와 변경 함수·회귀 테스트를 확인했다. 이번 리뷰에서는 제품 코드·정식 테스트·운영 설정·서비스·배포를 변경하지 않았다. 별도 실제 origin 호출도 수행하지 않았다. 아래는 mock transport/임시 SQLite 상태를 이용한 재현이다.

## A1. [P2] 이미 게시된 자체 seal 재개를 preservation_source 조회보다 먼저 처리할 것

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:3533-3550`, `issuer_scope.py:77-84`, `partial_execution.py:155-160`.

### 재현 및 결과

1. testbank/otherbank 정상 baseline에서 testbank collect receipt를 생성한다.
2. configured/selected adapters를 testbank 하나로 두고 process를 수행한다. otherbank는 보존하여 전체 generation을 게시한다.
3. remote current를 이 process의 generation으로 설정하고, 로컬 run 상태를 interrupted로 만들어 **pointer와 ready publication 증거는 존재하나 run 완료 기록이 없는 상태**를 모사한다. 새 pipeline 객체로 같은 run_id를 재개한다. 기존 exact committed resume 테스트와 같은 형태의 상태 주입이다.
4. 재개는 `skip_source_unavailable: stage=pdf`로 실패한다. registry factory를 이용하는 경우와 미등록 issuer fixture fallback의 경우 모두 재현했다.

현재 skip-pdf 진입부는 자기 retained seal을 보기 **전에** preservation_source를 호출한다. current generation이 자기 process 결과이고 비선택 issuer도 들어 있으므로, preservation_source는 그 run을 ReuseSource로 연다. 그런데 재개 중인 run은 running/interrupted 상태여서, 완료 source만 허용하는 ReuseSource가 거부한다. `_publish_sealed`의 기존 동일 generation 검증·완료 상태 reconciliation 분기까지 도달하지 못한다.

게시 데이터가 삭제됐다는 증거는 없다. 이미 성공한 원격 게시를 정상 완료로 복구하지 못해 process CLI가 실패하고 완료 후속 처리가 진행되지 않는 문제다. ReuseSource의 완료 상태 제한은 유지해야 하며, 미완료 자료를 일반 재사용 source로 허용하는 방식으로 해결하지 않는다.

### 수정 지침

- 자체 retained seal이 있는 재개는 먼저 그 seal의 안전한 읽기·issuer 계약 복원·무결성 검증과 기존 publication-only reconciliation을 수행한다. 일반 prior serving 보존 조회가 자체 seal 복구를 가로막지 않도록 순서를 분리한다.
- 최초 실행 또는 seal이 없는 replay는 기존 검증된 preservation_source 및 receipt baseline fence를 유지한다.
- contract/hash 검증과 최신 pointer 보호를 삭제하지 않는다. 현재 remote가 다른 수동 게시 세대이면 기존 predecessor fence에 따라 충돌 처리한다.
- 성공한 동일 세대 복구는 origin/OCR/구조/view/embedding을 다시 호출하지 않으며, 일반 process CLI의 receipt 소비 및 선택 백업 후속 처리로 정상 복귀해야 한다.

### 인수 및 회귀

- 등록/미등록 adapter 양쪽에서 아래 독립 재현을 통과시킨다. 같은 run_id, 동일 generation, status=succeeded, 전체 issuer 자료 보존, 추가 PDF/OCR/embedding 호출 0.
- 실제 실패 주입 위치를 pointer 전환 직후 또는 ready 증거 저장 후 finish_run 이전으로 고정한 검사도 추가한다. 가능하면 CLI의 원래 명령+resume/bare resume 경로에서 exit 0과 acquisition-consumed/완료 후속 처리도 검사한다.
- seal 생성 후 pointer 전환 전 실패(FIX_03 기존 검사), 이미 게시된 exact seal, 다른 최신 수동 pointer, 손상 seal을 함께 검증한다. 후자의 두 경우 안전한 거부를 유지한다.
- 기존 선택 범위·비선택 처리 0·전체 serving·90일 감사/캐시/보존·동일 lock 조건은 변경하지 않는다.

## 직접 검증 및 한계

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_partial_execution.py -q --tb=short` → **73 passed, 23.02초**.
- `uv run --no-sync ruff check apps/cardrag-worker/` → 통과.
- `uv run --no-sync ruff format --check apps/cardrag-worker/` → 123 files already formatted.
- `git diff --check` → 통과.
- 아래 임시 재현: `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_017_review_after_commit.py -q --tb=short` → **2 failed, 2.56초**. 두 매개변수 모두 preservation_source → ReuseSource 상태 검사에서 실패한다. 정상 기대값을 검사해 결함을 드러낸 결과다.
- 실제 운영 이미지·원격 WebDAV·8사 실데이터·proxy/90일 장기 순회/백업 장애 복구는 이번 리뷰에서 검증하지 않았다. 롯데 ZIP 미지원으로 complete collection 운영 인수는 계속 보류다.

다음 Executor는 A1을 수정하고 `FIX_04_REPORT.md`를 새로 작성한다. 기존 handoff 문서는 수정·덮어쓰지 않는다. 관련 회귀와 Worker 전체, ruff/diff 검증 및 실제 수행/미수행을 기록한다.

## 부록: 독립 재현 코드

아래를 `/tmp/test_017_review_after_commit.py`로 저장한다. 성공 게시 후 로컬 상태를 interrupted로 바꾸는 것은 remote commit과 로컬 완료 기록 사이 중단 상태를 재현하기 위한 fixture 조작이다.

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


@pytest.mark.parametrize("registered", [False, True])
async def test_process_reduced_pool_resume_after_commit(corpus, monkeypatch, registered):
    from cardrag_worker.acquisition import AcquisitionSource
    from cardrag_worker.pipeline import WorkerPipeline
    factory, prior, state, pdf_calls, embed_calls, ocr, dav = corpus
    activate(dav, prior)
    initial = factory()
    base = initial.adapters[0].record
    other = _Adapter(replace(base, issuer='otherbank', product_code='other', source_post_id='other', source_url='https://cards.example/other.pdf'))
    other.spec = replace(other.spec, code='otherbank', sort_order=2)
    if registered:
        from cardrag_worker.issuers.registry import REGISTERED_ISSUERS, Registration
        monkeypatch.setitem(REGISTERED_ISSUERS, 'otherbank', Registration(other.spec, lambda: other))
    def runner(adapters, plan=None, source=None, selected=('testbank',)):
        return WorkerPipeline(state=state, state_dir=prior.root, adapters=adapters, selected_issuers=selected, ocr=ocr, embeddings=initial.embeddings, webdav=dav, maximum_attempts=1, retry_cap_seconds=0, execution_plan=plan, reuse_source=source)
    full_runner = runner([initial.adapters[0], other], selected=('testbank', 'otherbank'))
    full = await full_runner.run()
    baseline = ReuseSource(prior.root, full.run_id, state.path)
    activate(dav, baseline)
    collected = await runner([initial.adapters[0], other], ExecutionPlan(phase='collect', selected_issuers=('testbank',))).run()
    source = AcquisitionSource(prior.root, collected.run_id, state.path)
    plan = ExecutionPlan(frozenset({'pdf'}), collected.run_id, phase='process', selected_issuers=('testbank',))
    from cardrag_worker.pipeline import WorkerUnexpectedFailureError
    committed = await runner([initial.adapters[0]], plan, source).run()
    run_id = committed.run_id
    committed_source = ReuseSource(prior.root, run_id, state.path)
    activate(dav, committed_source)
    state.finish_run(run_id, 'interrupted', error='review: pointer committed before local acknowledgement')
    before = (len(pdf_calls), ocr.calls, len(embed_calls))
    result = await runner([initial.adapters[0]], plan, source).run(resume_run_id=run_id)
    assert (len(pdf_calls), ocr.calls, len(embed_calls)) == before
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {'testbank', 'otherbank'}



```

## Worker 전체 재검증 결과

`uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` → **1,343 passed, 10 warnings, 47.56초**. 기존 OCR cache/fallback 경고이며 위 두 추가 재현은 기존 suite 밖의 결함이다.
