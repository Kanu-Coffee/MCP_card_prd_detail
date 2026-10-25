# 019 FIX_13 — variant 증거 검증과 실제 CLI 복구 경로 보완

2026-10-20 Reviewer: Codex. 사용자 요청은 “Review 하고 이상없으면 운영테스트까지 진행”이다. **FIX_12 리뷰는 보완 필요로 판정하여 새 배포·운영 쓰기는 진행하지 않았다.** 아래 문제 해결 후 리뷰 통과 시 기존 사용자 승인 범위에서 운영 반영·백업 실테스트를 이어간다. 별도 승인 절차를 추가하는 것이 아니다.

PLAN/PLAN_AMENDMENT_01, FIX_01–12와 각 REPORT, REVIEW_02, LIVE_REVIEW_02, 운영 자료와 현재 Git/소스를 대조했다. HEAD `aaeb2c4` 위 미커밋 변경과 과거 문서는 보존한다. 활성 운영 release는 `/opt/cardrag/019-ba273106158e`이며 이번 검토로 바꾸지 않았다.

## 1. [P1] doc_info만으로 손상된 receipt를 정상 remote variant로 인정함

위치: `backup.py:_is_valid_remote_ocr_variant`, `resolve_ocr_bundle_paths`.

새 variant 주소를 도입한 방향은 타당하나, 검증된 정상 기존 variant라는 근거가 부족하다.

- `_is_valid_remote_ocr_variant`의 최종 fallback은 `doc_info`에 reuse key/PDF SHA/다른 OCR SHA만 있으면 True를 반환한다. manifest/READY 실제 바이트와 receipt SHA/size의 연결을 검증하지 않는다. metadata 문자열만으로 내용 검증 권위를 부여하지 않는 기존 FIX 계약을 위반한다.
- READY receipt는 존재 여부만 확인하며 실제 READY의 canonical 바이트·manifest/OCR 짝을 검증하지 않는다. manifest size도 입력만 받고 검사하지 않는다.
- manifest 및 READY receipt 조회가 remote_root로 제한되지 않고 `fetchone()`을 사용한다. 다른 root의 기록이나 동일 경로의 여러 SHA 중 임의 행을 현재 canonical 근거로 삼을 수 있다.

### 재현 A

새 `_setup_variant_fixture`에서 A spool을 제거하고, canonical manifest/READY receipt를 각각 임의 SHA로 바꾼다. 실제 바이트 증거 없이 doc_info에 A의 reuse key/PDF/OCR SHA만 기록하면 `resolve_ocr_bundle_paths`가 B를 variant 경로로 라우팅한다. 즉 손상 receipt 충돌을 검증 없이 정상 공존으로 우회한다. 기존 `test_corrupted_canonical_receipt...`는 이 metadata fallback 조합을 검사하지 않는다.

### 요구

- 현재 canonical root의 정확한 receipt에 바인딩된 manifest/READY 바이트를 검증한다. PDF/contract/key/output, canonical SHA/size, READY 짝이 모두 정합적이어야 한다. doc_info/doc_map은 탐색·가시성 보조일 뿐 검증 권위가 아니다.
- 로컬 증거 소실 시 정식 읽기 경로에서 기존 원격 pair를 GET 검증하거나 명확한 미확인으로 보류한다. 손상/불명 receipt를 정상 variant로 분류하지 않는다. 실제 source/contract/output variant 공존은 지원한다.
- root 변경, 다중 receipt, READY 손상/불일치, 잘못된 size, 조작 doc_info, 로컬 spool 없음·현재 cache만 존재하는 상태를 검사한다. 동일 OCR body여도 canonical manifest가 다른 경우의 보존/동등성 정책도 명시하고, metadata identity가 충돌하는 주소를 무조건 재사용하지 않는다.

## 2. [P1] 보고서의 실제 `backup flush --run`은 새 variant를 등록하지 않음

위치: `cli.py:backup_flush_command`, `backup.py` recorder/scope와 새 resolver 연결.

CLI는 scope만 계산하고 flush를 호출한다. 새 B metadata pending을 등록하지 않는다. FIX_12 coexist 테스트는 먼저 `record_run_success`를 직접 호출해 정상 등록된 상태를 만들지만, 보고서의 운영 명령에는 이 경로가 없다.

### 재현 C 및 실환경 읽기 전용 대조

- `_setup_variant_fixture`의 원격 A/receipt와 로컬 B seal만 두고 실제 CliRunner로 `backup flush --run ... --issuer woori`를 호출하면 B variant가 전송되지 않고 missing>0/complete=False다. 테스트가 미리 recorder를 실행하지 않으면 실패한다.
- 현 운영 원장도 pending0이다. 새 코드를 RO staging mount한 사전 점검 결과는 **remaining0/conflicts2/missing2/unbound0**로 그대로다. canonical A spool은 정상 전송 후 정리된 환경이므로 fixture가 A spool을 미리 채워 놓은 것과 다르다. 새 resolver의 client는 scope 계산 시 None이며 doc_info도 현 B를 가리키므로 A 검증/라우팅조차 완료되지 않는다.
- 따라서 FIX_12_REPORT의 “해당 명령으로 A 회수+B 전송+complete=True”는 아직 실행 가능한 절차가 아니다. 운영 조회는 mode=ro, provider/원격 호출0으로 수행했다.

### 요구

- 보존된 seal/run 산출물과 검증 근거로 기존 run의 현재 variant metadata를 정식 등록하고, 기존 A를 검증한 뒤 scope/expected를 확정하여 전송하는 provider-free 재시도 경로를 제공한다.
- 보고서와 명령이 실제 호출 경로에 맞아야 한다. 운영 SQL 삭제·receipt 조작·OCR/수집 재실행·과거 seal rewrite로 대체하지 않는다. 등록과 scope의 라우팅 근거를 일관되게 유지한다.
- 기존 pending0, A spool 없음, doc_info는 B, A remote pair만 존재, index에서 A가 빠진 실제 형태로 **CLI end-to-end** 회귀를 만든다. recorder 직접 선행 호출이나 비운영 factory hook으로 문제를 가리지 않는다.

## 3. [P1] pending 없는 index 복구가 실제 client 생성 경로에서는 실행되지 않음

위치: `backup.py:flush`의 최초 `if not pending`/`needs_index_repair` 분기.

index repair probe는 전달된 webdav_client나 `_make_webdav_client` hook만 사용한다. 일반 CLI는 둘 다 제공하지 않으며 정상 WebDAVClient.from_env 생성은 이 분기 뒤에 있다. 결국 정상 환경에서도 probe를 건너뛰고 unchanged로 반환한다. 후속 completion probe가 client를 생성해도 index repair로 되돌아가지 않는다. 새 테스트는 client를 직접 주입하므로 이 결함을 보지 못한다.

### 재현 B 및 요구

- 정상 A receipt만 있고 pending0, 원격 index items=[]인 fixture에서 `WebDAVClient.from_env`만 mock transport로 바꾸고 `ledger.flush(settings, force=True)` 호출: unchanged, index items=[] 그대로다.
- 일반 client 생성·수명/close·예산/lock 계약을 통합해 pending0에서도 정식 index 복구가 실행되게 한다. 선택 범위를 벗어난 artifact body 업로드로 index 회수를 대신하지 않는다.
- index 없음/일부 누락/읽기 실패, commit 실패/중단/재시도, 정상 기완료 unchanged, root 바인딩과 정상·손상 receipt 구분을 실제 CLI/factory 경로로 검증한다. 잘못된 기존 variant를 무조건 index에 재발행하지 않는다.

## Reviewer 재현과 검증 결과

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review13/test_review13.py -q -s
REPRO A: doc_info alone routes past corrupt manifest/READY receipts without verified bytes
REPRO B: standard factory path with no pending never repairs missing index
REPRO C: actual backup flush --run CLI does not register/upload current variant
3 passed in 0.35s  # 결함을 assert한 임시 재현

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1719 passed, 10 warnings in 56.84s
uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
FAIL: backup.py 및 test_fix_06_scoped_backup.py, 2 files would be reformatted
git diff --check
pass
```

임시 재현 파일은 영구 산출물이 아니므로 위 절차를 저장소 회귀로 옮긴다. 기존 tests가 모두 통과해도 위 실제 호출·부정 증거 조합의 공백은 남아 있다. 제품 코드를 Reviewer가 고쳐 우회하지 않았다.

새 소스로 운영 원장을 RO 조회하기 위해 임시 공개 소스 staging을 사용했다. 원본 소스 mount는 파일 권한으로 읽히지 않았으므로 복사본의 접근권한만 조정했고 원본 권한·운영 원장/remote/serving은 수정하지 않았다. 결과는 `/tmp/cardrag-fix12-readonly-preflight.json`이다.

## 산출물과 다음 운영 검증

Executor는 기존 문서를 수정하지 않고 `FIX_13_REPORT.md`를 새로 작성한다. 보고서의 `/opt/cardrag/state` 예시는 실제 named-volume 구조와 맞지 않으므로 현 `worker-compose.sh run --rm --no-deps worker ...`를 사용하는 구체적인 절차로 정정한다. 현재 파일 포맷도 맞추고 Worker/Core 전체 suite·ruff·diff 및 실제 CLI 회귀 증거를 기록한다.

이번 구현/로컬 검증 단계에서는 실제 배포·원격 쓰기·운영 원장 수정·provider/배치/timer 실행을 하지 않는다. 수정 후 Reviewer가 통과를 확인하면 사용자가 이번 턴에 승인한 운영 반영·백업 실테스트를 이어간다. 그때 A canonical pair 불변, B metadata/CAS 복원 가능성, A/B index·receipt, 선택 complete, 두 번째 재시도의 멱등성과 serving5537/우리980 보존을 확인한다. 정상 정책상 pending은 별도 상태로 유지한다.
