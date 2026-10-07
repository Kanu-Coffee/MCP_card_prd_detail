# FIX_01 — OpenCode OCR 후보를 실행 가능한 격리 샘플까지 완성

작성 역할: Reviewer. 2026-10-06 KST. `PLAN.md`, `REPORT.md`, `main` 기준 작업 트리와 225개 관련 테스트를 검토했다. **현재 006은 인수 보류**한다. 호스트의 합성 이미지 1회 호출과 fake provider 캐시 테스트는 유효한 단계 1·2 진전이지만, PLAN의 단계 3 및 실제 Worker 이미지·Compose 계약은 아직 충족하지 않는다. 운영 stable 전환(단계 4)과 수일 관찰(단계 5)은 이번 FIX에서 수행하지 않는다.

## 수정해야 할 사항

1. **[P1] Worker 이미지에서 실행 불가.** `Dockerfile`의 worker stage에는 `opencode` 및 필요한 런타임이 없고, `deploy/worker/compose.yaml`은 호스트 바이너리도 마운트하지 않는다. 호스트 `opencode` v1.18.34 실사는 컨테이너 동작의 증거가 아니다. PLAN 단계 0의 두 방식 중 하나를 택해 *새 후보 이미지에서만* 버전·출처가 고정된 CLI를 제공하고, UID 10001/read-only rootfs에서 실제 `opencode --version`과 짧은 합성 이미지 호출을 확인한다. 운영 이미지·볼륨·timer는 변경하지 않는다. 빌드 자원이 과도하면 그 지점에서 중단하고 원인을 보고한다.
2. **[P1] Compose가 모델·effort 기본값을 덮어쓴다.** `compose.yaml:47-48,65`는 provider를 `opencode`로 바꾸어도 기존 `CARDRAG_OCR_MODEL`과 `CARDRAG_OCR_REASONING_EFFORT` 값을 항상 주입한다. Reviewer의 실제 `docker compose config` 결과도 `provider=opencode, model=gpt-5.6-sol, effort=high`였다. 기존 provider 기본값은 유지하면서 OpenCode 선택 시 `alibaba-token-plan/qwen3.8-flash`·`medium`이 적용되도록 별도 opt-in overlay 또는 동등한 배선을 만든다. OpenCode가 fallback일 때도 기존 primary의 `high`를 무심코 재사용하지 않도록 provider별 기본 effort와 contract를 검증한다. 기존 provider의 config/contract hash 불변 테스트를 추가한다.
3. **[P1] 도구 격리와 자격 검증이 불충분하다.** `providers.py:826,881-916`은 전용 agent가 빈 문자열이고, 설정에서 `bash/write/edit/browse` 네 항목만 비활성화한다. [OpenCode 공식 도구 문서](https://docs.opencode.ai/docs/tools/)는 기본 도구가 활성화된다고 설명하며 [권한 문서](https://docs.opencode.ai/docs/permissions/)는 `permission: {"*":"deny"}` 형태를 제공한다. 설치한 CLI 버전에서 검증된 전용 OCR agent·전체 도구 deny 설정을 필수로 하고, `--pure`는 플러그인 제한으로만 취급한다. `CARDRAG_OPENCODE_CONFIG`로 외부 설정을 지정할 때도 같은 제한을 검증하거나 거부한다. `require_providers=True`에서 실행 파일과 API 키(또는 승인된 secret 파일), agent/config의 존재와 권한을 사전 검증한다. 키·OCR 원문은 로그·보고서·커밋에 넣지 않는다.
4. **[P2] 응답 실패를 성공으로 오인할 수 있다.** `providers.py:948-979`는 JSON 파싱 실패 라인을 건너뛰고, text가 없으면 임의의 비 JSON 출력을 OCR 본문으로 반환한다. `--format json`의 미형식·알 수 없는 이벤트/텍스트 스키마·UTF-8 오류·명시적 error 이벤트는 안전한 allowlist reason으로 실패시킨다. 응답 한도는 `communicate()`가 전체 stdout/stderr를 메모리에 받은 *뒤* 확인하고 있다. 가능하면 읽는 동안 상한을 적용하고, timeout/cancel 시 자식 프로세스까지 정리되는지 fake executable로 확인한다. 기존 `split_ocr_pages` 검증은 완화하지 않는다.
5. **[P1] PLAN 단계 3과 운영 문서가 빠졌다.** `REPORT.md`에는 단일 합성 PNG 실사만 있고 3~5유형의 격리 OCR→캐시 재실행→품질 비교, 중단/재개, 전후 운영 무영향 근거가 없다. `docs/OPERATIONS.md`도 미수정이며 별도 opt-in 배포 구성·롤백·관찰 기준이 없다. 아래의 제한된 샘플 검증과 문서를 완료한다. 결과가 부적합하면 운영 반영을 시도하지 말고 정확한 차단점만 보고한다.

## Executor 진행 순서와 비용 경계

1. 현재 브랜치·diff·운영 timer·디스크 여유를 읽기 전용으로 확인한다. 코드는 위 P1을 먼저 고친다. 샘플 실호출은 이미지/Compose/agent 게이트와 fake binary 실패 케이스가 통과하기 전까지 재개하지 않는다.
2. 실제 설정을 반영한 `docker compose config`를 **비밀값 출력 없이** 확인한다. primary와 fallback 조합의 모델·effort·외부 OCR 허용 게이트를 테스트한다. OpenCode 실행/응답/타임아웃/취소·캐시 2회차 hit 테스트를 추가하고 `git diff --check`의 EOF 공백도 정리한다.
3. 후보 이미지에서 합성 공개 가능 1페이지를 한 번만 짧게 호출한다. agent 도구 호출 0건, 페이지 마커, 지연·토큰만 확인한다. 이어서 운영과 분리된 state·WebDAV 게시 비활성 환경에서 **최소 3개 서로 다른 유형 PDF**(짧은 문서, 표·각주, 분할 호출 대상)를 OCRResolver 경로로 제한해 검증한다. 전체 corpus discovery, 5,514건 재처리, Paddle 재실행, 새 운영 generation, 48GiB clone은 금지한다. 모델 호출/토큰·시간 상한을 미리 정하고 초과 시 멈춰 보고한다. 같은 입력을 다시 실행하여 provider 호출 0건·캐시 hit를 입증하고, 페이지·핵심 숫자/표·누락·중복만 기존 결과와 대조한다. 원문 OCR/PDF는 evidence에 넣지 않는다.
4. `docs/OPERATIONS.md`와 opt-in 예시에 실행 파일 위치, 전용 설정/secret, 모델·effort, 격리 샘플 방법, 환경변수 원복·직전 이미지 digest 롤백, 관찰 지표/중단 조건을 기록한다. 기존 stable provider의 설정과 결과가 바뀌지 않도록 한다.
5. 관련/전체 테스트, Ruff, mypy, compose config, Dockerfile 변경 시 hadolint, 설정/자격 변경분 gitleaks를 실행한다. `FIX_01_REPORT.md`에 정확한 명령·성공/실패, 후보 이미지 digest, 샘플 쪽수·유형·호출/토큰/시간·cache hit, 운영 전후 비교, PLAN과의 차이를 기록한다. 전체 테스트를 실행하지 못했으면 통과로 쓰지 않는다.

**인수 범위:** 단계 3까지 합격하면 006의 코드·샘플 검증을 인수할 수 있다. 운영 provider 활성화·stable 이미지 교체는 PLAN 단계 4의 별도 승인 대상이다. 이번 FIX는 운영 timer, `/etc/cardrag/worker.env`, 운영 state/OCR 캐시, WebDAV stable/candidate 포인터를 쓰거나 재기동하지 않는다.
