# FIX_07_REPORT: v1.0.38 운영 이미지 출처·복구점 정합성 보완 완료 보고서

- **작성 일자**: 2026-10-31
- **수행 역할**: Executor
- **참조 문서**:
  - `.handoff/022_woori-raonk-session-auth/FIX_07.md`
  - `.handoff/022_woori-raonk-session-auth/REVIEW_03.md`
  - `.handoff/022_woori-raonk-session-auth/FIX_06_REPORT.md`
  - `.handoff/STATUS_v1.0.38.md`

---

## 1. 개요 및 지시사항 이행 요약

`FIX_07.md` 및 `REVIEW_03.md`에서 요청된 2가지 운영 정합성 보완 항목을 완결하였습니다.

1. **[P2] 운영 이미지 OCI Version/Revision 출처 레이블 정합성 확보**:
   - `Dockerfile`에 정의된 `APP_VERSION`, `VCS_REF`, `SOURCE_URL` 빌드 인자를 공식 릴리스 커밋(`99b5ba69fb5907be2218873c10bbb945d6181dcd`)과 버전(`1.0.38`)에 맞추어 명시적으로 주입하여 재빌드하였습니다.
   - 고유 식별자(`cardrag-worker:v1.0.38-99b5ba69fb59`, `cardrag-mcp:v1.0.38-99b5ba69fb59`)로 빌드 후 공식 태그(`cardrag-worker:v1.0.38`, `cardrag-mcp:v1.0.38`)를 갱신하였습니다.
   - 실행 중인 MCP는 기존 `cardrag-mcp:013-50a0129`(패키지 1.0.34)를 안정적으로 유지하였으며, 신규 MCP 이미지는 빌드 전용(build-only)으로 보관합니다.
2. **[P2] 우리카드 재개 완주 상태 반영 신규 독립 복구 스냅샷 생성 및 가이던스 일치화**:
   - 실행 중인 writer/backup이 없는 정비 구간에서 `cardrag-worker-state`의 `worker.lock` 및 `backup.lock`을 비차단 획득한 격리 펜스 컨테이너(`cardrag-v1038-snapshot-fence`)를 구동하였습니다.
   - 우리카드 resume 성공(`df29d8e5dbb34048942a5296bb2d6ac8`, `succeeded`) 및 활성 서빙 generation(`g-df29d8e5dbb34048942a5296-5a3a99224f92`)이 온전히 반영된 신규 독립 복구 스냅샷 `v1038-aligned-20261031`을 SQLite online backup으로 생성하였습니다.
   - `verify-recovery-point.py` 무결성 검증 통과 후, 단일 복구 스냅샷 정책에 따라 재개 이전의 구 스냅샷 `022-woori-session-auth-20261030`을 정리(prune)하여 `/state/deployment-backups/`에 단 1개의 검증된 스냅샷만 유지하도록 조치하였습니다.
   - 활성 릴리스(`/opt/cardrag/v1.0.38`)와 코드 롤백 릴리스(`/opt/cardrag/022-woori-session-auth-20261030`)의 `recovery-point.json` 및 `snapshot-retention.json`을 100% 동일하게 동기화하였습니다.
3. **Systemd Timer 상시 유지**:
   - `cardrag-worker.timer`는 정지나 비활성화 없이 지속적으로 `active (waiting)` 상태를 유지하였습니다 (다음 실행 예정: 2026-10-31 10:00:00 KST).

---

## 2. 이미지 식별자 및 OCI 레이블 전후 비교

### A. Worker Image
- **기존 이미지 (dev 레이블)**:
  - Image ID: `sha256:d1bd4bb35090056778d90c2a59480733655181918d3524e7d8d22ec9c4d3d511`
  - Version: `dev` / Revision: `unknown`
- **신규 정합 이미지**:
  - Image ID: `sha256:8399cd078ad468646959c3980581608e70d4dba7f6e538d5a7c5ebfa546a3a05`
  - 고유 태그: `cardrag-worker:v1.0.38-99b5ba69fb59`
  - 운영 태그: `cardrag-worker:v1.0.38`
  - OCI 레이블 검증:
    ```json
    {
      "org.opencontainers.image.version": "1.0.38",
      "org.opencontainers.image.revision": "99b5ba69fb5907be2218873c10bbb945d6181dcd",
      "org.opencontainers.image.source": "https://github.com/Kanu-Coffee/MCP_card_prd_detail",
      "org.opencontainers.image.title": "CardRAG Worker"
    }
    ```

### B. MCP Image (Build-only)
- **기존 이미지 (dev 레이블)**:
  - Image ID: `sha256:a0ded5064dfff593414931e73b0e5350c3185056999af847057a81c6eb9317c4`
  - Version: `dev` / Revision: `unknown`
- **신규 정합 이미지**:
  - Image ID: `sha256:fd32cce9b7363279dd4481890fe8ccfc19edf2f301da34add29da089cfc9bb0a`
  - 고유 태그: `cardrag-mcp:v1.0.38-99b5ba69fb59`
  - 태그: `cardrag-mcp:v1.0.38`
  - OCI 레이블 검증:
    ```json
    {
      "org.opencontainers.image.version": "1.0.38",
      "org.opencontainers.image.revision": "99b5ba69fb5907be2218873c10bbb945d6181dcd",
      "org.opencontainers.image.source": "https://github.com/Kanu-Coffee/MCP_card_prd_detail",
      "org.opencontainers.image.title": "CardRAG MCP"
    }
    ```
- **실행 중인 MCP 상태**:
  - Container: `cardrag-mcp`
  - 실행 이미지: `cardrag-mcp:013-50a0129` (패키지 1.0.34)
  - 역할: 호스트 프록시 및 고객 edge를 통해 서빙 볼륨을 안정적으로 중계 (교체 불필요, build-only MCP 1.0.38과 명확히 구분).

---

## 3. 신규 독립 복구 스냅샷 생성 및 가이던스 동기화

### A. 격리 펜스 컨테이너 및 락 획득
- **펜스 컨테이너**: `cardrag-v1038-snapshot-fence`
- **보호 락**: `/state/worker.lock` 및 `/state/backup.lock` 비차단 배타 락(`LOCK_EX | LOCK_NB`) 획득 상태에서 수행.
- **스냅샷 식별자**: `v1038-aligned-20261031` (snapshot tag 규칙 `[a-zA-Z0-9-_]` 준수)
- **스냅샷 경로**: `/state/deployment-backups/v1038-aligned-20261031`

### B. 스냅샷 데이터베이스 무결성 검증
`verify-recovery-point.py` 실행 결과:
```json
{
  "schema_version": "cardrag.recovery-point-verification.v1",
  "verified": true,
  "protected_snapshot": "v1038-aligned-20261031",
  "snapshot_directory": "/state/deployment-backups/v1038-aligned-20261031",
  "snapshot_evidence_sha256": "430335eebcc074e2a86851b2eec03d6d63c5d648dd0beaf39c096e95be4d4b1a",
  "databases": {
    "worker-state.sqlite3": {
      "size": 7466295296,
      "sha256": "772f126da361bb9aa8f9e07572ce346cbb06d2cfa23ba8f070b4792ecf1469e3",
      "quick_check": "ok",
      "wal_bytes": 0
    },
    "backup-ledger.sqlite3": {
      "size": 27185152,
      "sha256": "b06497f3b8908323a6f4a86b3ec482df790bf70e060011504936b801a2f643e1",
      "quick_check": "ok",
      "wal_bytes": 0
    }
  },
  "latest_run": [
    "df29d8e5dbb34048942a5296bb2d6ac8",
    "succeeded"
  ],
  "stable_generation": "g-df29d8e5dbb34048942a5296-5a3a99224f92",
  "code_rollback": {
    "available": true,
    "release": "/opt/cardrag/022-woori-session-auth-20261030",
    "image": "cardrag-worker:022-woori-auth-47ea2207b173",
    "image_id": "sha256:b0b765d9e1083fe5eb1649ee4d1918de6a87b54c656458285a68cc664f2064c0",
    "scope": "code/image rollback only; database restore separate"
  },
  "database_restore": {
    "authorized": false,
    "performed": false,
    "compatibility_verified": false,
    "reason": "DB restore is separate; rollback image/schema compatibility has not been established."
  },
  "historical_release": {
    "release": "/opt/cardrag/019-fix32-55c579b986cb",
    "snapshot_directory": "/state/deployment-backups/019-fix32-current-20261022",
    "snapshot_status": "pruned_missing",
    "snapshot_exists": false,
    "selected_as_current": false
  },
  "restore_performed": false,
  "state_written": false
}
```

### C. 단일 독립 스냅샷 정책 이행 (구 스냅샷 정리)
- 신규 스냅샷 `v1038-aligned-20261031` 검증 완료 후, 재개 이전의 구 스냅샷 `022-woori-session-auth-20261030`을 펜스 락 하에서 안전하게 정리하였습니다.
- `/state/deployment-backups/` 잔존 스냅샷 확인:
  `Remaining snapshots in deployment-backups: ['v1038-aligned-20261031']` (정확히 1개).

### D. 활성 및 롤백 릴리스 가이던스 일치화
`/opt/cardrag/v1.0.38/operations/recovery-point.json` 및 `/opt/cardrag/022-woori-session-auth-20261030/operations/recovery-point.json`에 동일한 안내를 동기화 기록하였습니다.
- `active_release`: `/opt/cardrag/v1.0.38`
- `protected_snapshot`: `v1038-aligned-20261031`
- `code_rollback`:
  - `release`: `/opt/cardrag/022-woori-session-auth-20261030`
  - `image`: `cardrag-worker:022-woori-auth-47ea2207b173`
  - `scope`: `code/image rollback only; database restore separate`
- `database_restore`:
  - `authorized`: `false` (DB 복원은 코드 롤백과 명확히 분리되며 사전 호환성 검증 전까지 미승인)
- `pruned_snapshots`: `["019-fix33-current-20261022", "021-ocr-logging-20261029", "022-woori-session-auth-20261030"]`

---

## 4. 런타임 검증 및 상태 보고

### A. Startup Check
신규 이미지 `cardrag-worker:v1.0.38`(`sha256:8399cd078ad4...`)로 `startup-check` 수행:
- `status`: `ready`
- `stage`: `completed`
- `quality_status`: `passed` (DeepInfra Qwen repeated cosine: `1.000000`, 24 samples 통과)
- `ocr_provider`: `codex-exec` (`gpt-6.1-terra`, high)
- `ocr_fallback_provider`: `local-paddleocr` (`PaddleOCR-VL-1.6`)

### B. 라이브 MCP 연동 검증
`operations/test-live-mcp.sh` 실행:
- Active Generation: `g-df29d8e5dbb34048942a5296-5a3a99224f92` (`matches_worker_stable: true`)
- Sample 1: `doc_0bab913a6e71f31d40db4839b109f3291280d44e756b5ea184ef9f4977295e85` (3,605 chars)
- Sample 2: `doc_fccf4004609c1ba37e67c3e67a6c246953d4ea6ea79d4b47c784a1efee903311` (4,698 chars)
- 결과: `ocr_page_calls_succeeded: 2`, `read_only: true`.

### C. 디스크 및 용량 버퍼
- `/state` 가용 공간: **74.02 GiB**
- 호스트 루트 파일시스템: **75 GB 가용** (Reviewer의 12.15GB 캐시 정리 후 넉넉한 공간 유지).

### D. Systemd Timer
- `cardrag-worker.timer`: `active (waiting)` 유지
- Trigger: **Sat 2026-10-31 10:00:00 KST** (~1시간 후 정기 실행 예정).

---

## 5. 결론

Reviewer가 `FIX_07.md`에서 요구한:
1. OCI 이미지 레이블 출처 정합화 (`version=1.0.38`, `revision=99b5ba69fb5907be2218873c10bbb945d6181dcd`)
2. 최신 완주 상태 반영 신규 독립 복구 스냅샷(`v1038-aligned-20261031`) 생성 및 단일 스냅샷 보존 정책 준수
3. 활성 및 롤백 릴리스 가이던스 일치화
4. 서비스 중단 없는 타이머 상시 활성화

모든 항목이 결함 없이 완결되었습니다.
