# 019 FIX_13 재검토 — 코드 리뷰 통과, 승인된 운영 검증 진행

2026-10-20 Reviewer: Codex. **현재 코드·로컬 테스트 범위에서 FIX_13 리뷰 통과.** 사용자 승인 “Review 하고 이상없으면 운영테스트까지 진행”에 따라 운영 반영·백업 실검증을 이어간다. 운영 결과는 별도 OPERATIONS_REPORT_03에 기록한다.

PLAN/PLAN_AMENDMENT_01, FIX_01–13 및 REPORT, REVIEW_02/LIVE_REVIEW_02와 현재 Git/실제 호출 경로를 대조했다. HEAD `aaeb2c4` 위 미커밋 변경을 검토했으며 기존 문서는 보존한다. 018은 종료 상태를 유지한다.

- metadata-only variant 인정 fallback은 제거됐다. manifest/READY receipt가 현재 root에 묶이고, 실제 manifest SHA/size·canonical 모델·PDF/key 및 파생 READY SHA/size를 대조한다. 불명/손상/다른 root 기록은 정상 variant로 우회하지 않는다.
- `backup flush --run`이 보존된 run 산출물을 정식 재등록한 뒤 같은 root 근거로 scope/expected를 확정한다. provider/수집 없이 새 variant pending을 회수하는 실제 CLI 회귀가 통과했다.
- pending0 index 복구도 표준 WebDAV client 생성·소유권/close 경로를 사용한다. from_env만 mock한 회귀에서 index 회수와 멱등을 확인했다. 읽기 실패/손상 index에 임의 쓰기를 하지 않는 경계를 유지한다.
- 기존 A pair의 로컬 근거가 정리된 경우에는 완료를 단정하지 않는 보류 정책이다. 실환경은 이 형태이므로, 운영 테스트 전에 실제 원격 A pair를 GET하여 기존 root-bound receipt SHA/size·canonical/PDF/contract/READY 연결을 확인하고 검증된 로컬 evidence를 복원한다. 전체 OCR/backup restore나 수집을 반복할 필요는 없다. 검증 근거 복원은 원격 overwrite/허위 receipt/원장 SQL 수정과 구분한다.
- 동일 body의 다른 canonical manifest는 이번 정책에서 공존으로 자동 인정하지 않고 보류한다. 대규모 identity/저장 구조 변경으로 확대하지 않는다.

Reviewer 직접 검증:

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1731 passed, 10 warnings in 56.55s
uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
153 files already formatted
git diff --check
pass
```

제품 코드는 이 리뷰에서 수정하지 않았다. 보고서의 raw SQL 예시를 실행할 때는 image entrypoint를 Python으로 명시해야 하며, 운영 테스트는 실제 named volume/compose helper 구조에 맞춰 수행한다. 검토 통과만으로 실환경 복구 성공을 선언하지 않는다.
