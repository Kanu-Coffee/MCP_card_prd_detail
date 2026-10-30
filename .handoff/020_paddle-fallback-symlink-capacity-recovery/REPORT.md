# 020 Executor 보고 — Paddle 임시 링크와 용량 검사 복구

작성: 2026-10-27 KST. `PLAN.md`와 현재 Git·운영 상태, 019 종료 및 관련 FIX, 015 용량 검사 계약을 확인했다. 이번 실행 범위는 코드·테스트·운영 절차 구현과 읽기 전용 운영 진단이다. 운영 배포, 실제 링크 삭제, 배치 재개와 timer 변경은 별도 운영 실행 범위로 남긴다.

## 확인한 원인과 현재 상태

- 10월 27일 하나카드 run `9fa3b10def034fa8b65029e6df2d29ae`은 OCR 742/742 완료 후 `v5_capacity_preflight_failed`, `capacity_path_invalid`로 실패했다. 운영 이미지에서 상태 트리 symlink 거부를 읽기 전용 재현한 PLAN의 직접 원인은 유효하다.
- 문제 링크는 `ocr/fallback/checkpoints/.paddle-input-2788.pdf`이며 운영 경로와 동일하게 state를 읽기 전용 마운트했을 때 대상 CAS 객체가 존재했다. 문제 문서의 fallback checkpoint에는 이 링크만 있었다. 대상이나 링크를 수정하지 않았다.
- run의 문서 디렉터리 5,643개는 모두 `doc_` + 64자리 소문자 hex 형식이었다. 정리 도구의 허용 문서 이름을 기존 `SourceRecord.document_id` 계약과 이 실측에 맞췄다.
- 해당 child가 timeout, 취소, 강제 종료 또는 다른 사유 중 무엇으로 종료했는지와 primary 폴백 원인은 확정하지 못했다. 당일 journal에는 generic fallback 경고 외 구체 사유가 없고, 확인한 해당 시간대 kernel journal에는 OOM/killed-process 항목이 없었다. 이는 OOM이 없었다는 확정 증거가 아니다.
- 운영 image ID는 `sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450`, active release는 `/opt/cardrag/019-fix33-7d2e39edbf59`. 운영 이미지의 `paddleocr_runner.py`/`providers.py`는 착수 전 소스와 동일했다. `pipeline.py` 전체는 달랐으나 기존 sweep 함수의 누락은 양쪽에서 확인했다. 이번 수정 이미지는 빌드·배포하지 않았다.
- 점검 시 service는 `failed`, timer는 `active`, 다음 실행은 10월 28일 10:00 KST다. 상태는 이후 다시 확인해야 한다. 남은 링크가 그대로면 다음 시작 용량 검사도 실패할 수 있다.

## 구현

1. `paddleocr_runner.py`의 확장자 없는 입력용 임시 `.pdf` 링크를 상태 checkpoint 옆이 아닌 호출별 `/tmp/cardrag-paddle-input-*` 디렉터리에 만든다. 기존 CAS 객체는 복사·수정하지 않고 링크만 사용한다. 인식 가능한 원래 suffix가 있으면 원래 입력을 사용한다. direct runner 경로에도 동일한 격리 정책을 적용한다.
2. `PaddleOCRVLProvider.recognize_document`가 임시 링크를 소유하고 `.pdf` 경로를 해석하지 않은 채 child에 전달한다. timeout/취소에는 child kill의 `ProcessLookupError`를 허용하고, 반복 취소가 와도 child wait가 완료된 다음 alias를 정리한다. OCR 내용·모델·fallback 허용 범위·cache identity·checkpoint 계약은 변경하지 않았다. `/tmp`는 배포 Compose에서 read-only rootfs와 별개의 2 GiB tmpfs다. 기존 운영 이미지, UID 10001, read-only rootfs, 동일 tmpfs 옵션으로 링크 생성·정리를 별도 확인했다. 강제 부모 종료 시 `/tmp` 잔여물은 가능하지만 state 용량 검사를 막지 않는다.
3. 기존 명시 resume의 **lock 이전 자동 sweep** 및 pipeline 내부 sweep를 제거했다. `run`, `resume`, `startup-check`는 기존 링크를 자동 삭제하지 않는다. 별도의 `paddle-alias-recovery RUN_ID` 명령은 기본 dry-run이고 `--apply`를 명시했을 때만 처리한다. `CARDRAG_WORKER_STATE_DIR` 및 기존 `worker.lock`만 사용하며 provider 초기화, SQLite open, run 생성, capacity 성공을 요구하지 않는다.
4. 복구는 정확한 32자리 run과 `documents/doc_<64 hex>/ocr/{checkpoints,primary/checkpoints,fallback/checkpoints}`에서 `.paddle-input-<숫자>.pdf` symlink만 대상으로 한다. 대상 PDF와 regular file은 수정하지 않는다. root/조상은 no-follow descriptor로 열고 filesystem·inode 및 lock identity를 확인한다. 목록을 먼저 수집한 뒤 각 항목의 경로·링크 identity를 재확인하고 unlink한다. lock 부재/경합, unsafe 조상, 유효하지 않은 run/document, inode 교체, 파일 접근 오류는 비밀 없는 오류로 실패한다. run이 없을 때도 빈 성공으로 위장하지 않는다. dry-run은 잠금만 잡고 파일을 쓰지 않는다. 최대 문서 100,000개·대상 링크 1,000개로 제한한다.
5. capacity 거부 계약은 유지하고 `path_kind` 숫자 `1=symlink`, `2=non-regular`, `3=filesystem boundary`를 추가했다. 상태 상대 경로는 검증된 canonical run/document의 Paddle alias 형식만 제한적으로 로그에 표시한다. 나머지 이름과 링크 대상은 capacity 로그에 출력하지 않는다. 복구 dry-run의 target은 canonical 운영 CAS 경로만 표시하고 그 외는 `<redacted>`다.
6. `docs/OPERATIONS.md`에 수정 이미지 적용 후 dry-run, 명시 `--apply`, startup-check 순서와 제약을 추가했다. 기존 미커밋 10시 예약/OCR 문서 변경을 보존했다.

## 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py -q
16 passed in 1.57s

uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_capacity_v5.py apps/cardrag-worker/tests/test_capacity_v5_export_regression.py -q
219 passed in 8.72s  # 이후 전용 테스트 및 경계 조건 보강 전 중간 검사

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
1964 passed, 11 warnings in 65.79s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check [변경한 Worker Python 8개 파일]
8 files already formatted

uv run --no-sync mypy [변경한 Worker 소스 6개 파일]
Success: no issues found in 6 source files

git diff --check
통과
```

전용 테스트는 유효 run과 3개 checkpoint 경로, 끊어진/연결된 링크, dry-run과 apply, 기존 lock 경합, 정규 파일·대상 파일 보존, 잘못된 run·누락 run, symlink 조상·디렉터리 교체 거부, CLI 단일 JSON, 진단의 안전한 경로 제한을 확인한다. 별도 child 실행 fixture는 성공·문서 오류·설정 오류·spawn 실패·timeout·취소·timeout 후 재시도를 검증하며 child PID가 종료되고 alias가 제거됐는지 확인한다. 추가 mock은 reap 대기 중 반복 취소가 alias를 조기 삭제하지 않음을 검증한다. 기존 fallback·순회·게시·용량 회귀는 전체 테스트에 포함된다. 실제 Paddle 모델 추론을 실행한 증거는 아니다.

운영 dry-run은 수정 소스의 임시 복사본을 운영 image에 read-only로 공급하고, `cardrag-worker-state`도 `:ro`로 마운트하여 UID 10001로 실행했다. 기존 worker lock을 획득한 결과 `status=ok`, `mode=dry_run`, 해당 run의 예상 링크 **1건**과 `would_remove`를 반환했다. 운영 volume과 이미지에 적용하지 않았다. 최초 소스 직접 mount 시 컨테이너 UID에 저장소 경로 접근 권한이 없어 실패했으며, 소스만 임시 복사해 재시도했다.

## 계획 대비 차이·잔여 위험

- 명시적인 별도 복구 명령을 선택했고, legacy 자동 resume sweep는 제거했다. 따라서 운영자는 기존 링크를 정리한 다음에야 startup/resume할 수 있다. 여러 항목을 적용하다 오류가 나면 앞선 일부 unlink는 완료됐을 수 있으므로 dry-run 재조회가 필요하다. 삭제는 각 항목별 안전 검사 후 실행하며 트랜잭션으로 묶이지 않는다.
- 임시 symlink는 copy/hardlink보다 PDF 추가 용량과 nlink 변경이 없다. `/tmp` writable 여부는 위 컨테이너 환경에서 확인했다. 이미지의 실제 Paddle 로딩·추론과 운영 Worker 전체 완주는 아직 확인하지 않았다.
- 복구 명령의 no-follow 검사는 협력하는 Worker의 기존 lock을 전제로 한다. 임의의 비협력 외부 프로세스가 검사와 unlink 사이에 파일 이름을 교체하는 공격에 대한 원자적 inode-조건부 unlink는 POSIX API가 제공하지 않는다. 운영 정지 구간과 파일 권한을 유지해야 한다. 정리 대상이 symlink인지 재확인하지만 외부 공격자가 동시에 state를 변경하지 못하는 것이 전제다.
- 진단은 `path_kind` 및 검증된 alias 상대 경로를 추가했다. provider child의 실제 당시 종료 사유는 새로 생성하지 않았고 원시 stderr는 기록하지 않았다.
- 실제 배포 image의 `pipeline.py`가 저장소와 다르므로 배포 전 새 release의 전체 차이·의존성·서비스 계정 접근을 검토해야 한다. 운영 dry-run에서 소스 복사본과 기존 이미지 조합의 import 및 정리 경로만 확인했으며 이는 수정 이미지 전체의 배포 검증이 아니다.
- startup-check, capacity 재검사, 운영 링크 unlink, 당일 run의 재개 가능성, 새 generation·MCP·scope backup 확인, 다음 예약 배치 관측은 수행하지 않았다. 실제 Paddle inference·운영 완주를 테스트 대역으로 주장하지 않는다. PLAN §6 순서에 따라 별도 운영 지시가 필요하다.

## Git 상태와 인계

착수 HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`. 착수 전 사용자 변경 `deploy/worker/cardrag-worker.timer`, `docs/OPERATIONS.md`, `docs/SIMPLE_RUNTIME.md`, 미추적 `docs/WORKER_RUNTIME_OPTIONS.md` 및 020 PLAN을 보존했다. 이번 코드 변경은 `capacity_v5.py`, `cli.py`, `paddleocr_runner.py`, `pipeline.py`, `providers.py`, 새 `paddle_cleanup.py`, 관련 두 테스트 파일, `docs/OPERATIONS.md`의 추가 절차와 본 보고서다. 커밋·PR·배포는 하지 않았다.

Reviewer는 PLAN·REPORT·현재 diff와 테스트를 검토한다. 추가 수정이 필요하면 020에 다음 미작성 `FIX_01.md`를 생성하고, Executor는 대응 보고서를 신규 작성한다.
