# 019 FIX_15_REPORT — content 문서 소유 백업 보호, 정식 복구 판정 및 실행 가능한 운영 인계

- 작성일: 2026-10-20
- 역할: Executor (Antigravity)
- 근거: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_15.md` 및 `REVIEW_04.md`
- Git 기준: HEAD `aaeb2c4` 위의 019 미커밋 작업 트리 (기존 변경 및 과거 handoff 문서 보존)
- 원칙 준수: 실제 운영 배포, 운영 WebDAV 원격 쓰기, 운영 SQLite 원장 수동 수정, 외부 API/provider 호출, 배치/timer 기동은 수행하지 않음. 로컬 단위/통합 테스트 및 실제 운영 자료 대상 읽기 전용(RO, network none) 대조로만 검증.

---

## 1. 구현 내용

### 1.1 [P1] shared-cache 제외와 현재 소유 metadata 소실을 엄격히 구분 (`backup.py`)

- **원인 분석**:
  - `backup.py`의 이전 Phase 1은 `doc_kind in ("content", "adopted")` 문서를 일괄 제외하여 로컬 캐시에 유효한 증거가 있어도 재바인딩을 건너뛰었음.
  - 이전 Phase 2는 `doc_has_bound_metadata == False`일 때 문서의 모든 태그를 shared cache로 분류하여, 현재 run 소유의 manifest가 소실/손상되었을 때 전송 대기 metadata를 `pending_out_of_scope`로 밀어내고 `backup_scope_complete=True`로 오판하는 심각한 false completion 위험이 있었음.
  - 반대로 현재 정상 번들이 있을 때는 번들과 다른 과거 키의 doc_map 태그가 scope에 추가되어 오탐 `unbound=2`가 발생했고, CAS 오브젝트(`v1/objects/...`)까지 제외 카운트에 합산되어 `excluded_shared_cache`가 5로 왜곡되었음.

- **코드 수정 (`apps/cardrag-worker/src/cardrag_worker/backup.py`)**:
  1. **Phase 1 (정규 증거 재바인딩)**:
     - `doc_kind`에 대한 일괄 차단을 제거하고, 로컬 캐시(`state_dir / "cache" / "ocr" / key`)에 현재 sealed 문서의 PDF SHA-256, OCR SHA-256, 크기, 페이지 수와 일치하는 검증된 native pair가 존재할 경우 캐시 종류와 무관하게 정규 증거로 재바인딩하여 `doc_has_bound_metadata[doc_id] = True` 및 `expected` 등록을 수행함 (`FIX_15.md` 요구 c).
  2. **Phase 2 (범위 가시성 및 shared cache 분리)**:
     - **CAS 오브젝트 보호**: `rel in paths`인 경우(PDF/OCR CAS 등) 이미 in-scope이므로 절대 제외 카운트에 넣지 않고 통과.
     - **Case 1 (Active Pending 보호)**: `rel in pending_all_remotes`(`backup_pending`에 status='pending'으로 대기 중인 항목)인 경우, 증거 소실 여부와 무관하게 무조건 `paths`에 유지. 증거가 소실되었다면 `expected`에 바인딩되지 않으므로 `in_scope_unbound > 0`, `in_scope_remaining > 0`으로 남아 `backup_scope_complete=False`로 차단함 (`FIX_15.md` 요구 b).
     - **Case 2 (기 바인딩 문서의 구형/초과 태그)**: `doc_has_bound_metadata == True`인 경우, 현재 문서의 필수 metadata는 이미 `expected`에 등록되었으므로 과거 키의 doc_map이나 superseded canonical 태그는 `excluded_shared_cache_paths`로 정상 제외하여 오탐 unbound를 방지함 (`FIX_15.md` 요구 a).
     - **Case 3 (미바인딩 문서의 태그)**:
       - `doc_kind == "native"`: 필수 native 증거 소실이므로 `paths`에 추가하여 `unbound`로 보고.
       - `doc_kind in ("content", "adopted")`: active pending이 없고 번들도 없는 과거 native doc_map은 정책상 shared cache로 분류하여 `excluded_shared_cache_paths`에 추가 (`FIX_15.md` 요구 d).
  3. **제외 카운트 및 진단 스키마 정합성**:
     - `excluded_shared_cache_paths -= paths`를 보장하여 실제 in-scope 경로와 제외 경로가 겹치지 않게 함.
     - `self._last_excluded_shared_cache_by_run[run_id]`를 통해 run별 제외 카운트를 격리.
     - `_scope_diagnostics`에서 `allowed_remote_paths is None`(unscoped flush)인 경우 `excluded_shared_cache`를 `None`으로 유지하여 허위 0 표기를 방지.

### 1.2 [P1 정정] OCR variant ID 계약 사실 정정 (`ocr.py`)

- `FIX_14_REPORT` 1.1절의 설명(예: prior.reuse_key를 variant로 주입)은 실제 구현과 차이가 있었으므로 이를 정정합니다.
- 실제 구현([`apps/cardrag-worker/src/cardrag_worker/ocr.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/ocr.py#L2090-L2115)):
  ```python
  variant_id: str | None = None
  if prior.cache_kind == "content":
      variant_id = (
          prior.variant_id
          if (prior.variant_id and len(prior.variant_id) == 64)
          else prior.ocr_sha256
      )
  ```
- `prior.cache_kind == "content"`일 때만 64자리 hex variant ID 또는 OCR SHA를 전달하고, `native`/`adopted`일 때는 반드시 `cache_variant_id=None`을 전달하여 [`OCRResult.__post_init__`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/ocr.py#L225) 계약을 준수합니다.

---

## 2. 테스트 및 실행 검증 결과

### 2.1 신규 및 확장 회귀 테스트 (`apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py`)

Reviewer의 4가지 검증 요구사항(a, b, c, d)을 포함하여 총 12개 테스트 작성 및 전건 통과:
1. `test_lookup_prior_local_sealed_ocr_native_has_none_variant_id`: native prior 시 variant_id=None, native 유지.
2. `test_lookup_prior_local_sealed_ocr_adopted_has_none_variant_id`: adopted prior 시 variant_id=None, adopted 유지.
3. `test_lookup_prior_local_sealed_ocr_content_has_variant_id`: content prior 시 64자리 hex variant ID 정상 부여.
4. `test_resolver_prior_native_without_manifest_reused_without_provider`: manifest 없는 native prior를 resolver fallback에서 provider 미호출 및 동일 OCR SHA로 재사용.
5. `test_samsung_native_to_content_transition_excludes_old_doc_map`: 과거 native doc_map 2건 정상 정책상 제외 (`unbound=0`, `excluded_shared_cache=2`, `complete=True`).
6. `test_samsung_content_run_with_verified_native_bundle_preserves_bundle`: **(요구 a)** 정상 번들 + 옛 태그 존재 시 오탐 unbound 없이 `scope=4, bound=4, unbound=0, excluded=2, backup_scope_complete=True`.
7. `test_samsung_content_run_with_lost_native_bundle_blocks_completion`: **(요구 b)** 번들 등록 후 manifest 소실 시 false completion 차단 (`scope=4, bound=2, unbound=2, pending_out_of_scope=0, in_scope_remaining=2, backup_scope_complete=False`).
8. `test_samsung_content_run_rebinds_from_local_cache`: **(요구 c)** run 디렉터리 manifest 소실 시 local cache의 유효한 증거로 Phase 1 재바인딩 성공 (`scope=4, bound=4, unbound=0, excluded=2, backup_scope_complete=True`).
9. `test_true_native_evidence_loss_remains_unbound_and_blocks_completion`: native 문서의 필수 cache 증거 소실 시 shared cache로 오판하지 않고 unbound로 차단.
10. `test_lookup_prior_sealed_ocr_rejects_hash_mismatch`: retained OCR SHA 불일치 시 안전 거부.
11. `test_lookup_prior_sealed_ocr_rejects_symlink`: symlink 디렉터리 안전 거부.
12. `test_stale_past_revision_with_doc_info_mismatch_excluded_when_current_bound`: stale revision 과거 doc_map 안전 제외.

### 2.2 전체 테스트 스위트 및 코드 품질 검사

```bash
# 1. 백업 및 019 핵심 테스트 세트 (106개 통과)
$ uv run --all-packages pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py
============================= 106 passed in 6.06s ==============================

# 2. Worker 및 Core 전체 테스트 스위트 (1,743개 통과)
$ uv run --all-packages pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/
====================== 1743 passed, 10 warnings in 57.35s ======================

# 3. 코드 포맷 검사
$ uv run --all-packages ruff format --check apps packages
220 files already formatted

# 4. 린트 검사
$ uv run --all-packages ruff check apps packages
All checks passed!

# 5. Git diff 공백/충돌 마커 검사
$ git diff --check
# (출력 없음, 오류 0)
```

---

## 3. 실제 운영 자료 대상 읽기 전용(RO) 대조 검증

실제 운영 컨테이너(`cardrag-worker:019-e7fb8a0ba60c`, `--network none`, 볼륨 `:ro` 마운트)에서 패치 코드를 오버레이하여 실측 검증을 수행함.

### 3.1 BC sealed OCR 재사용 대조

- **문서**: `doc_0d3baaab7cdcdf03c487604146ff3628a021c7cb6f727b66f8f0d37e2b33cf08` (PDF `76ae4c0d...`, 2쪽)
- **실측 결과**:
  ```text
  BC_FIX_VERIFY: SUCCESS
    cache_reused: True
    provider_called: False
    ocr_sha256: c3a3556d7b80a20d69f1cd8ca4e329c73ef4e223e6cb1f509b978d55013eb3f8
    cache_variant_id: None
    cache_kind: native
  ```
  - 원본 OCR SHA(`c3a3556d...`) 100% 보존.
  - 외부 provider 호출 0회.
  - variant_id=None으로 `OCRResult` 계약 준수.

### 3.2 삼성 백업 범위 및 진단 실측

- **대상 Run**: `44d55df9db8645c8b9060dc4631ff463`
- **원격 Root**: `https://webdav.hlhfamily.myds.me/home/cardrag`
- **실측 결과**:
  ```text
  SAMSUNG_SCOPE_VERIFY:
    total_scope: 2085
    bound: 2085
    unbound_count: 0
    unbound_items: []
    excluded_shared_cache: 2

  SAMSUNG_DIAG_VERIFY:
    in_scope_remaining: 0
    in_scope_lost_source: 0
    in_scope_conflicts: 0
    in_scope_missing: 0
    in_scope_unbound: 0
    pending_out_of_scope: 0
    pending_out_of_scope_bytes: 0
    excluded_shared_cache: 2
  ```
  - `total_scope: 2085`, `bound: 2085`, `unbound_count: 0`.
  - 이전 3개로 과대 집계되던 CAS 오브젝트가 정상 제외되어 `excluded_shared_cache`가 정확히 **2**(과거 native manifest, READY)로 집계됨.
  - `in_scope_unbound: 0`, `in_scope_remaining: 0`으로 scope completion 판정 차단 원인이 완전히 해소됨.

### 3.3 비선택 7사 및 전체 serving(5543) 보존 확인

- `/serving/v1/channels/stable.json`의 `generation_id`는 삼성 성공 세대(`g-44d55df9...`)를 유지하고 있으며, 전체 5,543개 문서(신한 735, 삼성 766, 현대 647, 우리 980, 하나 931, 롯데 605, KB 757, BC 122)가 무결하게 보존되어 있음을 재확인함.

---

## 4. [P2] 실제 배포 구성에 맞는 실행 가능한 운영 인계 절차

Reviewer 검토 및 사용자 승인 후 실제 배포 시 사용할 수 있도록 실제 환경 구성에 맞추어 작성된 절차입니다 (이번 턴에서는 실행하지 않음).

### 4.1 이미지 빌드 및 식별 (Source Tree Digest 기준)

저장소 루트의 [Dockerfile](file:///home/lee/projects/MCP_card_prd_detail/Dockerfile) (`--target worker`)을 정규 빌드에 사용하거나, 운영 디렉터리의 [code-image.Dockerfile](file:///opt/cardrag/current/operations/code-image.Dockerfile) 오버레이 절차를 사용합니다.

```bash
# 1. 소스 트리 SHA-256 식별 (미커밋 변경 구별)
TREE_SHA=$(git diff HEAD | sha256sum | cut -c1-12)
REL_TAG="019-$(git rev-parse --short HEAD)-${TREE_SHA}"

# 2. 이미지 빌드 (Root Dockerfile 기준)
docker build --target worker -t "cardrag-worker:${REL_TAG}" -f Dockerfile .

# 또는 운영 호스트의 code-image.Dockerfile 오버레이 방식 사용 시:
# docker build -t "cardrag-worker:${REL_TAG}" -f /opt/cardrag/current/operations/code-image.Dockerfile .
```

### 4.2 릴리스 활성화 (`operations/activate.sh`)

`/opt/cardrag/current/operations/activate.sh`를 통해 timer와 service의 중지 상태를 검증한 후 원자적 심볼릭 링크 교체를 수행합니다.

```bash
# 1. 새 릴리스 디렉터리 준비 (/opt/cardrag/${REL_TAG})
NEW_REL="/opt/cardrag/${REL_TAG}"
mkdir -p "$NEW_REL"
rsync -a --delete --exclude '.git' --exclude '.venv' /home/lee/projects/MCP_card_prd_detail/ "$NEW_REL/"
cp -a /opt/cardrag/current/operations/compose.runtime.private.yaml "$NEW_REL/operations/"

# 2. 릴리스 활성화 (타이머/서비스 정지 확인 및 심볼릭 링크 원자 교체)
"$NEW_REL/operations/activate.sh"
```

### 4.3 정규 `worker-compose.sh` 기반 상태 조회 및 Flush 명령

실제 환경의 마운트 볼륨과 환경변수를 전달하는 `/opt/cardrag/current/operations/worker-compose.sh`를 사용합니다.

```bash
# 1. Read-Only 백업 상태 진단 (RO 진단이므로 backup_scope_complete는 null로 반환됨)
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup status --run <RUN_ID> --issuer bc

# 2. 정규 Flush 실행 (실제 WebDAV 전송 및 commit 후 backup_scope_complete=True 반환)
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup flush --run <RUN_ID> --issuer bc
```

### 4.4 롤백 절차 (안전 기준 준수)

배치 자동 기동(`up -d`)을 절대 사용하지 않고, 릴리스 포인터만 복귀하며 타이머/배치는 중지 상태를 유지합니다.

```bash
# 1. 실행 중인 컨테이너 및 락 점검
if /opt/cardrag/current/operations/worker-compose.sh ps -q | grep -q .; then
    echo "Worker 컨테이너 실행 중: 종료 후 진행" >&2
    exit 1
fi

# 2. 직전 검증된 릴리스로 심볼릭 링크 복구 (이전 릴리스: 019-e7fb8a0ba60c)
PREV_REL=$(cat /opt/cardrag/current/operations/previous-release.txt 2>/dev/null || echo "/opt/cardrag/019-e7fb8a0ba60c")
ln -sfn "$PREV_REL" /opt/cardrag/current.rollback
mv -Tf /opt/cardrag/current.rollback /opt/cardrag/current

# 3. 설정 정합성 확인 (배치/타이머 자동 기동 금지)
/opt/cardrag/current/operations/worker-compose.sh config --quiet
printf "롤백 완료. 현재 활성 릴리스: %s (타이머 및 배치는 중지 상태 유지)\n" "$(readlink -f /opt/cardrag/current)"
```

---

## 5. [P2] DB 복구본 분석 및 안전 Dry-run 인벤토리

### 5.1 DB 크기 실측 및 내용 동일성 분석

- **현재 활성 Worker DB (`/state/worker-state.sqlite3`)**: `7,305,965,568 bytes` (2026-10-20 05:38 수정)
- **직전 복구본 DB (`/state/deployment-backups/019-ba273106158e/worker-state.sqlite3`)**: `7,291,777,024 bytes` (2026-10-19 21:59 수정)
- **차이 분석**:
  - 두 DB 사이에 우리, KB, 삼성, BC 4개 카드사의 live 실행이 발생하여 run 이력, diff, 로그가 `worker-state.sqlite3`에 영속화되었습니다.
  - 따라서 단순 코드 패치 배포라는 이유로 이전 복구본 DB를 재참조해서는 안 되며, **새 릴리스 배포 시 현재 상태의 새 일관된 snapshot(7.3GB)을 보존**해야만 4개 카드사 실행 결과를 유실하지 않고 롤백할 수 있습니다.

### 5.2 복구본 인벤토리 및 보호 집합(Protected Set)

실제 볼륨(`cardrag-worker-state:/state:ro`) 조회를 통해 확인된 복구본 인벤토리:

| 복구 디렉터리 | 디스크 점유 | 설명 및 의존성 | 보호 여부 |
|---|---:|---|---|
| `/state/deployment-backups/019-e7fb8a0ba60c` | 27.2 MiB | 현재 활성 릴리스 복구본 (`019-ba273106158e` DB 참조) | **보호 (Active Release)** |
| `/state/deployment-backups/019-ba273106158e` | 6.8 GiB | 직전 롤백 대상 릴리스 복구본 (자체 7.3GB DB 보유) | **보호 (Rollback Target)** |
| `/state/deployment-backups/019-4dba44664659` | 6.8 GiB | 3세대 이전 구형 019 복구본 (독립 DB 보유) | **정리 후보 (Outside Protected Set)** |
| `/state/deployment-backups/017-fca5ef313a0b` | 6.8 GiB | 017 릴리스 복구본 (독립 DB 보유) | **정리 후보 (Outside Protected Set)** |

### 5.3 안전 Dry-run 및 예상 물리 회수량

- **보호 집합**:
  1. 현재 활성 릴리스: `019-e7fb8a0ba60c`
  2. 직전 롤백 대상 릴리스: `019-ba273106158e` (활성 릴리스의 DB 의존성 포함)
- **정리 후보 (Dry-run)**:
  - 후보 1: `/state/deployment-backups/019-4dba44664659` (6.8 GiB)
  - 후보 2: `/state/deployment-backups/017-fca5ef313a0b` (6.8 GiB)
- **예상 물리 회수량**: 약 **13.6 GiB**
- **조치 방침**:
  - 이번 FIX_15 구현 검증에서는 디스크 공간(여유 약 76GiB)이 충분하므로 삭제를 강제하지 않으며, 향후 사용자 명시 승인 시 위 두 디렉터리에 한정하여 수동 정리를 수행합니다.
  - `docker system prune` 및 volume prune은 절대 사용하지 않습니다.
