# 019 FIX_01 — Reviewer 수정 요청

2026-10-19, Reviewer: Codex. **판정: 수정 필요, 코드 검토 통과 및 운영 적용 승인 보류.**

## 검토 기준과 범위

`PLAN.md`, `PLAN_AMENDMENT_01.md`, `ANALYSIS.md`, `REPORT.md`, 현재 Git status/diff와 관련 구현 및 테스트를 대조했다. 변경 문서가 우선하므로 과거 22건의 무조건 계승이나 복잡한 metadata resolver 복원을 요구하지 않는다. 승인된 과거 이력 제외 정책에 맞게, 현재 자료의 오류와 과거 제외를 구분하고 검증된 대상만 처리하는 것이 이번 수정의 목표다.

HEAD는 `aaeb2c4`이며 017–019 관련 변경이 다수 미커밋으로 공존한다. 019 디렉터리에 기존 FIX 문서는 없었다. 이 리뷰는 제품 코드를 수정하지 않았고, 운영 DB/컨테이너/카드사 origin에 접근하거나 배포하지 않았다. 기존 PLAN/REPORT를 덮어쓰지 않고 다음 correction artifact로 본 문서를 추가한다.

## 1. [P1] 현재 수집 결과 없이 영구 제외를 확정하고 이후 오류에도 해제하지 않음

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:3640–3647`, `unresolved_remediation.py:145–186,247–263`, `state.py:is_revision_remediated`.

자동 경로는 discovery 이전에 `plan_unresolved_remediation(state, seal)`과 apply를 실행한다. `known_sources`, `current_discovery`, `current_failed_products`를 전달하지 않고, 수집/OCR 이후에 이를 다시 평가하는 호출도 없다. 따라서 테스트에서만 공급하는 현재 실패 정보는 실제 실행에서 사용되지 않는다. metadata 누락 등은 현재 상품 상태를 알 수 없어도 `past_excluded`로 영구 기록되고, collision은 cache active 표시만으로 `updated_to_latest`가 된다. 최신 유효 PDF나 성공적인 현재 처리를 확인한 결과가 아니다. 뒤에서 해당 카드사의 수집/처리가 실패해도 먼저 기록한 제외 상태는 남는다.

더욱이 이후 같은 identity를 `current_error`로 평가해 apply해도 기존 suppression 행을 제거하거나 비활성화하지 않는다. `is_revision_remediated()`는 disposition을 검사하지 않고 행 존재만으로 True를 반환한다. 따라서 재평가 후에도 원래 오류를 계속 필터링할 수 있다.

수정 요구:

- 사전 검사와 최종 정비 확정을 구분한다. 선택 카드사의 현재 목록 성공/완전성 및 실제 처리 결과를 이용하고, 최신 유효본 확인 없이 `updated_to_latest`를 기록하지 않는다.
- 비선택의 과거 unresolved ledger 제외는 허용되지만 그 사실을 명시하며, 이를 현재 정상본 갱신이나 상품 소멸의 증거로 취급하지 않는다.
- 현재 오류가 확인되면 기존 제외 기록 때문에 오류가 숨겨지지 않게 한다. 이전 failed run의 기록이 다음 실행을 부당하게 제약하지 않도록 transaction/적용 시점 또는 재평가 정책을 정한다.
- 실제 pipeline을 통한 discovery 실패, 부분 수집, 다운로드/DRM/ZIP/OCR 실패, 같은 identity 재평가 테스트를 추가한다. helper에 임의의 실패 목록을 넣는 테스트만으로 완료하지 않는다.

## 2. [P1] PDF 객체 존재 판정이 디스크 검증을 하지 않아 현재 오류를 과거 제외로 오분류

위치: `apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py:126–131,172–183`.

`pdf_cache_object_exists`는 `SELECT 1 FROM pdf_cache_object`만 검사한다. 실제 파일 존재/크기/해시와 무관하다. revision은 object 행을 FK로 참조하므로 정상 DB에서 revision 행이 남아 있으면 object 행도 존재한다. 이 때문에 코드가 의도한 `is_current_in_cache and not pdf_cache_object_exists` 분기는 실제 파일 유실을 판별하지 못한다.

임시 SQLite에 active source/revision과 object 행을 만들고 object 파일은 만들지 않은 재현 결과:

```text
actual_file_exists=False
is_current_in_cache=True
reported_object_exists=True
disposition=past_excluded
```

apply 후 이 identity는 정비 완료로 표시된다. 현재 바이트 오류 검증을 약화시키지 말라는 변경 계획의 요구에 어긋난다.

수정 요구: 기존 PDF cache의 안전한 파일/내용 검증을 재사용하거나, 검증이 없는 상태에서는 객체 존재/복구 완료를 주장하지 않는다. active revision과 historical revision 각각에 대해 object 행만 존재하는 파일 유실, 크기/해시 불일치를 시험하고 현재 오류가 보존되는지 확인한다. CAS 바이트를 정비 목적으로 삭제하지 않는다.

## 3. [P1] 검증되지 않은 ledger와 실제 serving에 연결되지 않은 seal로 영구 suppression 가능

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:2909–2925`, `unresolved_remediation.py:214–225`, `pipeline.py:3646–3647`, `partial_execution.py:750–759`.

CLI는 actual current serving 확인 없이 최근 retained run의 첫 파일 또는 임의 `--seal-path`를 `json.loads`로 읽고 apply한다. planner는 ledger schema, canonical 순서/중복, 사유, count/hash를 검사하지 않는다. 자동 경로의 `preservation_source`/`ReuseSource`도 이 호출 전에 unresolved metrics의 일관성을 검증하지 않는다. 기존 `_validated_v5_metrics`에 있는 ledger 검증이 정비 전에 적용되지 않는다. 재처리 경로는 입력 ledger hash 검증 없이 필터링하고 새 hash를 계산한다.

별도 임시 state와 CLI를 사용한 재현: 존재하지 않는 run/generation을 지정하고 ledger SHA를 `INVALID`로 둔 seal을 `--seal-path ... --apply`로 전달했는데 `exit_code=0, status=succeeded, applied_count=1`이었다. source/revision 증거도 없는 identity가 영구 제외 대상으로 등록됐다. 이는 단순한 진단 입력이 아니라 이후 실행의 오류 필터링에 영향을 준다.

수정 요구:

- 정비 대상을 실제 serving에 결합된 검증된 baseline으로 제한하고 run/generation/profile 및 ledger의 schema/count/hash/canonical 일관성을 검증한 뒤 mutate한다.
- 오프라인 운영을 지원해야 한다면 기존 검증된 serving receipt/fence에 결합하는 명시적 방법을 설계한다. 최근 run이라는 이유만으로 current baseline이라고 가정하지 않는다.
- explicit seal도 동일 검증을 거치며, 실패 시 DB를 변경하지 않는다. 손상 hash, 중복 entry, 알 수 없는 reason, 비서빙/stale run, 잘못된 source/revision 연결의 테스트를 추가한다.
- 구형 seal은 기존 호환 정책에 따라 처리하되, 검증할 수 없는 입력을 새 hash로 정상화하여 통과시키지 않는다.

## 4. [P2] 최초 전체 배치 및 collect/process에서는 자동 정비 경로를 건너뜀

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:3627–3647`, `issuer_scope.py:preservation_source`, `partial_execution.py:750–762`.

유일한 자동 plan/apply는 `_preserved_source is not None` 안에 있다. 전체 카드사를 선택하면 `preservation_source()`는 비선택 issuer가 없다는 이유로 None을 반환하므로, 아직 정비 테이블이 비어 있는 최초 전체 배치는 기존 과거 오류를 다시 확장한다. 재처리/replay 경로는 그 앞에서 반환하며 기존 테이블을 조회하는 필터만 있으므로 최초 정비를 수행하지 않는다. 이후 새로 발생한 동일 유형의 과거 오류도 다음 실행 방식에 따라 결과가 달라진다. REPORT의 전체 재개/재실행 방지 주장을 뒷받침하지 못한다.

수정 요구: 비선택 문서 보존 여부와 검증된 baseline 정비 대상 산출을 분리하고, 수동 선택/round-robin/전체 배치/collect-process/reprocess/게시 재개에 일관된 정책을 적용한다. 모든 경로에서 무조건 mutate할 필요는 없지만, 의도적으로 건너뛸 경우에도 같은 오류가 재유입되지 않는 근거와 테스트가 필요하다. 각 경로를 정비 테이블이 비어 있는 상태에서도 검증한다.

## REPORT 및 검증 근거 보완

기존 REPORT는 보존하고 수정 결과와 정확한 검증 범위는 `FIX_01_REPORT.md`에 기록한다.

- `test_selected_run_preserves_serving_and_skips_nonselected_adapters`는 두 개 가상 issuer의 작은 fixture다. 8사 5,536문서 보존을 검증하지 않는다. discovery 호출 여부와 issuer 집합만 확인하며 비선택 PDF/OCR/parser/embedding 미호출과 문서/아티팩트별 불변 비교도 없다. 실제 규모 검증과 fixture 검증을 구분한다.
- 세 사유 helper 테스트의 identity는 4개다. 실제 baseline 22건 inventory와 처분을 검증했다는 주장으로 확대하지 않는다. 새 fixture는 운영 product/source ID를 그대로 쓰지 않는다.
- 정상 미존재/실패 테스트는 함수에 `current_failed_products`를 직접 주입하며 실제 pipeline 연결을 검사하지 않는다. 위 1번 회귀를 추가한다.
- REPORT에 기재된 `remediate-unresolved-history --dry-run`은 실제로 옵션이 없어 exit=2다. 명시적 옵션을 구현하거나 기본 무옵션 dry-run 사용법으로 바로잡고 CLI 도움말/인계 절차를 일치시킨다.
- 자동 적용한 inventory는 현재 지역 변수에만 있고 상태 테이블에는 baseline/run 및 근거가 남지 않는다. 최소한 검증한 baseline과 각 identity의 처분/근거를 추적 가능한 결과물에 남긴다. 실제 잔여 unresolved 및 운영 미검증 항목도 명시한다.

## Reviewer 검증 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1375 passed, 10 warnings in 49.19s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted
```

추가로 임시 디렉터리/SQLite와 `CliRunner`를 사용해 2번 파일 유실 오분류, 잘못된 ledger hash의 적용 허용, `current_error` 재평가 뒤 suppression 잔존, 문서상 `--dry-run`의 exit=2를 재현했다. 운영 state와 원본 바이트는 사용하지 않았다. 전체 테스트 통과는 확인했지만 위 경계 조건을 기존 테스트가 다루지 않아 승인 근거로 충분하지 않다.

## Executor 인계 및 완료 기준

1. 본 FIX와 PLAN/변경 문서, 기존 REPORT 및 실제 Git 상태를 다시 확인한다. 017–018의 공존 변경을 덮어쓰지 않는다.
2. 위 1–4의 경계를 수정하고 의미 있는 회귀 테스트 및 REPORT 근거 보완을 수행한다. 상품별 하드코딩/운영 ad-hoc SQL/기존 seal rewriting은 금지한다.
3. 관련 전용 테스트, Worker 전체 테스트 및 ruff check/format을 실행하고 명령·실제 결과·잔여 제약을 `FIX_01_REPORT.md`에 기록한다. 원본 REPORT의 실제 운영 검증 미실시 사실과 fixture의 한계를 명확히 정정한다.
4. Reviewer 재검토 전 배포·실제 수집·timer 활성화를 하지 않는다. 검토 후의 사용자 수동 우리 실행 → 나머지 7사 → 백업/degraded 확인 → timer 조정 경계는 기존 계획을 유지한다.
