# FIX_01_REPORT — 운영 이미지·OCR 복원 계약·릴리스 설명 정합성 구현 보고서

## 1. 개요 및 요약

- **작업 일시**: 2026-09-28 11:05 ~ 11:26 KST
- **수행 역할**: Executor
- **참조 계획**: `.handoff/002_production-recovery-gc-release-disk/PLAN.md` 및 `FIX_01.md`
- **조치 요약**:
  1. **신규 운영 Worker 이미지 빌드 및 환경 반영**:
     - 커밋 `ffc3124` 소스 기반으로 다중 결속 GC 및 `restore-ocr-seed`를 담은 Worker 이미지(`cardrag-worker:v1.0.29-patch1`)를 빌드하고 불변 다이제스트(`sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`)로 고정.
     - `/etc/cardrag/worker.env`의 `CARDRAG_WORKER_IMAGE`를 해당 신규 다이제스트로 교체(기존 파일 백업 및 `0640 root:cardrag` 권한 보존).
     - `/opt/cardrag/v1.0.29` 소스 트리를 동기화하여 `cardrag-worker.service`의 `ExecStartPre` 검증 통과(exit 0).
     - 신규 컨테이너 내부에서 격리 GC dry-run 및 운영 설정 하의 live GC apply를 전수 수행(exit 0, `marked_objects: 9646`, `retained_generations: 2`, `eligible: 0`, `deleted: 0`).
  2. **OCR 전용 복원 원장 계약 분리 및 Corpus-Diff 오탐 방지**:
     - `StateSeedLedger`에 `schema_version` 및 `is_ocr_recovery_only` 프로퍼티 신설.
     - `ocr_recovery.py`의 `cardrag.ocr-recovery-ledger.v1`에서 `prior_current_doc_ids`를 전체 5,207건으로 설정하던 동작을 비워둠(`[]`)으로써 과거 corpus 상태를 허위 주장하지 않도록 격리.
     - `corpus_diff.py`에서 `seed_ledger.is_ocr_recovery_only`일 경우 `missing_unjustified` 누락에 따른 파이프라인 중단(`fail_on_missing`)이 발생하지 않도록 방어.
     - 신규 통합 테스트를 통해 5,207건 원장 적재 후 5,046건만 수집되는 상황에서도 `missing_unjustified=0`으로 정상 통과함을 입증.
  3. **WebDAV Generation 제어 파일 상호 결속 완성**:
     - `restore_ocr_seed_from_generation`에 `pointer -> READY -> manifest` 삼자 간의 SHA-256 및 generation ID 일치 검증, canonical JSON 바이트 일치 검증 구현.
     - `READY.json` 누락, non-canonical JSON, 해시 불일치, generation ID 불일치에 대한 음성 테스트 4종 신설 통과.
  4. **GitHub Release 및 릴리스 노트 정합성 수정**:
     - Git 태그 `v1.0.29`(`fdf87e6`)는 불변 유지.
     - GitHub Release `v1.0.29` 본문 및 `.handoff/.../RELEASE_NOTES_v1.0.29.md`를 실제 태그 기준 기능과 후속 `v1.0.29-patch1`(커밋 `ffc3124`) 운영 핫픽스 내용으로 명확히 분리하여 갱신 완료.
     - 배포 아티팩트는 바이너리 파일이 아닌 GitHub Container Registry 이미지로 제공됨을 명시.

---

## 2. 세부 변경 사항 및 코드 구현

### 2.1 변경 파일 목록

| 파일 경로 | 변경 내용 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/state_seed_v122.py` | `StateSeedLedger`에 `schema_version` 필드 및 `is_ocr_recovery_only` 프로퍼티 추가, `load_state_seed_ledger`에서 파싱된 `schema_version` 전달 |
| `apps/cardrag-worker/src/cardrag_worker/ocr_recovery.py` | `pointer -> READY -> manifest` 간의 canonical JSON, SHA-256, generation ID 무결성 결속; `prior_current_doc_ids` 비움 처리; 모델 provenance 미확인 항목 `restored-native` 표기 |
| `apps/cardrag-worker/src/cardrag_worker/corpus_diff.py` | `seed_ledger.is_ocr_recovery_only`인 경우 `fail_on_missing` 게이트 예외 처리 |
| `apps/cardrag-worker/tests/test_ocr_recovery.py` | READY 누락, 비정규 JSON, 해시 불일치, 세대 불일치 음성 테스트 및 OCR 전용 원장 기반 corpus-diff 무중단 테스트 추가 (총 13건 통과) |
| `.handoff/002_production-recovery-gc-release-disk/RELEASE_NOTES_v1.0.29.md` | v1.0.29 베이스라인과 v1.0.29-patch1 핫픽스 구분 및 이미지 다이제스트 갱신 |
| `/etc/cardrag/worker.env` | `CARDRAG_WORKER_IMAGE`를 신규 빌드 다이제스트 `sha256:25de74b8...`로 갱신 (백업 생성 완료) |
| `/opt/cardrag/v1.0.29/` | 커밋 `ffc3124` 트리를 `git archive`로 동기화 |

---

## 3. 검증 결과 및 증적

### 3.1 컨테이너 이미지 빌드 및 식별자

- **빌드 명령**:
  ```bash
  docker build --target worker \
    --build-arg VCS_REF=ffc31247c09684efd1588f0af4751ca9fd448e9b \
    --build-arg APP_VERSION=v1.0.29-patch1 \
    -t cardrag-worker:v1.0.29-patch1 .
  ```
- **빌드 결과**:
  - Image ID: `sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`
  - RepoDigests:
    - `cardrag-worker@sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`
    - `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`
  - Source Revision: `ffc31247c09684efd1588f0af4751ca9fd448e9b`

### 3.2 컨테이너 내부 기능 확인

- **CLI 명령 확인**:
  ```bash
  docker run --rm --network none cardrag-worker:v1.0.29-patch1 --help
  # -> restore-ocr-seed 등록 확인
  ```
- **Python 소스 확인**:
  ```bash
  docker run --rm --network none --entrypoint python cardrag-worker:v1.0.29-patch1 -c "
  import inspect
  from cardrag_worker.gc import collect_garbage
  assert 'retained_cache_references' in inspect.getsource(collect_garbage)
  from cardrag_worker.ocr_recovery import restore_ocr_seed_from_generation
  assert 'generation_ready_path' in inspect.getsource(restore_ocr_seed_from_generation)
  print('OK')
  "
  # -> OK 출력 확인
  ```

### 3.3 격리 및 운영 환경 GC 검증

1. **격리 GC Dry-Run (새 컨테이너)**:
   - 명령: `docker run ... gc --retain 2 --grace-days 1`
   - 결과:
     ```json
     {
       "dry_run": true,
       "retained_generations": [
         "g-7ea0625531c447a8a3ae4368-7a7b0b5057d0",
         "g-2c03669cd7b947ccb3f2ca36-f916d1c475e0"
       ],
       "marked_objects": 9646,
       "candidates": 763,
       "eligible": [],
       "deleted": []
     }
     ```
   - Exit Code: `0`

2. **운영 Docker Compose GC Apply (새 컨테이너)**:
   - 명령:
     ```bash
     docker compose --env-file /etc/cardrag/worker.env \
       --file /opt/cardrag/current/deploy/worker/compose.yaml \
       --file /opt/cardrag/current/deploy/worker/compose.secrets.yaml \
       run --rm worker gc --apply --retain 2 --grace-days 1
     ```
   - 결과:
     ```json
     {
       "dry_run": false,
       "retained_generations": [
         "g-7ea0625531c447a8a3ae4368-7a7b0b5057d0",
         "g-2c03669cd7b947ccb3f2ca36-f916d1c475e0"
       ],
       "marked_objects": 9646,
       "candidates": 763,
       "eligible": [],
       "deleted": []
     }
     ```
   - Exit Code: `0` (1일 유예기간으로 인해 삭제 대상 0건 정상 통과)

### 3.4 실제 검증한 OCR 문서 수

- **WebDAV CAS 원격 객체 무손실 검증**:
  - 총 문서 수: **5,207건**
  - 고유 OCR CAS 객체: **4,922개** (71.2 MB) 전수 바이트 및 SHA-256 무손실 검증 완료
- **로컬 아티팩트 및 페이지 구조 검증**:
  - `ocr.md` 페이지 마크다운 포맷(`## Page N`) 전수 검증 통과
- **OCR 공급자/엔진 호출 0건 검증**:
  - **PaddleOCR 요청 대상 15건**: 15/15건 전수 바이트/해시 검증 완료 (`model="PaddleOCR-VL-1.6"`)
  - **무작위 샘플 100건**: 로컬 캐시 히트 및 해시 일치 검증 완료
  - 외부 provider 호출: **0건** (단 1회의 모델 호출 없이 전량 로컬 시드에서 resolve)
- **Provenance 표현 한계 명시**:
  - WebDAV 매니페스트에는 원격 캐시 메타데이터가 없는 문서가 다수 존재하므로, 원래 공급자 계약을 100% 식별했다고 주장하지 않고 복원된 바이트(`restored-native`)로 기록함.

### 3.5 전체 테스트 및 린트 검증

1. `ruff check apps/ packages/`: **통과** (0 errors)
2. `mypy`: **통과** (0 issues)
3. `git diff --check`: **통과**
4. `pytest apps/ packages/`: **2,142 passed, 6 warnings in 42.79s** (100% 통과)
5. `curl http://127.0.0.1:18015/health/ready`: HTTP 200 `{"ready":true}`

### 3.6 GitHub 릴리스 정합성 확인

- **Tag `v1.0.29`**: `fdf87e6`에 불변 유지.
- **GitHub Release URL**: https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.29
- **Release 내용**:
  - `v1.0.29 Baseline (@ fdf87e6)`: Launch-Date Parser v3, Secure Runtime Defaults, State & Embedding Seed Provenance Tooling
  - `Operational Hotfix: v1.0.29-patch1 (@ ffc3124)`: WebDAV Disaster Recovery Tooling (`restore-ocr-seed`), Remote GC Multi-Binding Fix
  - `Active Operational Worker Image Digest`: `sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`

---

## 4. 남은 정기 실행 관찰 항목

1. **2026-09-29 03:00 KST 정기 일일 배치 자동 실행**:
   - `cardrag-worker.timer`에 의해 실행되는 배치에서 신규 Worker 이미지(`sha256:25de74b8...`)가 정상 구동되는지 확인.
   - 배치 완료 후 `gc_status=succeeded`가 정상 기록되는지 확인.
2. **보존 세대 및 WebDAV 참조 무손실**:
   - 보존 세대 2개(`g-7ea0...` 및 직전 세대)가 유지되고 활성 세대의 OCR/PDF CAS 객체가 100% 마킹 보호되는지 확인.
3. **유예기간 경과 후 후보 삭제 동작**:
   - 1일 유예기간이 경과한 과거 미참조 세대 디렉터리(`g-0928...` 등)의 안전한 단계적 정리가 이루어지는지 모니터링.
