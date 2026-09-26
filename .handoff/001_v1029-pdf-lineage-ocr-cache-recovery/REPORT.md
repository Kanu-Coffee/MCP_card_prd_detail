# v1.0.29 PDF lineage·OCR 캐시 복구 및 실행 검증 보고서 (REPORT.md)

- **작성 일시**: 2026-09-25
- **작성자 역할**: Executor (Antigravity)
- **작업 기준 문서**: [.handoff/001_v1029-pdf-lineage-ocr-cache-recovery/PLAN.md](file:///home/lee/projects/MCP_card_prd_detail/.handoff/001_v1029-pdf-lineage-ocr-cache-recovery/PLAN.md)
- **Git 기준 브랜치**: `release/v1.0.29`

---

## 1. 개요 및 수행 결과 요약

v1.0.29 릴리스 준비 중 발생했던 대규모 OCR 재처리 및 rate limit 실패(`cardrag-v122-candidate-worker-v1029`)의 원인을 해결하기 위해, v1.0.28 성공 상태(`cardrag-worker-v114-candidate-state`, generation `g-bd9a4c513041462886d347af-9b84c2e38c14`)로부터 5,192건의 PDF lineage와 OCR 캐시를 완전 복원하고, 신규 및 교체된 14건의 PDF만 로컬 PaddleOCR로 안전하게 처리하도록 파이프라인 및 복구 도구를 구현·검증했습니다.

| 검증 항목 | 계획 기준 (PLAN.md) | 실제 달성 결과 | 상태 |
|---|---:|---:|:---:|
| 이전 current 문서 수 | 5,044 | 5,044 | 일치 |
| 이전 historical 문서 수 | 148 | 148 | 일치 |
| Seed 수락 OCR 문서 수 | 5,192 | 5,192 (Native 3,682 / Adopted 1,510) | 일치 |
| Seed 수락 PDF CAS 객체 수 | 4,710 | 4,710 | 일치 |
| Seed 수락 소스 계보 수 | 5,203 | 5,203 | 일치 |
| Seed 수락 리비전 수 | 5,208 | 5,208 | 일치 |
| Seed 원자적 Ledger 해시 | 봉인 필수 | `15fe2504454553bc08a6a8621bbf48ceb325a7a559e2544cd52a509bbc871f81` | 검증 완료 |
| Seed 멱등성 (2차 적용 검증) | 추가 import 0건 | 0건 (`idempotence_verified: true`) | 검증 완료 |
| 외부 OCR 호출 허용 여부 | 0건 (`allowed=false`) | `CARDRAG_EXTERNAL_OCR_ALLOWED=false` | 강제 차단 |
| 기본 OCR Provider / Model | `local-paddleocr` / `PaddleOCR-VL-1.6` | `local-paddleocr` / `PaddleOCR-VL-1.6` | 적용 완료 |
| 전체 단위/통합 테스트 스위트 | 2,196 passed 이상 | **2,204 passed** (0 failed) | 100% 통과 |
| 타입 및 린트 검사 | 무결점 통과 | `ruff check` 통과, `mypy src` 44개 파일 0 errors | 100% 통과 |
| 실패 증거 보존 | 기존 컨테이너·볼륨 보존 | `cardrag-v122-candidate-worker-v1029`, `cardrag-worker-v122-candidate-state` 보존 | 보존 완료 |
| 후보 실행 컨테이너 기동 | Detached 기동 및 상태 검증 | `cardrag-v122-candidate-worker-v1029-r2` (`running`) | 정상 가동 중 |

---

## 2. 주요 코드 변경 사항

### 2.1 State & OCR Seed 복구 모듈 (`state_seed_v122.py` 및 CLI)
- [apps/cardrag-worker/src/cardrag_worker/state_seed_v122.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/state_seed_v122.py)
  - **다중 Manifest Schema 지원**: 배포 환경의 `publish.json` 스키마(`cardrag.generation.v6`)와 `cardrag.worker-manifest.v1`를 모두 허용하도록 스키마 검증 완화.
  - **Pruned 고대 객체 방어 처리**: 과거(2026년 8월 등) 정리되어 디스크에 없는 5건의 고대 CAS 객체는 안전하게 스킵하되, 활성 5,192건 대상 CAS PDF는 디스크 존재 및 해시 일치를 강제(fail-closed).
  - **자기참조 외래키 제약조건 순환 방지**: `pdf_cache_source(superseded_by_source_id)` 외래키가 먼저 삽입되지 않은 후속 source를 참조하는 문제를 방지하기 위해, 1차 삽입 시 `superseded_by_source_id`를 NULL로 삽입 후 모든 source가 확보된 뒤 일괄 UPDATE하는 2단계 리플레이 구현.
  - **CAS 객체 및 리비전 원자적 머티리얼라이즈**: `pdf_cache.ingest` 및 `WorkerState.record_pdf_cache_object`(`verified_at` 인자 일치)를 사용하여 안전하게 destination volume으로 CAS 및 메타데이터 복제.
  - **원자적 Ledger 커밋**: 최종 복구 결과를 `audit-reports/state-seed/<sha256>.json`에 기록하고 멱등성 검증(2차 pass 실행 시 0건 추가 생성 확인) 통과 시에만 적용 승인.
- [apps/cardrag-worker/src/cardrag_worker/cli.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/cli.py)
  - `cardrag-worker seed-state-v122 SOURCE_ROOT --generation-id ... [--apply] [--expected-documents 5192]` CLI 명령 추가. destination Worker lock 획득을 강제하고 멱등성 2회 검증 수행.

### 2.2 OCR Seed Preflight 및 외부 OCR 호출 차단 가드 (`ocr.py`)
- [apps/cardrag-worker/src/cardrag_worker/ocr.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/ocr.py)
  - `StateSeedLedger`를 로드하여 시드 generation에 포함된 5,192건 문서의 PDF SHA 및 identity를 조회.
  - 시드 문서가 예기치 않게 cache miss가 될 경우, 외부 유료 API나 대량 Paddle 재처리를 시도하지 않고 즉시 `OCRSeedPreflightError("seed_document_missing_from_cache")`로 fail-closed 중단.
  - `CARDRAG_EXTERNAL_OCR_ALLOWED=false` 환경에서 Codex/OpenRouter 등 외부 OCR provider가 선택되거나 fallback으로 지정되면 시작 전 차단.

### 2.3 강제 재검증 플래그 및 Corpus Diff 생성 (`pipeline.py`, `corpus_diff.py`)
- [apps/cardrag-worker/src/cardrag_worker/corpus_diff.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/corpus_diff.py)
  - 이전 seed 세대와 현재 세대 간의 차이를 정량 분석하는 `build_corpus_diff` 및 `write_corpus_diff` 구현.
  - 카테고리: `unchanged`, `same_source_revision`, `successor_source`, `new_product`, `historical_maintained`, `retired_lineage`.
- [apps/cardrag-worker/src/cardrag_worker/pipeline.py](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py)
  - `pdf_cache_force_revalidate` 옵션 지원: 168시간 캐시 TTL보다 우선하여 모든 current 소스를 조건부 헤더(If-None-Match, If-Modified-Since) 또는 신규 다운로드로 재검증.
  - Acquisition 단계 완료 후 `corpus-diff.json`을 아티팩트로 기록.

### 2.4 배포 Compose 설정 변경 (`compose.yaml`, `compose.candidate.yaml`)
- [deploy/worker/compose.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/worker/compose.yaml) / [deploy/worker/compose.candidate.yaml](file:///home/lee/projects/MCP_card_prd_detail/deploy/worker/compose.candidate.yaml)
  - 기본 OCR Provider를 `local-paddleocr`, Model을 `PaddleOCR-VL-1.6`으로 지정.
  - `CARDRAG_EXTERNAL_OCR_ALLOWED: "false"`, `CARDRAG_PDF_CACHE_FORCE_REVALIDATE: "true"` 설정.
  - `cardrag-worker-paddleocr-models` 볼륨 마운트 연동.
  - 로컬 candidate 검증을 위해 `pull_policy: ${CARDRAG_PULL_POLICY:-always}` 지원.

---

## 3. 검증 및 테스트 수행 결과

### 3.1 정적 분석 및 린트 검사
- `ruff check`:
  ```bash
  uv run ruff check apps/cardrag-worker
  # 결과: All checks passed!
  ```
- `mypy`:
  ```bash
  uv run mypy apps/cardrag-worker/src
  # 결과: Success: no issues found in 44 source files
  ```

### 3.2 단위 및 통합 테스트 스위트
- 신규 구현 단위 테스트 (99 passed):
  ```bash
  uv run pytest apps/cardrag-worker/tests/test_state_seed_v122.py \
                apps/cardrag-worker/tests/test_corpus_diff.py \
                apps/cardrag-worker/tests/test_cli_settings_provider.py
  # 결과: 99 passed in 2.21s
  ```
- Worker 전체 테스트 (1,073 passed):
  ```bash
  uv run pytest apps/cardrag-worker
  # 결과: 1073 passed, 6 warnings in 19.62s
  ```
- 저장소 전체 통합 테스트 스위트 (2,204 passed):
  ```bash
  uv run pytest
  # 결과: 2204 passed in 39.53s (이전 기준 2,196 passed 대비 +8건 신규 테스트 통과)
  ```

---

## 4. 실제 Production Seed 적용 결과

새로운 상태 볼륨 `cardrag-worker-v122-candidate-state-r2`에 v1.0.28 성공 상태 볼륨 `cardrag-worker-v114-candidate-state`(read-only 마운트)의 seed를 적용했습니다.

### 4.1 적용 명령
```bash
docker run --rm \
  -e CARDRAG_CHANNEL=candidate-v1.0.11 \
  -e CARDRAG_WORKER_STATE_DIR=/var/lib/cardrag-worker \
  -v cardrag-worker-v114-candidate-state:/source:ro \
  -v cardrag-worker-v122-candidate-state-r2:/var/lib/cardrag-worker:rw \
  cardrag-worker:v1.0.29-candidate seed-state-v122 /source \
  --generation-id g-bd9a4c513041462886d347af-9b84c2e38c14 \
  --expected-documents 5192 \
  --apply
```

### 4.2 실행 출력 결과 (JSON)
```json
{
  "accepted_ocr_documents": 5192,
  "accepted_pdf_objects": 4710,
  "accepted_revisions": 5208,
  "accepted_sources": 5203,
  "adopted_ocr_count": 1510,
  "applied": true,
  "dry_run": false,
  "generation_id": "g-bd9a4c513041462886d347af-9b84c2e38c14",
  "idempotence_imported_ocr_files": 0,
  "idempotence_imported_pdf_objects": 0,
  "idempotence_imported_revisions": 0,
  "idempotence_verified": true,
  "imported_ocr_files": 8874,
  "imported_pdf_objects": 4710,
  "imported_revisions": 5208,
  "ledger_path": "audit-reports/state-seed/15fe2504454553bc08a6a8621bbf48ceb325a7a559e2544cd52a509bbc871f81.json",
  "ledger_sha256": "15fe2504454553bc08a6a8621bbf48ceb325a7a559e2544cd52a509bbc871f81",
  "ledger_size_bytes": 3807912,
  "native_ocr_count": 3682,
  "prior_current_count": 5044,
  "prior_historical_count": 148,
  "reused_ocr_files": 0,
  "reused_pdf_objects": 0,
  "reused_revisions": 0,
  "run_id": "bd9a4c513041462886d347afd5c46c47",
  "schema_version": "cardrag.state-seed-report.v1",
  "source_database_sha256": "bb9e878c40b34a3dbfeb8cfa7d677309e511ae85dfef9ae46f52a07c9d43e72c",
  "status": "applied"
}
```

---

## 5. 후보 Worker 실행 및 기동 검증

### 5.1 증거 보존 확인
- 실패 컨테이너: `cardrag-v122-candidate-worker-v1029` (Status: `Exited (1)`, 삭제 없이 보존됨)
- 실패 상태 볼륨: `cardrag-worker-v122-candidate-state` (보존됨)

### 5.2 신규 후보 Worker 실행
신규 상태 볼륨 `cardrag-worker-v122-candidate-state-r2` 및 사전 탑재된 `cardrag-worker-paddleocr-models`를 바인딩하여 detached로 기동했습니다.

```bash
CARDRAG_WORKER_STATE_VOLUME=cardrag-worker-v122-candidate-state-r2 \
CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST=sha256:56fe65eb737d35260958ddc733de75354f11306515bf666dea5976c0e411f8b1 \
CARDRAG_CANDIDATE_IMAGE_REPOSITORY=cardrag-worker \
CARDRAG_PULL_POLICY=never \
docker compose --project-directory /home/lee/projects/MCP_card_prd_detail/deploy/worker \
  --env-file /etc/cardrag/worker.env \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  run -d --name cardrag-v122-candidate-worker-v1029-r2 worker run
```

### 5.3 기동 즉시 무결성 검증 (Acceptance Criteria)
1. **컨테이너 실행 상태**: `fa5a4155bc47` (`Status=running Running=true`)
2. **이미지 Digest 일치**: `cardrag-worker@sha256:56fe65eb737d35260958ddc733de75354f11306515bf666dea5976c0e411f8b1`
3. **환경 변수 안전 정책**:
   - `CARDRAG_OCR_PROVIDER`: `local-paddleocr`
   - `CARDRAG_OCR_MODEL`: `PaddleOCR-VL-1.6`
   - `CARDRAG_EXTERNAL_OCR_ALLOWED`: `false`
   - `CARDRAG_PDF_CACHE_FORCE_REVALIDATE`: `true`
   - `CARDRAG_STABLE_PUBLICATION_APPROVED`: `false`
   - `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED`: `false`
4. **Read-only Rootfs**: `ReadonlyRootfs=true`, `/tmp` tmpfs 격리
5. **Seed Ledger 탑재 확인**: `/var/lib/cardrag-worker/audit-reports/state-seed/15fe2504454553bc08a6a8621bbf48ceb325a7a559e2544cd52a509bbc871f81.json` 정상 마운트
6. **초기 기동 로그**:
   ```
   2026-09-25 09:55:46,183 INFO cardrag_worker.cli Worker startup capacity preflight passed filesystem_free_bytes=91758374912 minimum_free_bytes=34359738368
   2026-09-25 09:55:46,811 INFO cardrag_worker.cli OCR discovered 4 compatible contracts: gpt-5.6-sol, gpt-5.4, gpt-5.6-terra, anthropic/claude-sonnet-5
   2026-09-25 09:55:46,811 INFO cardrag_worker.cli Remote OCR cache access mode=read-only require_hit=False
   ```

---

## 6. 계획 대비 편차 및 참고 사항 (Deviations & Hand-off)

1. **Manifest Schema 유연성**:
   - PLAN.md에서는 `cardrag.worker-manifest.v1`를 기본으로 명시했으나, 실제 운영 기준인 v1.0.28 generation seal(`publish.json`)은 `cardrag.generation.v6` 스키마를 사용합니다. 두 버전 모두 지원하도록 확장하여 무결성을 확보했습니다.
2. **외래키 삽입 순서**:
   - `pdf_cache_source(superseded_by_source_id)`의 자기참조 외래키 제약조건 위반을 방지하기 위해 2단계(INSERT 후 일괄 UPDATE) 방식을 적용했습니다.
3. **장시간 작업 모니터링 원칙**:
   - PLAN.md의 지침("장시간 Worker를 계속 모니터링하지 않는다. 기동 확인 후 대기하고 사용자가 완료를 알리면 종료 코드, terminal result, corpus-diff, OCR/provider 지표와 불변성 증거를 확인한다")에 따라, detached로 정상 가동 및 필수 사전 검증을 완료한 상태에서 본 보고서를 작성합니다.

---

## 7. 결론

PLAN.md에 명시된 모든 구현, 단위·통합 검증, v1.0.28 상태 및 OCR 시드 복구, 멱등성 검증, 실패 증거 보존, 후보 Worker 컨테이너의 안전한 기동이 결함 없이 완료되었습니다.
Worker 실행이 완료되면 종료 코드, `corpus-diff.json`, 터미널 결과 및 수량 일치 여부(최종 current 5,045 / historical 161 / 신규 PaddleOCR 14건 / 외부 OCR 0건)를 최종 확인하여 릴리스 단계를 마무리할 수 있습니다.
