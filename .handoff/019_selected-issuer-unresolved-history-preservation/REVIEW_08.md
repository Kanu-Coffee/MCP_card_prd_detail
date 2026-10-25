# 019 REVIEW_08 — FIX_19 구현 리뷰 통과

2026-10-21 Reviewer: Codex. **FIX_19 구현 리뷰 통과, 추가 FIX 없음.** FIX_19/REPORT와 REVIEW_07의 실제 재현, 이전 계획/변경계획·FIX 계약, 현재 Git 및 backup/CLI/OCR/Core validator·회귀 코드를 대조했다. HEAD `aaeb2c4` 위 기존 미커밋 변경과 과거 문서를 유지했다. 이번 리뷰에서는 운영 배포·cache 복원·scoped flush·원격 접근/쓰기·배치/timer 기동·삭제를 하지 않았다.

## 차단 사유 재검증

1. **전체 pair 선택**: manifest identity뿐 아니라 동일 후보의 root receipt, canonical READY bytes와 receipt, 실제 scope routing을 모두 검증한 뒤 선택한다. 불완전 variant 후보를 건너뛰고 정상 canonical pair를 선택하며 그 반대 회귀도 추가됐다. manifest/READY를 서로 다른 후보에서 혼합하지 않는다. Core validator가 contract/source에서 reuse key를 재계산하는 기존 계약도 유지한다.
2. **cache 쓰기 경계**: state root 아래 디렉터리 chain을 no-follow로 고정하고 leaf의 regular/uid/nlink/size/read consistency를 검사한다. 임시 파일+no-replace link로 기존 leaf를 덮어쓰지 않고 최종 바이트를 확인한다. canonical에 다른 variant/부분 바이트가 있으면 보존하고 해당 variant 경로를 사용하며, unsafe/unreadable 경계는 오류로 보고한다. recovery/restore는 공유 비차단 lock으로 직렬화한다. 임의 외부 도구가 lock을 무시하는 경우까지 모두 직렬화한다고 주장하지 않는다.

Reviewer의 `/tmp/review-fix18-repro.py`를 현재 코드로 다시 실행했다.

```text
HEALTHY_CANONICAL_PLUS_INCOMPLETE_VARIANT
status=succeeded verified_count=1 in_scope_unbound 2 -> 0
manifest_path=canonical

DANGLING_SYMLINK
status=partial written_outside_cache=false pair=error
```

임시 fixture의 결과이며 운영 cache/원장을 변경한 재현이 아니다. 이전 두 차단 문제는 해결됐고, 검토 범위에서 추가로 재현 가능한 차단 결함을 확인하지 못했다. 기존 stale CAS scope 분리 및 ownership/lost_source/receipt-only 보호 회귀도 유지됐다.

## 이번 Reviewer 검사

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py apps/cardrag-worker/tests/test_fix_16_snapshot_lock.py -q
115 passed in 2.78s

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
통과
```

전체 Worker/Core **1775 passed, 10 warnings**는 Executor 보고 기록을 확인했으며 이번 리뷰에서 전체를 반복 실행하지 않았다. 실제 운영 metadata GET/remote index도 새로 조회하지 않았다. 운영 baseline은 후속 배포 시 현재 manifest로 다시 확정한다. LIVE_REVIEW_06 당시5545/우리980 기준을 과거5537로 되돌리지 않는다.

## 후속 운영 경계

현재 release는 `/opt/cardrag/019-fix16-4108e3dac5f5`이며 새 수정은 아직 배포되지 않았다. timer/service는 inactive다.

기존 사용자 승인 범위의 다음 운영 단계에서 식별 가능한 새 release와 source 검증, 현재 복구점/공간/실행 lock을 확인한 뒤 전환한다. 이후 **현대/BC 현재 retained run을 다시 식별**하고 정확한 BC 두 key의 recover-metadata, 현대/BC selected scope의 required=bound/unbound0, root receipt+committed remote index completion=True, 재시도 멱등과 원격 기존 객체 불변을 확인한다. local staging delta만으로 운영 완료를 선언하지 않는다.

신한/롯데 proxy의 기본 연결 확인과 해당 두 실제 배치 완주는 구분한다. 8사 수동 검증·백업 실제 완료 전에 019를 닫거나 timer를 재개하지 않는다. 새 운영 문제는 그 증거에 따라 다음 FIX로 다룬다.
