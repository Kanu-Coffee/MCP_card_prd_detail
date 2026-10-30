# 020 REVIEW_01 — FIX_01 수정 인수

2026-10-28 KST Reviewer Codex. 사용자의 Reviewer 요청에 따라 PLAN, REPORT, FIX_01, FIX_01_REPORT, 현재 Git diff/status와 구현·테스트를 대조했다. 다른 correction은 없다.

## 판정

**FIX_01의 P1·P2는 해결됐으며 현재 코드 변경을 인수한다. 추가 수정 지시는 없다.** 운영 배포·실제 링크 정리·배치 재개·게시 검증은 이번 인수의 범위에 포함되지 않는다. 020 전체의 운영 복구 완료나 계획 종료를 선언하지 않는다.

### P1: timeout 이후 reap 도중 취소 요청 보존 — 해결

`PaddleOCRVLProvider.recognize_document`는 drain 중 관측한 취소를 별도로 기록한다. timeout이 먼저 발생했어도 drain 중 취소 또는 pending cancellation이 있으면 child reap과 결과 정리 후 `CancelledError`를 전파한다. 취소가 없으면 원래 TimeoutError를 유지하며, 원래 cancellation 경로와 반복 취소의 fence도 유지한다.

Event 제어 테스트에서 timeout → reap 대기 → 첫/추가 취소 → reap 완료 순서를 확인했다. reap 전 alias 생존·Task 미완료, 완료 후 alias 제거·Task 취소를 검증한다. 별도의 무취소 timeout 테스트와 `_operation_with_signal_shutdown`의 모의 SIGTERM callback 연동이 있어, 원래 지적한 취소 유실과 shutdown 오분류를 직접 검사한다.

### P2: FIFO lock open의 무기한 대기 — 해결

`recover_paddle_aliases`는 `O_NONBLOCK | O_NOFOLLOW`로 기존 lock을 열고 regular file/nlink 확인 후 동일 inode의 flock을 비차단 획득한다. lock open 실패와 특수 항목을 안전한 오류로 거부하며 새 lock을 만들지 않는다.

FIFO는 별도 subprocess의 2초 deadline 안에 dry-run/apply 모두 거부된다. missing/symlink/directory/hardlink/FIFO와 busy/idle regular lock의 경계 검사 및 sentinel·alias·lock 보존 검사도 확인했다. 기존 경로 descriptor 검사와 삭제 범위는 유지된다.

## Reviewer 재검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_capacity_v5.py apps/cardrag-worker/tests/test_capacity_v5_export_regression.py -q
233 passed in 9.76s

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
1970 passed, 11 warnings in 67.15s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/src/cardrag_worker/capacity_v5.py apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_paddle_inputs_v130.py
8 files already formatted

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/capacity_v5.py apps/cardrag-worker/src/cardrag_worker/cli.py apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/src/cardrag_worker/paddle_cleanup.py
Success: no issues found in 6 source files

git diff --check
통과
```

11개 warning은 손상된 prior OCR 거부와 fallback/cache 충돌 fixture의 예상 경고다. 실제 Paddle 모델 추론이나 운영 배치 성공을 이 테스트로 증명하지 않는다.

검토한 주요 파일 SHA-256:

| 파일 | SHA-256 |
|---|---|
| `providers.py` | `3b9fec3ce6a47b14e41f352b10fcab44fc297e0c6dfb176489a69d83278d1b86` |
| `paddle_cleanup.py` | `5d5a8ce8bb4a31fded0ed85f4bd39465937bc7d27b06f93d5a7d212bc73a5f52` |
| `test_paddle_inputs_v130.py` | `1ca2d3965b4d6cb3b8b3fbfb838000e7e3ff7971f528a9bc8b33ca8b82d3242d` |

HEAD는 `06af791b6db3fa250b8b543deed46f77f3a4a7d7`이며 변경은 미커밋 상태다. 기존 사용자 timer/OCR 문서 변경을 보존했다. Reviewer는 소스·운영 데이터를 수정하지 않았으며 본 리뷰 문서만 추가했다.

## 운영 인계와 제한

다음 작업은 PLAN §6과 REPORT의 운영 경계를 따른다. 명시 운영 실행 지시 후 최신 service/timer·Worker/backup lock·복구점을 확인하고 수정 이미지와 실제 서비스 계정 접근을 검증한다. 검증된 복구 도구의 dry-run과 대상 확인 후에만 링크를 정리하고 startup/v5 capacity, run 재개 계약, 게시 generation·MCP·범위별 백업·예약을 확인한다.

기존 링크 제거와 새 이미지 배포는 별도 조치이며 하나만 완료해 운영 복구 성공으로 판정하지 않는다. 당시 child 종료의 구체 원인은 미확정이며, 수정된 실제 이미지의 Paddle 추론과 배치 완주는 여전히 미검증이다. 운영 이미지의 pipeline과 저장소의 차이는 배포 준비 시 다시 대조해야 한다. 본 코드 인수만으로 timer를 변경하거나 운영 데이터를 정리하지 않는다.
