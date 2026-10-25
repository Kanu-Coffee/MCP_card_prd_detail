# 019 FIX_30_REPORT — 공통 transient-origin 분류·재시도와 롯데/우리 실패 진단

2026-10-22 Executor. FIX_30 및 AMENDMENT_01에 따라 HTTP200 HTML 서비스 오류를 문서 손상과 구분하는 8사 공통 경계, bounded retry, collection/worker 진단과 PDF 집계를 보완했다. 실제 우리 origin은 현재도 HTTP200 HTML 서비스 오류를 반환하므로 **원인 해결·실배치 완주로 판정하지 않는다.** 새 image 배포·resume·timer 변경은 하지 않았다.

## 확정한 원인과 미확정 범위

- 현재 failed run `e81c17e69b5f428f8d6314ab2b8762d4`의 Woori PDF origin은 HTTP200 `text/html` 서비스 오류 페이지를 반환한다.
- 기존 코드는 이를 `PDFValidationError("response MIME type is not PDF-compatible")` → `issuer_source_invalid` nonretryable로 처리해 attempt1에서 종료했고, 선택 사 전체 quarantine 후 generic `worker_unexpected_failure`로만 남겼다.
- 새 코드로 같은 failed source를 대표 1회만 읽기 전용 probe한 결과도 동일했다.

```text
error_class=OriginServiceError
reason_code=issuer_origin_service_error
status_code=200
mime_type=text/html
service_signal=service_unavailable_phrase
origin_recovered=false
state_written=false
attempts=1
```

원본 HTML/쿠키/암호화 form/인증값은 저장하지 않았다. 우리 origin의 일시 장애인지 endpoint 정책 변경인지는 확정하지 않았다. 직접 URL 우회·browser/VPN fallback은 추가하지 않았다.

## 변경

### 공통 origin 오류

- `issuer_collection.py`
  - `IssuerCollectionError`에 bounded `status_code`, `mime_type`, `service_signal` 추가.
  - `OriginServiceError`와 `IssuerCollectionFailedError` 추가.
  - `origin_failure()`가 timeout/network/HTTP status/service error/source invalid를 allowlist reason으로 분류하고 status/MIME을 보존.
  - `IssuerCollectionOutcome`에 `source_id`, `origin_status_code`, `origin_mime_type`, `origin_service_signal` 추가.
- `downloader.py`
  - PDF 다운로드 응답이 HTML이면 최대 64KiB만 읽어 known service-error signal을 검사한다.
  - 서비스 오류는 `OriginServiceError`로 raise하고, 일반 로그인/challenge/알 수 없는 HTML은 기존 nonretryable 검증 오류로 유지한다.
  - 정상 PDF/DRM/ZIP/서명·구조 오류 처리는 변경하지 않았다.
- `rate_limit.py`
  - 목록·상세 등 공통 `RateLimitedClient.get/post`에서도 HTTP200 HTML 서비스 오류를 typed origin error로 변환.
  - 일반 landing HTML은 service-error marker가 없으면 그대로 통과.
- `issuer_metrics.py`
  - 403/429에 더해 Retry-After가 있는 503도 host/issuer cooldown에 반영.
  - Retry-After 없는 503은 기존 stage retry로 남겨 불필요한 장기 cooldown을 만들지 않음.

### 재시도와 보고

- HTTP200 HTML service error는 기존 `_finite_stage`의 bounded maximum_attempts/backoff 안에서 재시도하고, 소진 시 실패한다. 무한 재시도·전체 run circuit·별도 sleep loop는 추가하지 않았다.
- 모든 선택 사가 discovery/download에서 실패하면 `IssuerCollectionFailedError`로 전환해 generic runtime 대신 `issuer_origin` worker failure report를 생성한다.
- worker report에 reason_code/stage/document_id/status_code/mime_type을 제한적으로 기록한다.
- `issuer-collection.json`에 source_id/origin status/MIME/service signal을 기록한다.
- `PipelineResult`/CLI payload와 progress 로그에 `pdf_succeeded_count`/`pdf_skipped_count`를 추가했다. `completed`/`total`에 격리·미지원 skip이 포함될 수 있음을 명시했다.
- `docs/OPERATIONS.md`에 service-error typed reason, Retry-After/cooldown, PDF succeeded/skipped 구분, 같은 날짜 실패 시 `systemctl start` 반복 대신 명시 resume를 기록했다.

## 테스트

신규 `test_fix_30_origin_retry.py` 8건:

- downloader HTTP200 HTML service page → `OriginServiceError`, PDF 파일 미생성
- 공통 RateLimitedClient 목록 HTML service page → typed error
- origin_failure의 status/MIME/signal 보존
- PDF HTML service error 1회 → 정상 PDF 재시도·성공
- 지속 PDF HTML → max_attempts 소진 후 typed worker failure/collection 보고
- 지속 discovery HTML → typed worker failure/collection 보고
- 503 + Retry-After cooldown
- 503 without Retry-After no cooldown

기존 suite에서 generic runtime을 기대하던 discovery 전면 실패 3건은 새 typed `issuer_origin`/`issuer_source_invalid` 계약에 맞춰 갱신했다.

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py -q
8 passed

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1844 passed, 11 warnings in 62.55s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
161 files already formatted
git diff --check
통과
```

## 운영 상태

- active release/image: 기존 `/opt/cardrag/019-fix24-11d1321cc9e6` (새 image를 만들거나 배포하지 않음)
- current run `e81c17e69b5f428f8d6314ab2b8762d4`: failed 유지, day/rotation result를 수정하지 않음
- stable/serving: 기존 신한 `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`/latest succeeded run 유지, MCP `current=Worker stable` 확인
- timer: active/enabled, `Persistent=no`, next `2026-10-23 10:00 KST` (변경 없음)
- 실행 중 Worker: 0
- 실제 배치 재개·backup flush·provider 전체 호출·DB 수정·cursor reset: 미실행

## 남은 단계

- 새 Python 수정을 포함한 식별 가능한 Worker image를 Reviewer 승인 후 배포해야 다음 예약 실행에 적용된다. 현재 active image는 이 수정을 포함하지 않는다.
- 우리 origin이 계속 HTTP200 HTML이면 새 코드도 bounded retry 후 `issuer_origin_service_error`로 정직하게 실패한다. origin 회복 또는 공식 endpoint/protocol 변경 확인 전에는 성공·resume 완주를 선언하지 않는다.
- 실패한 same-day slot은 systemd start 반복이 아니라 원인 교정 후 동일 run의 명시 resume로 처리한다. cursor/day/result는 SQL로 수정하지 않는다.
- 10/23 자동 국민 배치와 019 최종 자동운영 인수는 계속 런타임 후속 검증 항목이다.