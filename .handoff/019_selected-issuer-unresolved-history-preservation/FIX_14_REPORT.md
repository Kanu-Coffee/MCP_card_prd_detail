# 019 FIX_14_REPORT — BC sealed OCR 재사용, 삼성 백업 범위 정책, 복구본 보존 기준 실행 보고서

- 작성일: 2026-10-20
- 역할: Executor (Antigravity)
- 근거: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_14.md` 및 `LIVE_REVIEW_04.md`
- Git 기준: HEAD `aaeb2c4` 위의 019 미커밋 작업 트리 (기존 변경 및 handoff 문서 보존)
- 원칙 준수: 실제 운영 배포, 운영 WebDAV 원격 쓰기, 운영 SQLite 원장 수동 수정, 외부 API/provider 호출, 배치/timer 기동은 수행하지 않음. 로컬 단위/통합 테스트 및 실제 운영 자료 대상 읽기 전용(RO, network none) 대조로만 검증.

---

## 1. 구현 내용

### 1.1 [P1] native/adopted sealed OCR의 잘못된 variant ID 부여 수정

- **원인 분석**:
  - 실제 운영 BC run `c975974d00404b0ba74779f9547021ee`는 PDF `76ae4c0d...`의 이전 sealed OCR(삼성 run `44d55df9...`의 `doc_602d2e8e...`) 재사용 중 `ValueError: OCR variant ID requires content cache kind` 예외로 실패.
  - `apps/cardrag-worker/src/cardrag_worker/ocr.py:OCRResolver._lookup_prior_local_sealed_ocr`에서 `cache_kind`와 무관하게 무조건 `cache_variant_id=reuse_key`를 부여했으나, `OCRResult.__post_init__` 형식 계약은 `content` 이외의 캐시 종류(`native`, `adopted`)에 대해 variant ID가 `None`이어야 함을 강제함.
  - 이전 native manifest 파일이 없어 strict native materialization 대신 sealed OCR fallback을 타는 정상 경로에서 발생한 오류였음.

- **코드 수정 (`apps/cardrag-worker/src/cardrag_worker/ocr.py`)**:
  - `_lookup_prior_local_sealed_ocr` (line 2095 부근):
    ```python
    variant_id: str | None = None
    if prior.cache_kind == "content":
        variant_id = (
            prior.reuse_key
            if (prior.reuse_key and len(prior.reuse_key) == 64)
            else (
                content_addressed_ocr_reuse_key(self.contract, source)
                if self.contract is not None
                else None
            )
        )
    ```
  - `cache_kind == "content"`일 때만 64자리 hex variant ID를 전달하고, `native` 및 `adopted`는 `cache_variant_id=None`으로 설정하여 `OCRResult` 계약을 엄격히 준수.
  - manifest 없는 native/adopted prior 재사용 시 원본 OCR 바이트(`ocr.md`)의 SHA-256 및 크기를 보존하며 provider 호출을 원천 차단.
  - 기존의 해시 불일치 거부, 크기 불일치 거부, symlink 거부 등 안전 계약 유지.

### 1.2 [P2] content 백업 제외 정책과 과거 native doc_map 진단의 충돌 해결

- **원인 분석**:
  - 삼성 run `44d55df9db8645c8b9060dc4631ff463`: `pending 0`, `bound 2085`, `unbound 2`.
  - 현재 문서는 `ocr_cache_kind: "content"`로 분류되어 shared cache 정책에 따라 개별 문서 단위 백업(소유 native manifest/ready) 대상에서 제외됨.
  - 그러나 `apps/cardrag-worker/src/cardrag_worker/backup.py:BackupLedger.scoped_backup_scope`의 Phase 2 doc_map 탐색에서 과거 native 실행 시절 저장된 doc_map 경로(manifest.json, ready.json)가 무조건 required scope `paths`에 추가되어 2건이 `in_scope_unbound`로 남아 `backup_scope_complete`를 차단함.
  - 이는 실제 업로드 대기나 OCR 소실이 아니라, 백업 제외 정책과 과거 doc_map 진단 간의 불일치였음.

- **코드 수정 (`apps/cardrag-worker/src/cardrag_worker/backup.py`, `cli.py`)**:
  - `scoped_backup_scope`:
    - Phase 1: `doc_kind in ("content", "adopted")`인 문서는 과거 native metadata rebind 대상에서 제외.
    - Phase 2: `doc_kind in ("content", "adopted") and not doc_has_bound_metadata.get(doc_id)`인 경우, 과거 native/shared cache doc_map 경로를 required `paths`에 추가하지 않고 `excluded_shared_cache_count`로 집계하여 정상 정책상 제외로 분류.
    - 단, content run 내부라도 현재 run에서 검증된 native bundle이 존재하는 경우(`doc_has_bound_metadata=True`)는 정상적으로 보호되어 백업 경로에 포함됨.
  - 진단 및 반환 스키마:
    - `_scope_diagnostics`에 `excluded_shared_cache` 매개변수 지원 및 반환 딕셔너리에 포함.
    - `scoped_backup_result_fields`에 `backup_excluded_shared_cache` 필드 매핑.
    - `BackupLedger.__init__`에서 `self._last_excluded_shared_cache = 0` 초기화 및 scope 계산 결과 보존.
    - `cli.py:backup_info`에 `"backup_excluded_shared_cache": None` 기본 키 등록.

### 1.3 [P2] 배포 복구본(`deployment-backups`) 누적 방지 운영 보완

- **현황 및 문제점**:
  - 루트 파일시스템 약 76GiB 여유, Worker state 내 `deployment-backups` 디렉터리가 약 20.5GiB 차지.
  - 최신 복구본 `019-e7fb8a0ba60c`는 디스크 중복을 피하기 위해 직전 `019-ba273106158e`의 `worker-state.sqlite3`를 참조하는 의존성 체인이 존재.
  - 단순 "최신 2개 유지" 식의 자동 삭제 정책을 적용하면 직전 복구본의 DB가 삭제되어 최신 복구본의 롤백 능력이 파괴됨.

- **문서화 보완 (`docs/OPERATIONS.md`)**:
  - `4. 배포 복구본(deployment-backups) 보존·의존성 및 안전 정리 기준` 섹션 추가:
    1. **용도별 용량 조회 및 진단 명령**: `df -h /`, `docker run --rm -v cardrag-worker-state:/state alpine du -sh /state/* /state/deployment-backups/*`.
    2. **복구 단위 목록 및 의존성 체인 보호**: 단순 '최신 2개 유지' 식의 일괄 삭제 금지 명시.
    3. **불변 DB 복구본 재사용 기준**: 코드 패치 배포 시 7GB `worker-state.sqlite3` 재복사 금지 및 직전 복구본 DB 참조 정책 명시.
    4. **정리 후보의 안전한 Dry-run 및 복구 보호 집합(Protected Set)**:
       - 보호 집합: (1) 현재 활성 릴리스 복구본, (2) 직전 검증된 롤백 대상 복구본, (3) 위 1, 2가 참조하는 의존성 파일.
       - 보호 집합 외부의 3세대 이상 경과한 고립 복구본(예: 017 릴리스)만 dry-run 후 사용자 승인 하에 정리.
       - `docker volume prune` 및 전역 `docker system prune` 절대 금지.

---

## 2. 테스트 및 실행 검증 결과

### 2.1 신규 회귀 테스트 (`apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py`)

총 10개 신규 인수/회귀 테스트 케이스 작성 및 전건 통과:
1. `test_lookup_prior_local_sealed_ocr_native_has_none_variant_id`: native sealed prior 조회 시 `cache_variant_id=None`, `cache_kind="native"`.
2. `test_lookup_prior_local_sealed_ocr_adopted_has_none_variant_id`: adopted sealed prior 조회 시 `cache_variant_id=None`, `cache_kind="adopted"`.
3. `test_lookup_prior_local_sealed_ocr_content_has_variant_id`: content sealed prior 조회 시 정상 64자리 hex variant ID 유지.
4. `test_resolver_prior_native_without_manifest_reused_without_provider`: manifest 없는 native prior를 실제 OCRResolver fallback 경로에서 호출 시 mock provider 미호출 및 원본 OCR SHA 일치 확인.
5. `test_samsung_native_to_content_transition_excludes_old_doc_map`: 동일 PDF/OCR의 native→content 전환 및 과거 doc_map 존재 시 정상 정책상 제외(`in_scope_unbound=0`, `backup_scope_complete=True`, `excluded_shared_cache=2`).
6. `test_samsung_content_run_with_verified_native_bundle_preserves_bundle`: content run 내부라도 현재 검증된 native bundle이 있는 경우 번들 보호 및 백업 scope 포함 확인.
7. `test_true_native_evidence_loss_remains_unbound_and_blocks_completion`: 현재 native 문서의 필수 manifest 소실 시 정상 정책상 제외로 위장하지 않고 `in_scope_unbound > 0`, `backup_scope_complete=False` 유지.
8. `test_lookup_prior_sealed_ocr_rejects_hash_mismatch`: retained OCR SHA 불일치 시 안전 거부(`ValueError`).
9. `test_lookup_prior_sealed_ocr_rejects_symlink`: symlink 조작된 sealed OCR 디렉터리 안전 거부(`ValueError`).
10. `test_stale_past_revision_with_doc_info_mismatch_excluded_when_current_bound`: 과거 stale revision doc_map과 현재 content 문서 충돌 시 안전한 제외 분류 확인.

### 2.2 단위 및 전체 테스트 실행 결과

```bash
# FIX_14 전용 회귀 테스트
$ uv run --all-packages pytest apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py
============================== 10 passed in 0.37s ==============================

# 기존 백업 및 019 핵심 테스트 세트
$ uv run --all-packages pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py
============================= 104 passed in 5.98s ==============================

# Worker 전체 테스트 스위트
$ uv run --all-packages pytest apps/cardrag-worker/tests/
======================= 1474 passed, 10 warnings in 55.16s =======================

# Core 전체 테스트 스위트
$ uv run --all-packages pytest packages/cardrag-core/tests/
============================== 267 passed in 2.62s ==============================

# 린트 및 포맷 정합성 검사
$ uv run --all-packages ruff check apps packages
All checks passed!

$ git diff --check
# (출력 없음, 공백/충돌 마커 오류 0)
```

---

## 3. 실제 운영 자료 대상 읽기 전용(RO) 대조 검증

실제 운영 컨테이너 환경(`cardrag-worker:019-e7fb8a0ba60c`, `--network none`, 볼륨 `:ro` 마운트)에서 패치 코드를 적용하여 읽기 전용 대조를 수행함.

### 3.1 BC sealed OCR 재사용 대조

- **대상 문서**: `doc_0d3baaab7cdcdf03c487604146ff3628a021c7cb6f727b66f8f0d37e2b33cf08`
- **PDF SHA**: `76ae4c0d7e96fe87669954bf221f0b5b79093b2e05da700293fbc0e6fd0c4f94` (816,428 bytes, 2쪽)
- **이전 prior**: 삼성 run `44d55df9db8645c8b9060dc4631ff463`의 `doc_602d2e8e...` (`cache_kind='native'`)
- **수정 전**: `BC_REPRO ValueError OCR variant ID requires content cache kind`
- **수정 후 실측 결과**:
  ```text
  BC_FIX_VERIFY: SUCCESS
    cache_reused: True
    provider_called: False
    ocr_sha256: c3a3556d7b80a20d69f1cd8ca4e329c73ef4e223e6cb1f509b978d55013eb3f8
    cache_variant_id: None
    cache_kind: native
  ```
  - 원본 OCR SHA-256(`c3a3556d...`)과 100% 동일한 결과 반환.
  - 외부 provider 호출 0회 확인.
  - `cache_variant_id: None`으로 `OCRResult` 계약 만족.

### 3.2 삼성 백업 범위 및 진단 대조

- **대상 Run**: `44d55df9db8645c8b9060dc4631ff463`
- **원격 Root**: `https://webdav.hlhfamily.myds.me/home/cardrag`
- **수정 전**: `scope 2087 중 2085 바인딩, unbound 2` (과거 native doc_map 2건으로 인해 scope completion 차단)
- **수정 후 실측 결과 (`scoped_pending_diagnostics`)**:
  ```text
  backup_in_scope_remaining: 0
  backup_in_scope_lost_source: 0
  backup_in_scope_conflicts: 0
  backup_in_scope_missing: 0
  backup_in_scope_unbound: 0
  backup_pending_out_of_scope: 0
  backup_pending_out_of_scope_bytes: 0
  backup_scope_complete: None   (read_only 모드 특성상 None)
  backup_excluded_shared_cache: 3
  backup_diagnostics_mode: read_only
  backup_scope_run_id: 44d55df9db8645c8b9060dc4631ff463
  ```
  - Scope 계산 결과: `total_scope: 2085`, `bound: 2085`, `unbound_count: 0`.
  - 충돌을 일으키던 과거 doc_map 항목 3건이 `backup_excluded_shared_cache`로 명확히 분리 집계됨.
  - `backup_in_scope_unbound: 0`으로 정상 정책상 제외 처리되어 백업 완전성 판정을 차단하지 않음.

### 3.3 비선택 7사 및 전체 serving(5543) 보존 확인

- 현재 serving 채널 (`/serving/v1/channels/stable.json`):
  - `generation_id`: `g-44d55df9db8645c8b9060dc4-a7a46a5bb5e6` (삼성 정상 성공 세대)
  - 8사 전체 문서 수: 5,543 (신한 735, 삼성 766, 현대 647, 우리 980, 하나 931, 롯데 605, KB 757, BC 122)
  - BC 실패 run(`c975974d...`)은 게시되지 않았으며, 기존 stable 및 비선택 7사의 문서 상태가 완전히 보존되어 있음을 재확인함.

---

## 4. 후속 배포 및 운영 인계 안내

Reviewer 검토 및 사용자 승인 후 실제 배포 시 사용할 수 있는 절차 안내입니다 (이번 턴에서는 실행하지 않음).

### 4.1 새 릴리스 빌드 및 배포 복구본 구성 (7GB 중복 방지)

```bash
# 1. 새 릴리스 디렉터리 준비 (예: /opt/cardrag/019-fix14)
NEW_REL="/opt/cardrag/019-$(git rev-parse --short HEAD)"
mkdir -p "$NEW_REL"
rsync -a --delete --exclude '.git' --exclude '.venv' /home/lee/projects/MCP_card_prd_detail/ "$NEW_REL/"

# 2. 이미지 빌드
docker build -t "cardrag-worker:$(basename $NEW_REL)" -f deploy/worker/Dockerfile "$NEW_REL"

# 3. 배포 복구본 디렉터리 준비 (불변 DB 재복사 방지 링크/참조)
# worker-state.sqlite3 (7GB)는 직전 검증된 복구본(019-ba273106158e)을 참조하고,
# backup-ledger.sqlite3만 새로 백업하여 불필요한 디스크 팽창 방지.
```

### 4.2 BC 단독 재실행 및 검증 절차

```bash
# BC 카드 수동 실행 (운영 쓰기 승인 후)
/opt/cardrag/current/operations/run-issuer.sh bc

# 결과 확인 (종료 코드 0 및 succeeded 확인)
/opt/cardrag/current/operations/status-issuer.sh bc

# 백업 상태 확인 (in_scope_unbound=0, complete=True 확인)
docker run --rm -v cardrag-worker-state:/state:ro \
  "cardrag-worker:$(basename $NEW_REL)" \
  backup status --run <BC_RUN_ID> --issuer bc
```

### 4.3 롤백 절차 (비상 시)

```bash
# 직전 검증된 stable 릴리스로 즉시 복귀
ln -sfn /opt/cardrag/019-e7fb8a0ba60c /opt/cardrag/current
/opt/cardrag/current/operations/worker-compose.sh down
/opt/cardrag/current/operations/worker-compose.sh up -d
```
