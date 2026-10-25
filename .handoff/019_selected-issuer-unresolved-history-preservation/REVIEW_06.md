# 019 REVIEW_06 — FIX_16 리뷰 통과

2026-10-20 Reviewer: Codex. **FIX_16 구현 리뷰 통과. 추가 FIX를 발행하지 않는다.** 이는 새 이미지 배포/BC 운영 완주 또는 019 전체 종료 판정이 아니다. 현재 Git HEAD `aaeb2c4` 위 미커밋 변경과 과거 handoff를 유지했다. 이번 리뷰에서는 제품 코드 수정·빌드·배포·운영 DB snapshot/변경·원격 쓰기·배치/timer 기동·삭제를 하지 않았다.

## 검토 근거 및 판단

FIX_16/REPORT/OPERATIONS, REVIEW_05와 이전 계획·변경계획·백업 계약, 현재 Git status 및 실제 backup/CLI/state lock/운영 도구·테스트 코드를 대조했다.

1. **현재 소유 metadata의 pending 독립 보호**: 검증된 bundle 등록 시 별도 run-bound required-path 기록을 남긴다. 이는 경로 가시성만 유지하며 expected SHA/size 또는 전송 권한을 부여하지 않는다. 현재 seal의 문서/PDF/OCR에 일치하는 기록은 대기 행 삭제·receipt-only에서도 남고, 바이트 근거가 없으면 unbound로 완료를 차단한다. legacy pending/lost_source도 보호한다. 기존 정상 삼성 shared-cache 제외는 유지된다.
2. **RO 조회 호환성**: 생성자 초기화를 생략하는 실제 monitor 형태의 subclass에서도 scope가 필요한 메모리 상태를 초기화한다. 없는 run으로 조회할 때 이전 제외 카운트를 초기화한다. 테스트는 실제 RO SQLite와 DB bytes 불변을 검사한다.
3. **snapshot/전환 경계**: 도구는 기존 Worker와 같은 flock 방식으로 worker/backup 두 lock을 획득하고, online backup으로 WAL을 포함한 DB snapshot을 검증한 뒤에도 두 lock을 유지한다. timer/service와 수동 Worker 컨테이너 비실행·수동 writer 금지라는 운영 전제는 유지된다. 준비 가이드에는 helper 전체 복사·실제 입력 digest·새 이미지 참조·설치 소스 대조·복구 증거·원자적 활성화가 연결돼 있고 기본 배치를 자동 실행하지 않는다. 미실행 운영 절차를 이미 검증된 배포 결과로 표시하지 않는다.
4. **공간 정책**: 현재와 다른 과거 DB의 무조건 재사용 안내를 제거했다. 새 일관된 snapshot이 필요하며 동일성 입증 때만 재사용한다. 기존 후보의 자동 삭제나 전역 prune을 수행하지 않는다. 대형 출력 형식/GC 재설계로 범위를 확장하지 않았다.

검토 범위에서 재현 가능한 추가 차단 결함을 확인하지 못했다. legacy content/adopted의 소유 근거가 전부 소실된 경우 현재 소유였다고 소급 추정하지 않는 한계는 REPORT에 명시돼 있다. 기존 승인된 shared-cache 제외 정책을 유지하는 해석이며, 새 검증 등록부터 의무를 보존한다. 원격 body/index 완전성은 별도 운영 검증 대상이다.

## 이번 Reviewer 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py apps/cardrag-worker/tests/test_fix_16_snapshot_lock.py -q
115 passed in 6.33s

uv run --no-sync ruff check apps packages deploy/worker/snapshot-lock.py
All checks passed!

uv run --no-sync ruff format --check apps packages deploy/worker/snapshot-lock.py
222 files already formatted

git diff --check
통과
```

현재 worker/core Python 80파일의 경로+NUL+bytes+NUL digest는 `d6685263a45526e28a4a3b1db2fc43ab2bd99cede49403f7f9a9b2bf9ad5b6a8`로 Executor 보고 대상과 일치한다. 전체 Worker/Core **1752 passed, 10 warnings**는 FIX_16_REPORT의 실행 기록을 확인했으며 이번 리뷰에서 전체를 반복 실행하지 않았다. Executor의 실제 RO 삼성 monitor 결과(2085 required/bound, 정상 제외2, unbound0)는 보고서와 구현·회귀 의미가 일치한다. 이번 리뷰에서 원격 index나 artifact body를 새로 읽지는 않았다.

## 후속 운영 인계

- 현재 `/opt/cardrag/current`는 `/opt/cardrag/019-e7fb8a0ba60c`, timer/service inactive다. 새 소스의 운영 반영은 아직 안 됐다.
- 기존 사용자 승인 범위의 다음 운영 단계에서 FIX_16_OPERATIONS의 frozen source/식별 가능한 새 image 준비 → 설치 소스 확인 → 정지/lock/snapshot → release 활성화 순서를 수행하고 실제 결과를 새 OPERATIONS_REPORT로 남긴다. 절차를 그대로 실행하기 전 당시 이미지·볼륨·권한·여유 공간과 활성 프로세스를 재확인한다.
- 사용자가 새 배포의 BC 단독 실행을 수행한 뒤 exit/run·비선택 7사·8사 serving·백업 required/excluded/remote-index completion을 검증한다. 기존 실패 run을 백업 완료 대상으로 대체하지 않는다. 삼성 retained run의 정상 제외 진단도 확인한다.
- BC 운영 성공과 나머지 카드사 수동 검증을 완료하기 전 019를 닫거나 timer를 재개하지 않는다. 운영 검증에서 새 문제가 확인되면 그 증거에 따라 다음 FIX를 발행한다.
