# CardRAG v1.0.38 릴리스 노트

CardRAG v1.0.38은 우리카드 RAONK 세션 인증 복구(022), PaddleOCR 임시 심볼릭 링크 격리 및 상태 트리 복구(020), OCR 프로바이더 호출 구조화 진단 로깅(021), 단일 복구 스냅샷 운영 정책 정비 및 누적 변경 사항을 정리한 **GitHub 소스 릴리스**입니다.

---

## 주요 변경 사항

### 1. 우리카드 RAONK 세션 인증 및 다운로드 복구 (022)

- **배경 및 장애 원인**: 우리카드 PDF 다운로드 엔드포인트는 단순 GET/POST 요청 시 세션 및 RAONK 인증 헤더가 누락되면 HTTP 200 상태 코드와 함께 오류 안내 HTML(`issuer_origin_service_error`)을 반환하여 수집 파이프라인이 중단되는 문제가 있었습니다.
- **해결 및 개선**:
  - **세션 핸드셰이크 통합**: 다운로드 전 공식 약관 안내 페이지(`/dcpc/yc1/crd/crd02/H1CRD202S00.do`)로 세션을 수립하고, HTML 내 숨겨진 `raonkAuthPageId`와 `raonkAuthKey` 폼 토큰을 안전하게 추출합니다.
  - **RAONK 파라미터 암호화**: 고정 AES-128-CBC 키(`2018.1548512.15\0`)와 커스텀 Base64 변환 알파벳을 사용하여 `raonkparam`을 생성하고, HTTP 헤더(`RAONK_AUTH_PAGE_ID`, `RAONK_AUTH_KEY`)를 단일 HTTP 클라이언트에 결속합니다.
  - **쿠키 식별자 및 라이프사이클 관리**: RFC 6265 표준에 맞춰 `(name, norm_domain, norm_path)` 튜플 기반의 엄격한 쿠키 식별자를 유지하며, 서버에 의한 쿠키 만료/삭제(`Set-Cookie` max-age=0)를 인터셉트하여 필요 시 세션을 안전하게 재수립합니다.
  - **외부 리다이렉트 보호**: 다운로드 과정에서 외부 호스트로 리다이렉트될 경우 인증 헤더(`RAONK_AUTH_PAGE_ID`, `RAONK_AUTH_KEY`)를 제거하여 자격증명 누출을 방지합니다.
  - **수집과 문서 지원의 구분**: 우리카드 총 922개 상품 레코드 중 918건의 PDF가 정상 다운로드 및 캐시 히트 처리되었으며, DRM 보호 문서(SCDSA004 / 500061, 500062) 4건은 안전하게 건너뛰어(skip) 처리되었습니다. (비정상 HTML을 PDF로 위장 통과시키지 않음)

### 2. PaddleOCR 임시 심볼릭 링크 격리 및 복구 CLI (020)

- **배경 및 장애 원인**: PaddleOCR 하위 프로세스가 강제 종료될 경우 작업 디렉터리에 잔존하는 `.paddle-input-<pid>.pdf` 심볼릭 링크로 인해 V5 용량 점검이 `capacity_path_invalid`로 차단되는 현상이 있었습니다.
- **해결 및 개선**:
  - **임시 경로 격리**: PaddleOCR 입력용 PDF 별칭을 컨테이너 내부의 격리된 `/tmp` 디렉터리에 생성하고, 프로세스 종료 및 예외 발생 시 상시 제거되도록 컨텍스트 매니저 수명 주기를 정비했습니다.
  - **명시적 복구 도구 제공**: 기존에 잔존하던 레거시 심볼릭 링크를 안전하게 점검하고 제거할 수 있도록 `cardrag-worker paddle-alias-recovery <RUN_ID> [--apply]` 명령을 추가했습니다. (반드시 `worker.lock` 배타 락 하에서 안전하게 작동)

### 3. OCR 프로바이더 호출 구조화 진단 로깅 (021)

- **구조화 로깅 (`cardrag.ocr-event.v1`)**: Worker의 stderr 및 systemd journal에 `OCR_EVENT` 마커와 함께 JSON 라인을 출력하여 OCR 프로바이더 호출, 응답, 실패, 폴백 및 취소 이벤트를 안전하게 추적할 수 있습니다.
- **보안 및 민감정보 보호**: 원문 텍스트, 자격증명(API 키, 토큰), 개인정보는 일체 기록하지 않으며, `run_id`, `invocation_id`, `document_id`, `call_id`, `call_kind`, 목표 페이지 범위 등의 상관관계 식별자만 기록합니다.
- **캐시 재사용과 프로바이더 호출 구분**: 재개(resume) 실행 시 기존 캐시가 적중된 문서는 프로바이더 호출 수 0으로 처리됨을 명확히 식별할 수 있습니다.

### 4. 단일 복구 스냅샷 운영 정책 정비

- **단일 독립 복구본 원칙**: `docs/OPERATIONS.md` 정책에 맞춰 `/state/deployment-backups/`에는 검증된 최신 독립 복구 스냅샷 1개만 유지하며, 릴리스 교체 검증 완료 후 불필요하게 누적된 수 기가바이트(GB) 단위의 과거 DB 스냅샷을 안전하게 정리(prune)합니다.
- **코드 롤백과 DB 복원의 분리**: 직전 릴리스 디렉터리 및 도커 이미지를 보존하여 코드 롤백은 즉시 가능하도록 하되, 데이터베이스 복원은 별도의 승인 및 호환성 검증 절차를 거치도록 분리 명시하였습니다.
- **Systemd Timer 유지**: 운영 Worker의 정기 예약은 매일 **10:00 Asia/Seoul** (Persistent=false)로 유지되며, 배포 및 수동 재개 중에도 타이머를 임의로 중단하지 않고 시스템 락을 통해 안전하게 상호 배제를 수행합니다.

---

## 검증 결과

- **자동화 테스트 스위트**: core, worker, mcp, runtime_v1 전체 2,893개 테스트 100% 통과 (0 failed).
- **정적 분석 및 포맷팅**:
  - `ruff check`: All checks passed.
  - `ruff format --check`: All formatted.
  - `mypy` (strict mode): Success (no issues found in 117 source files).
  - `git diff --check`: 통과.
- **운영 릴리스 검증 (022 배포 및 재개)**:
  - 재개 대상 run `df29d8e5dbb34048942a5296bb2d6ac8` 완주: `exit 0`, `status=succeeded`, `quality_status=passed`.
  - 게시 generation `g-df29d8e5dbb34048942a5296-5a3a99224f92` 서빙 반영 및 라이브 `cardrag-mcp` 정상 응답 확인.
  - 비선택 7개 카드사 매니페스트 문서 레코드 변경 0건 (완전 보존 확인).
  - 대상 백업 범위 완결: `backup_scope_complete: true`.

---

## 릴리스 형태 및 범위 안내

- 본 릴리스는 **GitHub 소스 릴리스**이며, 공식 Git 태그는 `v1.0.38`입니다.
- Docker Hub / GHCR에 대한 v1.0.34 전용 공개 레지스트리 이미지 배포 워크플로는 본 릴리스에 적용되지 않습니다.
- 운영 환경은 관리 호스트(`/opt/cardrag`)에 검증된 1.0.38 소스 기반의 로컬 릴리스 및 이미지를 적용하여 서비스를 무중단 유지합니다.
