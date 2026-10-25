# 019 FIX_03 — FIX_02_REPORT 재검토 및 추가 수정 요청

2026-10-19 Reviewer: Codex. **판정: 수정 필요. 운영 적용 전 아래 3건을 수정하고 재검토한다.**

## 기준과 확인된 개선

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, `ANALYSIS.md`, 원본 `REPORT.md`, FIX_01/FIX_02 및 각각의 REPORT, 현재 Git 상태와 관련 코드를 대조했다. HEAD는 `aaeb2c4`이며 017–019 구현이 미커밋으로 공존한다. 기존 문서는 보존하고 다음 correction artifact인 본 문서만 추가한다.

FIX_02에서 지적한 ledger SHA 검증 예외 무시와 raw fallback은 제거됐다. 개정 확장 단계는 `past_excluded`만 반영하며 OCR 실패 후 `updated_to_latest`를 조기 등록하지 않도록 개선됐다. 최종 처리/replay 재평가와 inventory 아티팩트도 추가됐다. 이 개선은 인정하지만 새 최종 처리의 타입/해시 정합성 및 CLI baseline 인증이 아래와 같이 불완전하다.

## 1. [P1] DRM 항목이 있으면 최종 정비에서 AttributeError로 정상 문서까지 게시하지 못함

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:6310–6312`, `partial_execution.py:776–778`.

두 경로 모두 `unsupported`를 순회하면서 `doc.record.issuer`와 `doc.record.product_code`에 접근한다. 이 목록의 항목은 `UnsupportedProductRecord`이며 필드는 `source`다(`contracts.py:181`). `.record`는 존재하지 않는다. 정상 v5 baseline이 있는 실행에서 DRM 항목이 하나만 포함돼도 최종 정비 블록이 예외를 내며 배치가 실패한다. 기존의 DRM 격리·정상 문서 처리 지속 계약에 대한 회귀다.

실제 pipeline fixture에서 기존 정상 카드와 새 DRM 카드(`SCDSA002` 바이트)를 함께 수집했다. DRM은 다운로드 단계에서 정상적으로 unsupported로 분류됐지만, 최종 단계에서 다음 오류가 발생했다.

```text
pipeline.py:6312
AttributeError: 'UnsupportedProductRecord' object has no attribute 'record'
외부 노출: WorkerUnexpectedFailureError(worker_unexpected_failure)
```

수정 요구:

- 일반 실행과 replay 모두 실제 `UnsupportedProductRecord.source` 계약에 맞춰 처리한다.
- ZIP은 `unsupported_attachments`라는 별도 목록이다. 현재 final/replay의 `unsupported_map`은 이 목록을 포함하지 않으므로 DRM/ZIP 모두 처리했다는 REPORT 주장을 검증하고 필요한 실패/저하 증거를 포함한다.
- 정상 v5 baseline + 정상 문서 + DRM 문서의 일반 실행 및 실제 collect→process/reprocess 테스트를 추가한다. 정상 문서는 게시되고 DRM은 unsupported/degraded로 남으며 과거 오류를 허위 해결하지 않아야 한다. ZIP의 기존 마지막 정상본 보호도 검증한다.

## 2. [P1] 최종 ledger를 변경한 뒤 corpus_sha256은 변경 전 값으로 게시함

위치: `apps/cardrag-worker/src/cardrag_worker/pipeline.py:4848–4870,6335–6373`, `partial_execution.py:797` 이후의 ledger 필터 및 `_build_v5_generation` 호출.

v5 corpus identity는 `_v5_corpus_identity_payload()`에서 unresolved ledger/count/SHA까지 결합하는 계약이다. 일반 실행은 수집 직후 이 payload로 `corpus_sha256`을 계산한다. 이후 새 final 단계에서 `updated_to_latest`를 반영해 unresolved ledger를 필터링하고 ledger SHA를 다시 계산하지만 corpus SHA는 재계산하지 않은 채 `_build_v5_generation()`에 전달한다. replay도 ledger는 변경하면서 `source.manifest.corpus_sha256`을 그대로 재사용한다.

실제 fixture에서 source identity를 재계산해 만든 과거 cache source/revision을 추가하고, SourceRecord를 복원할 수 없는 과거 entry 1개를 재현했다. 현재 문서의 OCR/처리가 완료되면 그 entry는 정비 완료된다. 결과는 다음과 같았다.

```text
corpus hash에 결합된 unresolved entry 수: 1
최종 게시 seal의 unresolved entry 수: 0
final ledger를 사용한 canonical corpus SHA != 게시 manifest.corpus_sha256
다음 실행: 원본/문서 변화가 없어도 succeeded로 다시 게시
다음 실행 corpus_sha256 != 직전 실행 corpus_sha256
```

이는 단순 metrics 표시 차이가 아니다. 기존 corpus identity 계약과 no-change/재실행 안정성을 깨뜨린다.

수정 요구:

- 최종 ledger와 corpus identity를 같은 시점의 입력으로 확정한다. 일반/collect-process/reprocess에서 새 ledger가 실제 corpus SHA에 결합되도록 하고, 기존 immutable baseline은 수정하지 않는다.
- checkpoint/acquisition receipt/실행 결과/manifest/serving DB metrics가 서로 다른 시점의 identity를 섞지 않게 한다. 입력 수집 identity와 최종 게시 identity를 분리해야 한다면 그 의미와 검증을 명시한다.
- unresolved 1→0 및 current_error 잔존/재평가 사례에서 canonical payload와 게시 corpus SHA의 일치를 직접 검사한다. 동일 입력의 다음 실행이 불필요하게 다시 게시되지 않고 no-change로 안정되는지, 중단 후 재개도 hash drift가 없는지 확인한다.

## 3. [P1] CLI의 latest-ready ID 검사만으로 seal 내용 및 actual current serving을 인증할 수 없음

위치: `apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py:120–138`, `cli.py:remediate_unresolved_history_command`.

미게시 run/generation을 거부하는 검사는 개선됐으나 `require_verified_serving`은 여전히 SQLite에서 `published_at DESC`인 ready 행의 ID만 비교한다. 실제 current pointer나 그것에 결합된 검증 receipt를 확인하지 않는다. rollback/포인터 전환 상황에서 최신 ready 행과 실제 serving은 다를 수 있다. 또한 입력 seal의 manifest/DB/profile 및 unresolved ledger가 해당 publication의 검증된 내용인지 확인하지 않으므로, 기존 run/generation 문자열을 복사하고 ledger SHA를 재계산하면 별도 파일도 적용 가능하다.

임시 SQLite/CLI 재현:

```text
등록된 succeeded run과 최신 ready publication: 존재
pdf_cache_source 행: 존재
입력 seal: 그 run/generation ID만 복사한 별도 JSON
입력 manifest/게시 DB/profile: 없음
입력 source_id/pdf_sha256 revision 연결: 없음
입력 ledger: 형식/count/SHA만 자체 일관성 유지
CLI --seal-path fabricated-seal.json --apply
exit=0, status=succeeded, applied_count=1
```

source 행 존재만으로 입력 ledger의 출처가 증명되지는 않는다. FIX_02의 actual serving baseline 결합/임의 self-consistent seal 차단 요구는 아직 완료되지 않았다.

수정 요구:

- 적용 대상은 실제 serving에 결합된 검증된 baseline 및 그 ledger로 제한한다. 기존 baseline 검증 경로 또는 검증된 receipt/fence를 재사용한다. latest-ready 정렬과 ID 문자열 비교를 실제 current 증거로 취급하지 않는다.
- explicit `--seal-path`도 manifest/DB/profile/ledger의 출처·일관성을 검증한다. source 행은 있으나 입력 PDF revision 연결이 없는 경우를 포함해 잘못된 대상이 DB에 suppression을 남기지 않게 한다. 복잡한 metadata 복원 계층을 요구하는 것은 아니다.
- 실제 current보다 최신인 ready 행이 있는 rollback 상황, 실제 publication ID를 복사한 별도 self-consistent seal, source/revision 연결 불일치를 시험한다. 오프라인으로 증명할 수 없는 경우에는 진단과 apply 가능 여부를 구분한다.

## 검증 결과와 테스트 근거 보완

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1382 passed, 10 warnings in 50.13s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

git diff --check
통과
```

추가 임시 테스트는 기존 `test_partial_execution.corpus` fixture와 mock HTTP/provider만 사용했다. 경로는 `/tmp/cardrag019-review3.FU6fp1/test_review019.py`이며 다음 명령으로 재현했다.

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src uv run --no-sync pytest /tmp/cardrag019-review3.FU6fp1/test_review019.py -q -s -o asyncio_mode=auto
3 passed in 1.84s
```

이 3개 중 DRM crash와 corpus hash drift 테스트는 결함 동작을 확인하는 재현이며 승인 테스트가 아니다. 나머지는 실제 `AcquisitionSource`를 사용하는 collect→process의 기본 성공/정비 확정을 확인한 긍정 사례다. 위 CLI 우회는 별도 임시 state로 재현했다. 운영 DB, 실제 카드사 호출, 배포 또는 timer 변경은 없었다.

`test_collect_then_process_remediation_lifecycle`은 process의 source_run_id를 collect run으로 지정하지만 실제 `source` 인수에는 이전 baseline `ReuseSource`를 전달한다. 따라서 그 테스트만으로 실제 acquisition receipt→`AcquisitionSource`→process를 검증했다고 할 수 없다. 이 테스트를 실제 source로 바꾸고 위 hash 일치·DRM·재개 조건을 검증한다. 단순 성공 사례에서는 실제 AcquisitionSource도 동작함을 Reviewer가 확인했으므로, process 기능 전체가 실패한다는 판정은 아니다.

## Executor 인계

본 문서가 최신 미보고 correction이다. 기존 PLAN/REPORT/FIX 이력은 덮어쓰지 말고 구현·테스트·실제 결과·편차·운영 미검증 항목을 `FIX_03_REPORT.md`에 작성한다. 기존 017–018 변경을 보존하고, 공유 CAS/현재 serving/비선택 격리/마지막 정상본 보호 계약을 유지한다. 관련 전용 및 Worker 전체 테스트와 ruff를 실행한다. 이 리뷰는 제품 코드 변경·배포·실수집·timer 활성화를 승인하거나 수행하지 않았다. 검토 후 사용자 수동 우리 실행 → 나머지 7사 → 백업/degraded 확인 → timer 조정의 운영 순서를 유지한다.
