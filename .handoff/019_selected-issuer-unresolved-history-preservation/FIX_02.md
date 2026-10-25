# 019 FIX_02 — FIX_01_REPORT 재검토, 미해결 경계 수정 요청

2026-10-19 Reviewer: Codex. **판정: 수정 필요. FIX_01 요구사항 100% 완료 주장은 현재 코드 및 실행 결과와 일치하지 않는다.**

## 검토 기준과 확인된 개선

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, `ANALYSIS.md`, 원본 `REPORT.md`, `FIX_01.md`, `FIX_01_REPORT.md`와 현재 Git 상태/관련 코드를 대조했다. HEAD `aaeb2c4` 위에 017–019 변경이 공존하며 기존 이력은 보존했다. 기존 FIX_02 또는 FIX_02_REPORT는 없었다.

다음 개선은 확인했다: PDF 파일을 검증하는 경로 추가, `current_error` 재평가 시 suppression 해제, canonical ledger/hash 검증 함수 추가, 명시적 `--dry-run` 지원, 전체 배치용 baseline 조회 함수 분리. 과거 오류 이력 제외 자체는 승인된 정책이며 이번 수정에서 철회하지 않는다.

아래는 실제 호출 경로에서 여전히 남은 문제다. 이 리뷰는 제품 코드와 운영 환경을 변경하지 않았다. 추가 재현은 `/tmp/cardrag019-review2.E6CeCD/` 아래 테스트와 pytest 임시 fixture, 임시 CLI state만 사용했다.

## 1. [P1] ledger 검증 실패를 삼킨 뒤 실행·게시 또는 해시 재생성을 계속함

위치: `apps/cardrag-worker/src/cardrag_worker/partial_execution.py:759–776`, `pipeline.py:4430–4445`.

재처리 경로는 `validate_unresolved_ledger_in_seal`이 실패하면 `except Exception: unresolved_ledger = raw_unresolved`로 되돌린 뒤 canonical ledger와 새 SHA를 만든다. 따라서 입력 SHA 불일치나 중복/순서 오류가 거부되지 않고 정상 ledger로 다시 포장된다. FIX_01의 “검증할 수 없는 입력을 새 hash로 정상화하여 통과시키지 않는다” 요구와 직접 충돌한다.

일반 실행도 planner의 hash/count 검증 오류를 warning만 남기고 무시한다. 이후 `preserve_inputs`와 개정 확장은 기존 입력을 사용하거나 해당 ledger를 누락시킬 수 있으며, 최종 게시까지 진행한다. 검증 함수를 추가한 것만으로 invalid baseline을 차단한 것은 아니다.

실제 fixture 재현:

- baseline seal의 unresolved entry 형식은 유지하고 SHA만 `INVALID`로 변경했다.
- PDF reuse + local-only 재처리: `local_only` 완료, 새 seal에 같은 entry가 들어가고 SHA는 정상값으로 교체됐다.
- 전체 카드사 선택 일반 실행에서 새 source version을 공급: 동일한 손상 baseline을 두고 `succeeded`로 게시했다.

수정 요구:

- 입력 ledger 무결성 오류는 명확한 reason으로 차단한다. raw ledger fallback 및 catch-all 성공 진행을 제거한다. 구형 seal의 허용은 명시적 schema/버전 호환 정책으로 구분한다.
- 가능한 사전 검사 단계에서 canonical/hash/count 오류를 확인하여 origin/OCR 호출 전에 실패시킨다. 실제 처분 확정과 사전 무결성 검사는 분리한다.
- 일반/선택/전체/collect-process/reprocess 경로에서 손상 ledger에 대해 새 seal·게시·remediation DB mutation이 발생하지 않는 실제 pipeline 회귀 테스트를 추가한다.

## 2. [P1] 여전히 OCR 이전에 최신본 갱신을 확정하고, 실패 뒤에도 영구 기록이 남음

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:4421–4443`, `unresolved_remediation.py:analyze_unresolved_entry`, `pipeline.py`의 collect 반환(`4845` 부근) 및 OCR 처리(`5516` 이후).

정비 적용은 discovery 이후로 이동했지만 PDF revision expansion 직전이다. 이때 OCR은 아직 실행되지 않았고 collect phase는 OCR 없이 반환한다. `acquired_map`은 다운로드 완료 여부만 나타내며, `failed_map`에는 `_IssuerSkippedDownload`만 들어간다. DRM/ZIP/이후 OCR 실패 및 최종 유효본 상태를 반영한 재평가 호출이 없다. REPORT의 “수집/다운로드/OCR 완료 후”라는 설명은 실행 순서와 다르다.

실제 pipeline fixture에서 같은 product의 metadata-missing 과거 entry를 baseline에 추가하고 신규 현재 PDF 다운로드를 성공시킨 뒤 OCR resolver를 실패시켰다. 실행은 `OCRSystemicFailureError(ocr_unexpected_error)`로 중단됐지만 해당 과거 identity는 DB에 `updated_to_latest`로 남았고 `is_revision_remediated(...)`가 True였다. 최신 유효본이 완성되기 전에 완료를 확정한 것이다.

수정 요구:

- 수집/다운로드 단계에서는 잠정 분류 또는 과거 ledger에 한정한 제외만 수행한다. 최신본 갱신 완료 여부는 실제 문서 유형/lineage와 최종 처리 결과에 연결한다.
- OCR/후속 처리 실패 또는 collect 후 process 실패 시 잘못된 `updated_to_latest`가 영구 상태로 남거나 현재 오류가 필터링되지 않게 한다. 성공/실패 경계와 필요한 transaction 또는 receipt 정책을 명시한다.
- 상품 코드 단위 다운로드 성공만으로 서로 다른 문서 유형의 성공을 대신하지 않는다. DRM/ZIP 같은 결과도 실제 실패·degraded 정책에 맞춰 평가한다.
- 실제 pipeline 및 collect→process 테스트에서 OCR 실패, 목록 실패/부분 수집, 다운로드 실패, DRM/ZIP, 마지막 정상본 보호와 오류 기록을 확인한다.

## 3. [P1] CLI는 완료된 run 상태만 확인하며 actual serving baseline과의 결합을 검증하지 않음

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:2911–2944`, `unresolved_remediation.py:108–118`.

CLI는 여전히 최신 retained 파일 또는 임의 `--seal-path`를 선택한다. `require_verified_run=True`는 `run.status in ('succeeded','no_change')`만 검사한다. 입력 generation이 해당 run의 publication인지, 실제 current serving인지, manifest/profile/DB와 맞는지 검사하지 않는다. canonical SHA는 입력 스스로 재계산할 수 있으므로 baseline 출처 검증을 대신하지 못한다. CLI는 현재 수집 증거도 넘기지 않아 미확인 상태를 `past_excluded`로 적용할 수 있다.

별도 임시 state 재현:

```text
run: completed_but_not_published, status=succeeded
publish 행: 없음
current serving pointer: 없음
입력 generation: never_published_generation
입력 ledger: 형식/count/hash는 정상, source/revision 행 없음
CLI --seal-path fabricated-seal.json --apply
exit=0, status=succeeded, applied_count=1, disposition=past_excluded
```

FIX_01에서 요구한 비서빙/stale baseline 차단은 해결되지 않았다.

수정 요구: 자동 경로와 CLI가 동일한 검증된 baseline/fence 정책을 사용하게 한다. 실제 current serving 또는 그에 결합된 기존 검증 receipt, run/publication/generation/manifest/profile 및 ledger 일관성을 확인한다. 검증할 수 없는 오프라인 입력은 진단 가능 여부와 영구 적용 가능 여부를 구분한다. 미확인 current 상태를 정상 미존재로 보고 apply하지 않는다. 오래된 완료 run, 미게시 run, generation/run 불일치, 임의 self-consistent seal에 대해 DB가 불변임을 검증한다.

## 4. [P2] process/reprocess에서 최종 재평가가 없고 검증 근거도 실제 pipeline 테스트가 아님

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:3630`, `partial_execution.py:replay_pdf_source`, `apps/cardrag-worker/tests/test_019_unresolved_history.py:850–906`.

process/reprocess는 revision-expansion 정비 블록 이전에 `replay_pdf_source()`로 반환한다. replay에는 기존 suppression 테이블 필터링만 있고 최신 OCR/처리 결과로 정비 처분을 확정·재평가하는 호출이 없다. 따라서 collect에서 잠정 오류나 잘못된 완료 기록이 생기면 process 결과로 이를 바로잡지 않는다. “process phase 모두에서 ... 일관되게 정비를 평가하고 반영”한다는 REPORT 설명은 맞지 않는다.

추가된 `test_pipeline_discovery_and_acquisition_failures`는 WorkerPipeline/adapter/OCR를 실행하지 않고 helper에 set/map을 직접 전달하는 테스트다. 마지막 assertion은 미획득 상태에서 `past_excluded` 또는 `updated_to_latest`를 모두 허용한다. 이 테스트로 실제 수집 실패/현재 오류 보호를 검증했다고 할 수 없다.

수정 요구: 실제 phase 경로의 처분/ledger/state 일관성을 검증하고, 실패 후 재개·정비 테이블이 비어 있는 최초 실행·다음 issuer 실행을 다룬다. 같은 오류를 helper가 다시 분류할 수 있다는 사실과 pipeline에서 실제 재평가가 일어난다는 증거를 구분한다. 신규 테스트의 잘못된 허용 assertion을 강화한다.

## Reviewer 검증 및 보고서 보완

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1378 passed, 10 warnings in 49.94s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

git diff --check
통과
```

추가 pipeline 재현 3개는 기존 `test_partial_execution.corpus` fixture를 사용해 손상 hash 재처리/일반 게시 허용과 OCR 실패 후 `updated_to_latest` 잔존을 확인했다 (`3 passed in 1.74s`; 이 재현의 pass는 결함 동작이 관측됐다는 뜻이며 승인 테스트가 아니다). 별도 CLI 재현으로 3번을 확인했다. 모든 HTTP/provider는 기존 fixture의 mock이며 실제 origin/운영 state를 사용하지 않았다.

`FIX_01_REPORT.md`의 fixture 규모 정정과 운영 미검증 명시는 유효하다. 다만 100% 반영, OCR 완료 후 적용, process 정비, 실제 pipeline 실패 검증 주장은 수정 결과 보고서에서 정정한다. 자동 `remediation_plan`은 여전히 지역 변수이며 baseline/identity별 처분·근거 inventory를 run artifact로 남기지 않는다. FIX_01의 감사 근거 보완도 완료해 실제 baseline, 판단 시점, 각 처분과 잔여 오류를 추적할 수 있게 한다.

## Executor 인계

현재 미보고 최상위 correction은 본 `FIX_02.md`다. 기존 문서는 수정/덮어쓰기하지 않고 구현·검증·편차와 미검증 범위를 `FIX_02_REPORT.md`에 작성한다. 위 1–4의 실제 실행 경계를 수정하고 전용/관련 테스트, Worker 전체 테스트 및 ruff를 수행한다. 017–018의 공존 변경, 현재 serving, 공유 CAS 및 마지막 정상본 보호, 비선택 호출 금지 제약을 유지한다. 배포·실제 카드사 호출·timer 활성화는 수행하지 않는다. 사용자 수동 우리 검증 → 나머지 7사 → 백업/degraded 확인 → timer 조정의 운영 경계는 기존 계획을 따른다.
