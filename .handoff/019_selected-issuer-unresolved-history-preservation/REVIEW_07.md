# 019 REVIEW_07 — FIX_18 보완 필요

2026-10-21 Reviewer: Codex. PLAN/변경계획과 이전 FIX·리뷰 계약, FIX_18/REPORT, 현재 Git 상태 및 실제 backup/CLI/Core validator·회귀를 대조했다. **FIX_18 전체 승인 보류, FIX_19 발행.** 과거 문서와 HEAD `aaeb2c4` 위 기존 변경을 유지했다. 이번 리뷰는 로컬 fixture만 변경했으며 새 운영 배포·cache 복원·원격 접근/쓰기·배치·timer 기동·삭제는 하지 않았다.

## 확인한 개선

현재 seal refs·검증된 bundle·ownership을 먼저 보호한 뒤 과거 OCR CAS를 selected required scope에서 분리하는 변경과 receipt/doc_map 보존을 확인했다. BC metadata 복구는 Core OCRArtifactManifest validator로 source/contract에서 reuse key를 검증하고 현재 PDF/OCR identity, canonical READY 및 root-bound receipt를 검사한다. 정상/부정 및 실제 CLI 회귀가 추가됐다.

다만 새 복구 경로에 아래 두 재현 가능한 문제가 있어 배포 전 보완이 필요하다.

## 1. [P2] 불완전한 variant가 정상 canonical pair 복구를 막음

위치: `backup.py:2721–2769`, recover_verified_ocr_metadata.

variant manifest와 canonical manifest를 순서대로 탐색하지만 manifest identity만 맞으면 즉시 break한다. 그 뒤 receipt/READY 검사가 실패하면 다음 후보를 보지 않는다. 따라서 **scope가 요구하는 정상 canonical pair가 있어도**, 앞서 발견된 미완성 variant manifest 때문에 partial이 된다.

기존 `_r18_recovery_ctx`의 정상 canonical manifest/READY+두 root receipt를 그대로 두고, variant 주소에 같은 유효 manifest만 추가했다. variant의 receipt/READY는 없는 부분 게시 상태다. 결과:

```text
HEALTHY_CANONICAL_PLUS_INCOMPLETE_VARIANT
status=partial
verified_count=0
error=manifest bytes do not match a receipt at the canonical remote root
in_scope_unbound_before=2
in_scope_unbound_after=2
```

원래 canonical pair는 변조하지 않았다. 선택은 개별 manifest가 아닌 검증된 전체 pair 단위여야 하며, 실제 required scope와 receipt 경로를 고려해야 한다. 무효 variant의 receipt/READY를 만들어 주거나 덮어쓰는 방식은 금지한다.

## 2. [P2] 복구 cache 쓰기가 symlink를 따라 경계 밖 파일을 생성

위치: `backup.py:2771–2804`, CLI recover-metadata의 쓰기 경계.

is_file/read_bytes 검사 후 write_bytes를 사용해 parent/file symlink를 거부하지 않는다. dangling symlink는 is_file=False여서 그대로 따라 쓴다. 기존 fixture에서 `cache/ocr/<key>/manifest.json`을 cache 밖의 존재하지 않는 파일로 symlink한 후 복구하면:

```text
DANGLING_SYMLINK
status=succeeded
written_outside_cache=true
pair=restored
```

cache 경계를 벗어난 파일 생성이 성공으로 보고됐다. 현재 구현은 검사와 쓰기 사이 writer 직렬화도 없고 기존 파일 읽기/쓰기 OSError를 suppress하므로 파일 존재만으로 already_present/restored가 될 수 있다. 기존 OCR 경계의 symlink 거부 및 다른 local variant 무덮어쓰기 계약을 유지해야 한다. 단순히 경로 문자열을 resolve해 바깥으로 쓰는 것으로 해결하지 않는다.

## 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py apps/cardrag-worker/tests/test_fix_16_snapshot_lock.py -q
106 passed in 2.94s

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
통과
```

위 두 추가 fixture는 `/tmp/review-fix18-repro.py`로 재현했다. 전체1766 통과는 Executor 보고 결과이며 이번 리뷰에서 전체를 반복하지 않았다. 새 경계 조건은 기존 테스트에 없다.

보고서의 다음 운영 검증 기준 `serving 5537`은 오래된 값이다. LIVE_REVIEW_06 당시 기준은 **5545/우리980**이며, 후속 배포 때는 실제 current를 다시 식별해야 한다. 보고서의 staging delta와 실제 전체 운영 scope/remote-index 완료는 구분한다.

현대/BC 원장·원격 바이트를 숫자 맞추기 위해 변경하지 않는다. FIX_19 재검토 통과 후 정식 recovery와 scoped flush의 실제 운영 완료를 확인한다. 019와 timer 비활성 상태를 유지한다.
