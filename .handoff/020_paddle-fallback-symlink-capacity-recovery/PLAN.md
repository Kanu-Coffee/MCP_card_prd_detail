# 020 — Paddle OCR 폴백 임시 링크로 인한 Worker 용량 검사 실패 복구

작성: 2026-10-27 23시대 KST, Planner Codex.

사용자 요청은 당일 장애의 원인과 트러블슈팅 방법을 검토하여 **신규 handoff 계획을 작성**하는 것이다. 이번 세션은 읽기 전용 운영 진단, 로컬 임시 fixture 재현, 기존 테스트 실행 및 본 PLAN 작성까지 수행했다. 구현·운영 링크 삭제·timer 변경·배포·실제 배치 재실행은 수행하지 않았다. 본 PLAN만으로 그 운영 변경을 실행하지 않는다. Executor와 Reviewer는 사용자가 지정한다.

## 1. 목표·범위·현재 인계 상태

2026-10-27 하나카드 자동 순회 배치가 OCR 완료 뒤 `capacity_path_invalid`로 실패한 원인을 해결한다. 기존 OCR/PDF/embedding 캐시와 순회 이력을 보존하면서 해당 run의 복구 가능성을 판단하고, Paddle child의 중단이 다음 배치까지 차단하지 않도록 임시 입력 파일의 수명을 개선한다.

산출물은 재현 가능한 원인 분석, 코드·회귀 테스트, 제한된 기존 잔존물 복구 절차, 배포·실행 검증 절차 및 Executor `REPORT.md`다. 실제 운영 복구가 별도 승인되지 않았다면 코드 검증과 운영 준비까지의 완료 범위를 명확히 기록한다. 코드 테스트 통과만으로 당일 배치 복구·게시 완료를 주장하지 않는다.

- 저장소: `/home/lee/projects/MCP_card_prd_detail`.
- 조사 시 HEAD: `06af791b6db3fa250b8b543deed46f77f3a4a7d7`.
- 착수 전 존재한 변경: `deploy/worker/cardrag-worker.timer`, `docs/OPERATIONS.md`, `docs/SIMPLE_RUNTIME.md` 수정 및 미추적 `docs/WORKER_RUNTIME_OPTIONS.md`. 사용자의 10시 예약·Terra OCR 설정 문서화이므로 덮어쓰거나 되돌리지 않는다.
- `.handoff/STATUS_v1.0.37.md`와 019 `CLOSURE.md`에 따라 001–019는 종료됐다. `.handoff/README.md`의 ACTIVE 표시는 과거 기록이다. 이번 과제는 019의 재개가 아닌 새 020이며, 기존 handoff·종료 문서를 수정하지 않는다.
- 운영 확인 시점 2026-10-27 23시대: Worker service `failed`, 실행 중 Worker 없음. Timer는 enabled/active이고 다음 예정은 **2026-10-28 10:00 KST**다. 새 작업 착수 시 다시 조회한다. 계획 작성으로 예약이 중지된 것은 아니다.

## 2. 확정된 운영 사실과 증거

### 2.1 실행 식별·실효 설정

| 항목 | 확인값 |
|---|---|
| systemd 시작 / 종료 | 2026-10-27 10:00:00 / 15:08:10 KST |
| 서비스 결과 | `Result=exit-code`, `ExecMainStatus=1` |
| 당일 run | `9fa3b10def034fa8b65029e6df2d29ae` |
| 실행 범위 | run의 `issuer-scope.json`: `mode=round_robin`, `selected=[hana]` |
| 활성 release | `/opt/cardrag/019-fix33-7d2e39edbf59` (`/opt/cardrag/current`의 실경로) |
| Worker image | `cardrag-worker:019-fix33-7d2e39edbf59` |
| OCR 실효 설정 | `codex-exec` / `gpt-6.1-terra` / `high` |
| 폴백 실효 설정 | `local-paddleocr` / `PaddleOCR-VL-1.6` |
| 예약 | 매일 10:00 `Asia/Seoul`, `Persistent=no` |

OCR 값은 조사 시 `operations/worker-compose.sh config --format json`의 필요한 환경 키만 필터해 확인했다. 09:09 반영 기록은 `docs/WORKER_RUNTIME_OPTIONS.md`에 있다. 현재 config만으로 당일 모든 개별 OCR 호출이 해당 모델을 사용했다고 단정하지 않는다. 개별 문서의 primary/fallback/cache 사용은 manifest·checkpoint로 검증한다.

### 2.2 당일 journal의 시간순 결과

1. 10:00:02 startup capacity 통과: free `96205041664` bytes, minimum `2147483648` bytes. 이는 최소 기동 검사 통과이며 전체 배치 공간 보장은 아니다.
2. 10:01:06 하나 목록 729건 확인, 10:08:00 collection barrier 완료.
3. 10:36:11 `primary OCR failed; starting fresh whole-document fallback` 경고.
4. 14:57:38 OCR `completed=742 total=742 succeeded=742 failed=0 structure_failed=0`.
5. 15:07:35 아래 오류 후 CLI terminal `status=failed`.

```text
reason_code=v5_capacity_preflight_failed
derived_views=629832 unique_cache_misses=993
database_payload_bytes=3648887472 database_rows=12981569
capacity_reason=capacity_path_invalid probe_path=/var/lib/cardrag-worker
```

6. 15:08:10 systemd 종료 코드 1. 약 5시간 8분 실행 후 실패했으며, OCR 성공은 임베딩·게시 완료를 의미하지 않는다. 품질 preflight의 cosine warning은 통과한 별도 관찰사항으로 이번 terminal 원인과 구분한다.

### 2.3 차단 파일과 읽기 전용 재현

운영 `cardrag-worker-state` 볼륨을 `:ro`로 마운트하고, symlink를 따라가지 않는 트리 조사로 발견한 부적합 항목은 1개이며 조사 오류는 0개였다.

```text
/var/lib/cardrag-worker/runs/9fa3b10def034fa8b65029e6df2d29ae/documents/
doc_71f1ba9072b16b2f229dbfbe4002d7b5b56e327890391c97c2d0d1fefb62207c/
ocr/fallback/checkpoints/.paddle-input-2788.pdf
```

위 표시는 줄바꿈한 하나의 경로다. 링크 대상은 다음 확장자 없는 PDF CAS 객체다.

```text
/var/lib/cardrag-worker/pdf-cache/objects/sha256/4e/
4e968eb9f8ecf90b7a18c806e15967971275007332cbdf3627ce25eec0106bed
```

링크의 ctime은 2026-10-27 10:36:35 KST다. ctime은 생성 시각의 보장이 아니라 inode 변경 시각이며, 폴백 시작 경고 직후라는 정황으로 사용한다. 조사 컨테이너의 mount 경로는 `/state`여서 절대 링크 대상의 접근 여부는 운영 경로를 동일하게 마운트해 별도로 검증해야 한다. 삭제 시 링크 대상을 따라가거나 삭제하지 않는다.

같은 운영 이미지의 `preflight_worker_start_capacity(Path('/state'))`를 읽기 전용 실행해 다음을 재현했다.

```text
V5CapacityError: Worker state capacity tree contains a symlink
reason_code=capacity_path_invalid
```

이는 현재 잔존 링크가 capacity gate를 직접 차단한다는 증거다. 당일 실패도 같은 이유 코드이며 같은 트리를 검사한다. 원래 예외 메시지 전체는 journal에 없으므로 당일의 세부 판단과 현재 재현을 구분해 기록한다. 현재 여유 공간은 약 86 GiB다. 이번 오류는 공간 부족의 증거가 아니며, 링크 문제 해결 후 후속 quota/free-space gate의 통과 여부는 다시 측정해야 한다.

### 2.4 현재 서빙과 코드 일치 여부

- `/serving/v1/channels/stable.json`의 generation: `g-75e5a7f5c75e44b581bdf81d-6b81ea507cc4`.
- pointer mtime: 2026-10-26 15:42:21 KST. 조사 시 `http://127.0.0.1:18015/health/ready`는 `{"ready":true}`다. 이전 세대 서빙 증거이며 오늘 게시 성공 증거가 아니다.
- 운영 이미지의 `paddleocr_runner.py` SHA-256: `aa6cea2289079b6c7ea4cf6bc8d058d27cf76814c91c3c4ae53a86f24c044d43`.
- 운영 이미지의 `providers.py` SHA-256: `a98035a7b5877ce98e0acba6007b14e7df43adb91edda3c43d3f84672d764227`.
- 위 두 파일은 조사 시 저장소와 일치한다. `pipeline.py` 전체 파일은 운영 이미지와 저장소가 다르다. 다만 운영 이미지에서 `inspect.getsource(sweep_stale_paddle_input_symlinks)`를 직접 확인해 아래의 정리 범위 누락이 동일함을 확인했다. 전체 파일 차이가 없다고 간주하지 말고 Executor가 배포 원본·diff·이미지 ID를 확인한다.

## 3. 원인 분석과 추가 확인 항목

### 3.1 직접 원인과 복구 경로의 결함

1. `paddleocr_runner._paddle_input_path`는 확장자 없는 CAS PDF를 Paddle이 분류하도록 **결과 파일 옆**에 `.paddle-input-<pid>.pdf` 심볼릭 링크를 만든다. 폴백 결과의 경우 위치는 `ocr/fallback/checkpoints`다.
2. 정상 Python context 종료 시 `finally`로 삭제하지만 강제 종료 등에서는 실행되지 않을 수 있다. `PaddleOCRVLProvider.recognize_document`는 timeout/취소 시 `process.kill()` 및 `await process.wait()`를 수행하며 `paddle-document-result.json`만 삭제한다. 부모가 input alias를 회수하는 경로는 없다.
3. capacity 검사의 `_tree_usage_fd`는 상태 트리의 symlink·특수 파일·다른 filesystem 경계를 거부한다. 이는 유지해야 하는 무결성 계약이다. OCR이 최종 성공해도 이전 child의 링크가 남으면 후속 v5 preflight가 실패한다.
4. 기존 `sweep_stale_paddle_input_symlinks`는 지정 run의 `documents/*/ocr/checkpoints`만 확인한다. **`ocr/primary/checkpoints`와 `ocr/fallback/checkpoints`는 포함하지 않는다.** 유효 run ID와 실제 fallback 경로를 사용한 임시 fixture에서도 링크가 남는 것을 확인했다.
5. CLI의 해당 정리는 명시 resume 시 startup preflight 앞에서 실행되며, 일반 새 run/startup-check 경로의 잔존물 복구는 제공하지 않는다. 상태 전체를 검사하므로 실패 run을 피하고 다른 카드사로 실행해도 동일 링크가 다음 startup을 차단할 수 있다. 현재 pre-lock 정리 위치도 동시 실행 안전성 검토 대상이다.

### 3.2 기존 테스트가 놓친 이유

`uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py -q`는 **2 passed**였다. 그러나 `test_resume_sweep_only_touches_exact_symlinks`는 다음 결함이 있다.

- `run-9`를 전달하지만 실제 helper는 `[0-9a-f]{32}`만 허용하므로 정리 전체가 no-op이다.
- orphan을 없는 대상에 연결하고 `assert not orphan.exists()`만 사용한다. 끊어진 symlink는 남아 있어도 `exists()`가 False이므로 테스트가 통과한다.
- 별도 임시 fixture에서 `not orphan.exists() == True`와 `orphan.is_symlink() == True`가 동시에 성립하는 것을 재현했다. 신규 검증은 `lstat`/`is_symlink`/`lexists`로 **디렉터리 항목 자체의 소멸**을 확인한다.

### 3.3 아직 확정하지 않은 선행 원인

잔존 링크의 직접 차단과 정리 누락은 확인했다. 다만 **해당 child가 실제로 SIGKILL/timeout/취소 중 무엇으로 끝났는지**, primary가 왜 폴백했는지는 현재 journal만으로 확정하지 않았다. 약 4시간 간격이나 기본 timeout 14,400초만으로 원인을 단정하지 않는다.

Executor는 보존된 run reports, primary/fallback manifest·checkpoint, 제한된 provider diagnostics, child exit·timeout 설정 및 OOM/kernel 기록으로 가능한 범위에서 대조한다. primary 모델 quota/인증 오류, Paddle 모델 추론 오류, 시스템 OOM을 모두 사실처럼 나열하지 않는다. 근거가 없으면 미확정으로 남긴다. 원인 규명을 위해 전체 배치를 다시 돌리거나 모델을 임의 교체하지 않는다.

## 4. 유지할 계약과 관련 파일

- capacity 검사의 symlink 거부, no-follow traversal, free-space/최대 state/sidecar/DB 한도, cancellation fence·단일 writer lock을 유지한다. symlink 전체 허용, 해당 prefix의 용량 계산 무시, 임계값 완화로 우회하지 않는다.
- PDF CAS·OCR artifact·checkpoint·embedding cache·미해결 개정 이력·비선택 7사·serving baseline·백업 ledger·GC pin을 보존한다. 모델·OCR contract/cache epoch·PDF hash·issuer scope를 복구 편의로 바꾸지 않는다.
- timer의 10:00 KST·`Persistent=false`, 기존 순회 cursor·slot·일일 최대 3회 정책을 유지한다. DB 상태/회수/cursor를 SQL로 수정하거나 실패를 성공으로 바꾸지 않는다.
- private overlay·전체 Compose config·인증 파일·원문 OCR·provider stderr를 저장소나 보고서에 넣지 않는다. 진단은 허용된 이유 코드, 상대 경로, 파일 종류·크기·hash 및 제한된 개수로 남긴다.

중점 파일:

- `apps/cardrag-worker/src/cardrag_worker/paddleocr_runner.py`: `_paddle_input_path`, child의 입력 분류·정리.
- 같은 디렉터리 `providers.py`: `PaddleOCRVLProvider.recognize_document`의 임시 자원 소유권·timeout/취소·child 종료 대기.
- `pipeline.py`: `sweep_stale_paddle_input_symlinks`, resume, v5 preflight 및 오류 진단.
- `cli.py`: startup 검사와 writer lock 순서, 명시 resume·startup-check·일반 run의 복구 경계.
- `capacity_v5.py`: `_open_capacity_directory`, `_tree_usage_fd` 및 경로 오류 진단. 거부 계약을 유지한다.
- `state.py`, `rotation.py`, `partial_execution.py` 등: 필요 시 기존 resume/rotation/lock 계약을 확인하고 부수 변경을 제한한다.
- `tests/test_paddle_inputs_v130.py`, `test_cli_settings_provider.py`, `test_capacity_v5.py`, `test_capacity_v5_export_regression.py`, `test_pipeline_v5.py`, `test_fix_33_rotation_attempts.py` 및 필요한 신규 전용 테스트.
- 운영 절차가 바뀌는 경우 `docs/OPERATIONS.md`, `docs/WORKER_RUNTIME_OPTIONS.md`. 기존 미커밋 문서 변경을 보존한다.

착수 시 적용 AGENTS/GEMINI와 Git status/diff를 다시 읽는다. 019 `CLOSURE.md`, `FIX_23.md`/보고서(허용된 whole-document fallback), `FIX_24.md`/보고서(provider diagnostics), `FIX_33.md`/보고서와 amendment(자동 순회), `FIX_34.md`/보고서(서비스 계정 접근), 015 PLAN(capacity 계약)을 필요한 부분까지 검토한다.

## 5. 구현 방향과 트러블슈팅 방법

### 5.1 재발 방지: 임시 입력의 수명과 소유권

권장안은 부모 provider가 호출별 임시 디렉터리를 **영구 state 트리 밖**에서 소유하고, Paddle이 인식하는 `.pdf` alias를 거기에 만들어 기존 `--input`으로 전달하는 것이다. `/tmp` 등 실제 운영 컨테이너에서 쓰기 가능한 임시 공간을 검증해 선택한다. 정상 종료·문서 실패·설정 실패·timeout·취소 모두 child가 종료·회수된 뒤 부모 `finally`에서 정리한다. runner 직접 사용 시에도 상태 checkpoint 옆에 alias를 남기지 않도록 같은 원칙을 적용한다.

- 실제 CAS의 내용·hash·OCR identity와 기존 결과 경로는 유지한다. 임시 alias 경로를 cache contract에 포함하지 않는다.
- 호출별 격리로 다른 실행의 live alias를 glob으로 지우지 않는다. semaphore와 child lifetime, 중첩 취소·ProcessLookupError·spawn 실패를 고려한다.
- 부모까지 강제 종료되면 임시 파일이 남을 수 있지만 영구 state 검사와 다음 batch를 막지 않아야 한다. 컨테이너 임시 영역의 수명·용량도 확인한다.
- symlink 대신 regular copy를 선택한다면 디스크·복사 비용과 삭제, hardlink를 선택한다면 filesystem/nlink/용량 계산 계약을 검증한다. 단순 hardlink 전환을 무검증 채택하지 않는다. 실제 선택과 비용을 REPORT에 기록한다.
- timeout 값을 늘리거나 fallback을 끄는 변경은 이번 복구의 기본안이 아니다. 기존 오류 분류 및 whole-document fallback 허용 범위를 유지한다.

### 5.2 기존 잔존물 복구: 좁은 범위·lock·dry-run

신규 자원 관리만으로 기존 볼륨의 링크는 사라지지 않는다. 기존 helper를 보완하거나 작은 전용 복구 도구를 만들어 다음을 만족한다. 일반 capacity 함수가 파일을 지우게 하지 않는다.

1. 읽기 전용 dry-run으로 run/document·상대 경로·항목 종류·링크 대상·제안 조치를 제한적으로 출력한다. 대상을 따라가지 않고, 항목 수와 출력 길이를 제한한다.
2. 지원 경로는 검증된 run 아래 `documents/<doc>/ocr/checkpoints`, `ocr/primary/checkpoints`, `ocr/fallback/checkpoints`의 정확한 alias 이름으로 제한한다. 이름 규칙은 우선 `.paddle-input-[0-9]+.pdf`로 좁히고, 지원할 legacy 변형은 증거·테스트로 추가한다.
3. root부터 모든 부모 경로를 no-follow로 검사한다. 상위 `documents`/문서/`ocr`/`fallback` 중 하나가 symlink이거나 traversal 중 inode가 교체되면 중단한다. 최종 항목이 symlink인 경우에만 unlink하며, 같은 이름의 regular file·디렉터리·다른 artifact는 지우지 않는다. 링크 대상은 절대 변경하지 않는다.
4. 실제 삭제는 Worker가 없는 정지 구간에서 **기존 worker lock을 비차단 획득·유지한 상태**로 수행한다. lock 획득 실패 시 삭제 0개로 종료한다. 살아 있는 child alias의 stale 여부를 PID 숫자나 mtime만으로 판단하지 않는다.
5. CLI startup preflight가 이 링크 때문에 먼저 실패하므로 복구 도구는 통상 provider 초기화·ORM open·run 생성·capacity 성공을 전제하지 않아야 한다. 같은 기존 lock을 재사용하며 새 병렬 lock 체계를 만들지 않는다.
6. 기본은 명시 run/대상 복구다. 전체 과거 run 자동 정리를 도입하려면 같은 writer lock·안전한 ancestry·예산·dry-run 증거를 요구한다. 현재 문제 run만 정리한 뒤 startup 전체 검사로 다른 잔존물이 없는지 확인한다.
7. 기존 CLI의 pre-lock resume sweep는 lock 안의 안전한 복구로 옮기거나 제거·대체하고, 정상 run/startup-check/read-only 점검의 부수 효과를 명시한다. 단순히 `rglob` 범위를 넓혀 해결하지 않는다.
8. 삭제 거부·권한 오류·경로 교체는 제한된 이유 코드와 검사 실패로 보고한다. 예외를 삼킨 뒤 정리가 성공했다고 표시하지 않는다. 멱등성을 검증한다.

한 번의 정리만으로는 재발 방지가 끝나지 않는다. 반대로 신규 코드만 배포하고 기존 링크를 남기면 startup에서 계속 실패할 수 있다. 두 조치를 별도로 검증한다.

### 5.3 진단 개선

`capacity_path_invalid`의 외부 의미를 유지하면서, 내부 원인을 `symlink`/`non_regular`/`filesystem_boundary` 등 제한된 분류와 문제의 state-relative path로 구분할 수 있게 한다. 절대 호스트 경로·링크 대상 전체·URL·원문 payload를 무제한 로그로 출력하지 않는다. 동일 트리를 반복 전수 조사하지 않고 원래 거부 시 관측한 정보를 활용한다.

원래 child 종료 사유의 진단이 부족하다면 기존 provider diagnostics 정책 안에서 exit code, timeout/취소 분류, parent cleanup 결과를 보강한다. 진단 실패가 원래 오류를 숨기거나 batch를 성공으로 만들지 않게 한다.

## 6. 운영 복구 실행 절차 — 별도 실행 지시 후

1. **상태 재확인·예약 충돌 방지:** 실제 시각·timer·service·수동 Worker/backup 작업을 다시 확인한다. 운영 변경 승인이 있는 경우 timer의 원래 enabled/active/예약/drop-in 값을 기록하고 기존 운영 절차에 따라 예약을 중지한다. 실행 중 Worker가 있으면 중단을 강제하지 말고 lock 획득 실패로 복구 작업을 멈춘다. 다음날 예약은 링크가 남으면 startup에서 실패할 수 있음을 운영자에게 알린다.
2. **근거·복구점 보존:** 당일 journal, filtered config, image ID, 해당 run scope/reports, alias `lstat`/`readlink`, stable pointer 및 recovery guidance를 private 운영 증거로 보존한다. DB를 열어야 하거나 배포를 전환한다면 worker/backup lock을 유지한 정지 구간에서 최신 두 DB의 SQLite online backup, `quick_check`, SHA·WAL 상태를 기록한다. 운영 DB를 외부 모니터가 직접 열거나 오래된 snapshot을 현재 복구점으로 재표시하지 않는다. 작은 링크 증거 보존과 전체 DB 복구점 생성의 목적은 구분한다.
3. **기존 링크 정리:** 검증된 복구 도구의 dry-run 결과가 위 run의 예상 항목인지 확인한 뒤, lock 아래 symlink 항목만 unlink한다. 대상 PDF CAS의 존재·hash/identity와 OCR 산출물이 보존됐는지 전후 비교한다. 광범위 `find -delete`, `rm -rf`, 볼륨 초기화, cache flush, Docker volume prune는 사용하지 않는다.
4. **수정 이미지 준비·접근 검증:** 식별 가능한 새 이미지/release와 필요한 파일만 준비한다. 기존 운영 볼륨·private overlay·auth·Paddle 모델 캐시·로컬 serving을 유지한다. 기존 `verify-service-access.sh`로 실제 cardrag 계정 접근을 확인한다. config 문법 검사와 `startup-check`를 수행하고 OCR/embedding resolver 및 capacity 통과를 기록한다. `startup-check`는 전체 배치 용량·추론·게시 성공을 보장하지 않는다.
5. **해당 run의 재개 판단:** snapshot 또는 기존 공식 상태 경로에서 run 상태·재개 계약·scope·predecessor·seal 존재를 확인한다. 이번 실패는 게시 전 v5 검사이므로 `resume-publication`을 무조건 사용하지 않는다. 기존 run의 명시 `resume 9fa3b10def034fa8b65029e6df2d29ae`가 현재 계약상 가능하면 우선 검토한다. 명시 resume는 discovery를 새로 확인하고 stage의 유한 재시도 epoch를 재설정하므로 origin 미호출/OCR 무조건 0회를 약속하지 않는다. cache 계약과 hash가 유효한 완료 산출물의 재사용을 확인한다.
6. **날짜·순회 fence 검증:** 복구 실행이 10/28 이후라면 전날 자동 run의 origin/OCR 자동 재개 제한과 현 날짜 slot을 확인한다. 명시 resume·선택 재실행 중 실제로 허용되는 공식 경로를 선택하고 근거를 기록한다. 불가하면 수동 `run --issuer hana` 등 검증된 경로로 새 run을 만드는 방안을 보고한다. 날짜/cursor/result를 편집해 우회하지 않는다. 같은 날 round-robin 재실행은 최대 3회·새 run 정책과 다르므로 명시 resume와 혼동하지 않는다.
7. **배치·게시·백업 검증:** 터미널 결과·exit code·동일 run 또는 새 run ID, capacity 수치, 임베딩·seal/manifest·local pointer/MCP current generation을 연결한다. 비선택 7사와 미해결 이력의 보존을 확인한다. 해당 run/issuer 범위의 백업 등록·완결/대기를 기존 정책으로 검증하고, 기존 범위 밖 pending과 구분한다. 실패하면 마지막 정상 서빙을 유지한 채 원인을 기록한다.
8. **예약 복구와 후속 관측:** 운영 지시 범위와 기존 승인 절차에 따라 timer를 원래 매일 10:00 KST·`Persistent=false`로 복구하고 다음 실행을 확인한다. 다음 예약이 실제 기동해 terminal·게시까지 완료했는지 별도로 기록한다. 놓친 slot은 자동 보충되지 않는다. 정상화 검증용으로 cursor를 초기화하거나 8사 전체를 재처리하지 않는다.

이미지 rollback과 DB snapshot restore는 별개다. 구 이미지로 돌아가도 링크가 다시 생길 수 있고, 구 DB 복원은 당일 OCR/순회·백업 증거를 잃을 수 있다. 기존 정상 generation은 유지하고, DB restore는 계약·무결성 검증과 별도 사용자 승인 없이 수행하지 않는다.

## 7. 테스트와 인수 기준

### 코드 검증

1. 유효한 32자리 run ID, 확장자 없는 CAS PDF 및 `ocr/fallback/checkpoints`의 stale alias로 **수정 전 실패/수정 후 통과** fixture를 만든다. `ocr/checkpoints`, `ocr/primary/checkpoints`, 연결된 링크와 끊어진 링크도 포함한다. 단순 `exists()`로 삭제를 판정하지 않는다.
2. 실제 subprocess fixture로 alias 생성 후 timeout·취소·강제 child 종료를 재현한다. 모델 다운로드/외부 OCR 없이 검증하되 실제 `process.kill`/reap 경로와 부모 cleanup을 통과시킨다. 정상·문서 오류·설정 오류·spawn 실패·중첩 취소에서도 child 누수와 영구 state alias 잔존이 없다.
3. 정상 PDF suffix와 CAS 입력 모두 동일 페이지 결과·hash/cache identity를 유지한다. primary 실패→whole-document fallback→성공 및 fallback timeout→후속 재시도 성공에서 provider 텍스트를 혼합하지 않고 capacity 검사를 통과한다.
4. 정리 도구는 lock을 다른 프로세스가 소유하면 변경 0개, dry-run도 변경 0개다. 대상 밖 run, regular file, 잘못된 alias 이름, symlink 부모·문서 root·fallback dir, traversal 중 교체, 권한 오류를 시험한다. 링크 대상 PDF와 외부 sentinel은 모두 보존된다. 동일 작업 두 번 실행해도 안전하다.
5. 임의 symlink·특수 파일·다른 filesystem 경계는 계속 capacity에서 거부된다. 실제 free-space/state quota/sidecar/DB limit 오류도 원래 이유 코드로 실패한다. 정리가 capacity 무결성 검사를 무력화하지 않는다.
6. 명시 resume 전 startup 실패의 복구 순서와 일반 run/startup-check의 부수 효과를 테스트한다. 자동 순회 scope·당일 3회·다음 날짜 전진·게시 predecessor fence를 유지한다. 비선택 7사 보존·cache reuse·backup 계약 회귀를 확인한다.
7. diagnostics에 비밀 sentinel, 원문 stderr/URL/OCR 텍스트가 노출되지 않고 경로·항목 수 제한을 지킨다. cleanup 실패가 원래 오류를 가리지 않는다.

권장 실행 명령(새 전용 테스트 파일을 함께 지정):

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_paddle_inputs_v130.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_capacity_v5.py apps/cardrag-worker/tests/test_capacity_v5_export_regression.py -q
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
uv run --no-sync ruff check apps/cardrag-worker/
git diff --check
```

CLI/rotation/publication 계약을 변경했다면 관련 `tests/runtime_v1` 및 기존 repository CI의 type/관련 검사를 추가한다. 신규 외부 도구·dependency 설치를 전제하지 않는다. mock 테스트와 Paddle 실제 CPU 추론·운영 완주를 구분하고 실제로 수행한 검사만 REPORT에 적는다.

### 운영 인수

운영 실행이 승인된 범위에서 다음 증거가 있어야 복구 완료다.

- 현재 state의 문제 alias가 안전하게 제거됐고 startup/v5 capacity 검사 모두 통과했다.
- 수정된 provider의 정상·중단 경로가 영구 state에 alias를 남기지 않는다. 실제 모델 추론 검증을 했다면 사용 입력·모델·결과를 별도 기록한다.
- 복구 run의 exit·terminal 결과, 게시 generation·manifest·ready가 연결되며 MCP가 해당 검증 세대를 실제 제공한다. `ready=true`나 과거 exit 0만으로 판정하지 않는다.
- OCR/PDF/cache/비선택 7사·미해결 이력·순회 상태가 보존됐다. 백업은 해당 scope의 실제 상태로 보고한다.
- timer의 최종 상태·다음 예약을 기록하고, 다음 예약 배치 관측이 남았으면 명시한다. 자동 8일 전수 검증을 요구하거나 완료했다고 주장하지 않는다.

## 8. Executor 보고와 Reviewer 인계

Executor는 본 PLAN과 실제 Git/운영 상태를 대조한 뒤 `REPORT.md`를 새로 작성한다. 최소한 다음을 포함한다.

- 시작·종료 Git 상태, 변경 파일·이미지/release 식별자 및 운영/소스 차이.
- 확정한 직접 원인, child 종료의 선행 원인에 대한 확정/미확정 구분.
- 수정 전후 재현, 실 subprocess·안전 정리·capacity/회귀 테스트의 명령과 실제 결과.
- 선택한 임시 파일 설계와 대안 검토, cleanup의 lock/경로/진단 정책.
- 운영 작업의 승인·실행 범위, 실제 삭제 대상과 전후 증거, 복구점·배포·run·게시·백업·timer 결과 또는 미실행 경계.
- PLAN에서 달라진 점, 남은 위험·관측 항목과 다음 실행자에게 필요한 구체적인 조치.

Reviewer는 PLAN/REPORT 및 현재 diff·검증 증거를 읽고 판단한다. 수정이 필요하면 새 `FIX_01.md`부터 순서대로 작성하고 Executor는 대응 `FIX_01_REPORT.md`를 작성한다. 기존 019 문서나 본 PLAN을 덮어써 검토 이력을 바꾸지 않는다.
