# FIX_03 — process 게시 재개 계약 복원 및 검증·보고서 정정

작성: 2026-10-19, Reviewer Codex. 대상 HEAD `aaeb2c4`와 현재 미커밋 017/FIX_02 구현.

## 판정 및 검토 범위

**수정 필요.** FIX_02의 기본 경로 수정은 확인했으나, configured pool 밖 issuer를 보존한 process가 seal 생성 후 실패하면 일반 재개가 막힌다. 재개 회귀 검사와 보고서 사실관계도 보강해야 한다. 이 문서가 다음 Executor의 작업 지시이며 기존 PLAN/REPORT/FIX 문서를 덮어쓰지 않는다.

검토: PLAN.md, ANALYSIS.md, REPORT.md, FIX_01.md/FIX_01_REPORT.md, FIX_02.md/FIX_02_REPORT.md, 현재 Git status/diff와 신규 모듈 및 변경 테스트. 이전 리뷰의 A2 자동 pool 복원, A3 최초 process 보존, A4 큐 격리 기본 재현은 통과한다. A1의 정규화 코드 변경은 확인했지만 현재 회귀 테스트는 성공 경로의 증거로 충분하지 않다(B1).

이번 리뷰에서는 제품 코드·테스트 파일·서비스·운영 설정을 수정하지 않았으며 신규 origin 요청·배포·디스크 정리를 수행하지 않았다. 재현은 임시 파일과 mock HTTP/OCR/게시 transport를 사용했다.

## A. 제품 필수 수정

### A1. [P2] 보존 issuer를 포함한 process의 seal 재개 전에 동일한 계약을 복원할 것

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:3533-3541`, `issuer_scope.py:114-134`, `partial_execution.py:427-430`. contract 계산은 `pipeline.py:2110` 이하.

**재현:** 두 issuer 정상 baseline → testbank 한 곳의 collect → configured adapters를 testbank로 축소 → process에서 otherbank 자료를 보존하여 seal 생성 → pointer 전환에 일시 실패 주입 → 새 pipeline 객체로 같은 process run_id 재개.

**결과:** 최초 process는 보존용 adapter를 추가한 전체 publication 계약으로 seal을 만든다. 새 프로세스는 configured adapters만 가진 상태에서 retained seal을 먼저 발견하고 contract_sha256를 비교한다. 보존 adapter를 복원하는 replay/preserve_inputs 경로를 거치지 않으므로 `skip_artifact_incompatible: stage=export`로 거부된다. 변경 없는 정상 receipt·PDF·baseline인데도 일반 재개로 게시를 마칠 수 없다. 미등록 issuer용 dummy 경로와 registry factory 경로 모두 동일하게 재현했다.

기존 serving 삭제가 확인된 것은 아니다. 다만 일시적인 저장소/포인터 장애 뒤 정상 처리 복구가 막히며, 운영자가 재처리를 새 run으로 반복할 위험이 있다. 별도의 publication-only 명령이 존재하더라도 문서화된 process `--resume` 계약의 실패를 해결한 것은 아니다.

**수정 방향:** 최초 export와 seal 재개가 같은 검증된 publication issuer 계약을 사용하도록, 보존 범위 복원/고정을 seal 호환성 검사 이전에 수행한다. selected work scope 및 issuer-scope.json은 그대로 유지한다. 또는 기존 provider-free publication resumer를 안전하게 재사용하되 현재/이전 pointer fence와 seal 무결성을 유지한다. 비교를 삭제하거나 모든 adapter를 실행하는 방식으로 우회하지 않는다.

**인수:**

- 8사 baseline, 1사 configured/selected process를 seal 이후 중단하고 새 객체/프로세스로 같은 run을 재개해 전체 8사 자료를 보존하며 게시 완료.
- 재개 중 원본 HTTP·OCR·구조/view 재생성·embedding 추가 호출 0. registry factory와 현재 fixture fallback 양쪽 검증.
- 이미 같은 seal이 게시된 경우 idempotent reconciliation, 미게시/새 수동 pointer 충돌은 기존 fence에 따라 안전하게 거부. scope·예산·source pin·선택 백업과 수집 receipt 완료 증거도 유지.

## B. 필수 검증 및 보고서 보완

### B1. process CLI 재개 테스트가 실제 성공을 검증하도록 보강

위치: `apps/cardrag-worker/tests/test_017_scope.py:test_process_explicit_resume_restores_inferred_scope`.

현재 fixture는 execution-plan.json만 만들고 `collection-1`의 유효 acquisition receipt/PDF를 만들지 않는다. 마지막 assertion도 출력에 stage=plan이 없다는 것뿐이다. 동일 검사를 exit_code==0으로 바꿔 임시 실행하면 exit 1, status=failed, reason_code=worker_unexpected_failure인데 기존 assertion은 통과한다. 즉 보고서의 “successfully restores”를 실제 정상 재개 완료로 입증하지 못한다. 이 사실은 A1 정규화 코드 자체가 여전히 실패한다는 증거와 구별한다.

유효한 collect receipt와 실제 실패한 process run을 준비하고 원래 명령+resume, bare resume, 명시 동일 issuer 재개에서 exit 0과 동일 run/source/scope/정상 publication 결과를 검사한다. 명시 다른 issuer/source/channel/phase의 사전 거부도 유지한다. 테스트가 상관없는 입력/설정 오류로 통과하지 않도록 한다. 제품 A1의 pointer 실패 재개 검사와 연계해도 된다.

### B2. 롯데 장애의 근거와 운영 인수 보류 상태 정정

위치: `FIX_02_REPORT.md:76-78`의 결론. 기존 문서를 수정하지 말고 `FIX_03_REPORT.md`에서 해당 서술을 명시적으로 정정한다.

“FIX_02에 기재된 ZIP 다운로드 HTTP 500은 외부 endpoint/network 문제이며 code blocker가 아니다”라는 설명은 앞선 증거에 없다. REPORT/FIX_01_REPORT/FIX_02 및 현재 OPERATIONS.md의 확인 사실은 다음과 같다.

- 공식 목록 HTTP 200 응답에 DOCID 1893의 ZIP 첨부가 포함됨.
- 현재 PDF 전용 목록 parser가 `Lotte disclosure attachment filename is invalid`로 거부함.
- 별도 정상 PDF 1건의 strict 다운로드는 성공했음.
- ZIP 지원/완전성 보존 정책 해결 전 롯데 complete collection 운영 인수는 보류.

외부 HTTP 500을 추가로 확인했다면 별도 시점·요청 범위·근거로 기록해야 하며 기존 parser 결함을 대체하지 않는다. 근거가 없다면 HTTP 500 주장을 철회하고 위 상태를 정확히 계승한다. 이번 FIX에서 ZIP 구현을 새로 요구하지 않으며 롯데 정상 수집 완료로 승인하지도 않는다.

## C. Reviewer 직접 검증

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_partial_execution.py -q --tb=short` → **69 passed, 20.65초**.
- `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short` → **1,339 passed, 10 warnings, 45.47초**. 기존 OCR cache/fallback 경고.
- `git diff --check` → 통과.
- A1 임시 재현(아래 코드): `PYTHONPATH=apps/cardrag-worker/tests uv run --no-sync pytest -c pyproject.toml /tmp/test_017_reviewer_round3.py -q --tb=short` → **2 failed**, registry 미등록/등록 두 경우 모두 export 계약 오류. 이는 정상 기대값으로 검사해 현재 결함을 드러낸 결과다.
- B1 추가 검사: 기존 CLI test의 마지막 assertion을 exit_code==0으로 바꾼 임시 검사 → **1 failed**, exit 1/worker_unexpected_failure. 제품 성공 테스트로 교체 필요.
- 신규 운영 이미지·실제 8사 데이터·proxy 304/redirect·장기 순회 실동작·백업 복구는 이번 리뷰에서 검증하지 않았다. 코드/fixture 검토를 운영 인수로 확대하지 않는다.

다음 Executor는 A1 및 B1/B2를 처리하고 `FIX_03_REPORT.md`를 새로 작성한다. 관련 회귀·Worker 전체·ruff/diff 검사를 수행하고 실제 수행/미수행을 구분한다. 기존 보존/90일 감사/프록시 비공개/전체 배치·선택 격리 조건은 유지한다.

## 부록 — A1 독립 재현

아래를 `/tmp/test_017_reviewer_round3.py`에 저장하고 C의 명령으로 실행한다.

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
async def test_process_reduced_pool_resume_after_seal(corpus, monkeypatch, registered):
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
    dav.fail_pointer_once = True
    with pytest.raises(WorkerUnexpectedFailureError):
        await runner([initial.adapters[0]], plan, source).run()
    run_id = state.connection.execute('SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1').fetchone()[0]
    assert (prior.root / 'runs' / run_id / 'sealed/publish.json').is_file()
    before = (len(pdf_calls), ocr.calls, len(embed_calls))
    result = await runner([initial.adapters[0]], plan, source).run(resume_run_id=run_id)
    assert (len(pdf_calls), ocr.calls, len(embed_calls)) == before
    output = ReuseSource(prior.root, result.run_id, state.path)
    assert {d.issuer for d in output.manifest.documents} == {'testbank', 'otherbank'}


```
