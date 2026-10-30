# 022 FIX_05 REPORT — 인수된 우리카드 어댑터 운영 배포 및 실패 Worker 재개 완료 보고

작성일: 2026-10-31 KST  
역할: Executor (운영 배포 및 Worker 재개 실행)  
상태: 완료 (Completed)

---

## 1. 개요 및 사용자 지시 이행

`REVIEW_01.md` 및 `FIX_05.md`의 사용자 승인 운영 실행 지시에 따라, 검증 완료된 우리카드 세션 인증 및 RAONK 암호화 구현을 운영 환경에 적용하고, 2026-10-30 10:00:43에 실패했던 우리카드 수집 run(`df29d8e5dbb34048942a5296bb2d6ac8`)을 성공적으로 재개(resume) 완료하였습니다.

- **Systemd Timer 유지**: 사용자 지시("배포·재개 중 timer를 disable/stop하지 않으며...")에 따라 `cardrag-worker.timer`는 정지(stop)나 비활성화(disable) 없이 항상 `active (waiting)` 상태를 유지하였습니다.
- **동시성 제어**: `/opt/cardrag/.deployment.lock`, 구/신 릴리스의 `manual-start.lock`, 및 `cardrag-worker-state` 볼륨 내 SQLite 배타 락(`worker.lock`, `backup.lock`)을 순차적으로 획득하여 안전하게 스냅샷 및 릴리스 전환을 수행하였습니다.
- **단일 실패 Run 재개**: 신규 회차나 임의 슬롯 생성을 하지 않고, 실패했던 대상 run ID `df29d8e5dbb34048942a5296bb2d6ac8`를 명시하여 재개하였습니다.
- **완료 조건 충족**: 수집(collection) 성공, 918건 PDF 수집/검증, OCR 캐시 재사용(981건) 및 1건 신규 처리, V5 임베딩/파싱 검증 완료, 로컬 서빙 generation(`g-df29d8e5dbb34048942a5296-5a3a99224f92`) 게시 및 MCP 서빙 반영, 백업 scope 완결(`backup_scope_complete: true`)을 모두 확인하였습니다.

---

## 2. 배포 입력, 이미지 및 릴리스 전환 검증

### A. 배포 입력 파일 해시

`REVIEW_01.md`에서 검증된 세 파일의 SHA-256 해시가 운영 릴리스 소스 디렉터리에 정확히 반영되었습니다.

| 파일 경로 (`apps/cardrag-worker/` 기준) | SHA-256 Checksum | 검증 상태 |
|---|---|---|
| `src/cardrag_worker/issuers/woori.py` | `cc454a9ad01db4ad29a45592b6efdc2839744f8fe34bd593a7bbe97eb1d95039` | 일치 (Verified) |
| `src/cardrag_worker/downloader.py` | `b3d9e4021d086f9866e99944c7fc5c6890ccf109454a06840a801b5f9800a2d1` | 일치 (Verified) |
| `src/cardrag_worker/pipeline.py` | `c3f060cc2665b8596524f8491d48f7667ee19192bfad4ead9ce48ced60f72e2e` | 일치 (Verified) |

- **Patch Input SHA-256**: `47ea2207b17366f429c0add2be00d8d764822630f6b4a256e0a9d26a98475ef2`
- **Base Image**: `cardrag-worker:021-ocr-log-d01f2d908acc` (ID: `sha256:983b2196fb1f4b75d7f15c93248602e4c0d0ed50c944d9a7d471dc35b1234f92`)
- **Candidate Image**: `cardrag-worker:022-woori-auth-47ea2207b173` (ID: `sha256:b0b765d9e1083fe5eb1649ee4d1918de6a87b54c656458285a68cc664f2064c0`)
- **New Release Directory**: `/opt/cardrag/022-woori-session-auth-20261030`
- **Previous Release Directory**: `/opt/cardrag/021-ocr-logging-20261029`

### B. 런타임 설정 동등성 및 서비스 계정 검증

- **Compose Config 비교**: `worker-compose.sh config --format json`을 통해 기존 021 릴리스와 신규 022 릴리스 간 환경 변수, 볼륨 마운트, 시크릿, 보안 옵션(`read_only`, `security_opt`, `cap_drop`), UID/GID(`10001:10001`)가 100% 동일하며 오직 이미지 태그만 `cardrag-worker:021-ocr-log-d01f2d908acc`에서 `cardrag-worker:022-woori-auth-47ea2207b173`로 변경되었음을 기계적으로 검증하였습니다.
- **서비스 계정 검증**: `deploy/worker/verify-service-access.sh`를 실행하여 실제 `cardrag` 계정(uid=10001, gid=10001, docker 그룹 소속)에 의한 릴리스 디렉터리, compose 파일, 환경 파일(`/etc/cardrag/worker.env`) 접근 권한을 확인 완료하였습니다.

### C. 배포 전 실서버 사전 검증 (3개 상품 대상)

격리된 임시 컨테이너에서 우리카드 실서버로부터 3개 대표 상품(`100637`, `500107`, `104149`)을 다운로드하여 세션 발급, 토큰 추출, RAONK 인증 헤더 생성, PDF 무결성을 사전 검증하였습니다.

- `100637 (배달의민족 비장의카드 V.2)`: 1,141,253 bytes, 2 pages, sha256 `54aacbe92971458c61970e8c7b84aa871319a0ee95b73759329d996e21447b3f`
- `500107 (카드의정석2 SOLID)`: 328,332 bytes, 8 pages, sha256 `5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69`
- `104149 (the WON)`: 4,040,084 bytes, 9 pages, sha256 `0f9589481d5c08e10583d9c78b070b81aa86d9685a9cd869312288b896cedc6d`

---

## 3. 신규 복구 지점(Snapshot) 생성 및 릴리스 활성화

### A. 신규 복구 스냅샷 생성 및 검증

- **격리 펜스 컨테이너**: `cardrag-022-woori-snapshot-fence`를 구동하여 `/state/worker.lock` 및 `/state/backup.lock` 배타 락을 획득한 상태에서 `snapshot-lock.py`를 통해 안전하게 스냅샷을 생성하였습니다.
- **Protected Snapshot Name**: `022-woori-session-auth-20261030`
- **Snapshot Directory**: `/state/deployment-backups/022-woori-session-auth-20261030`
- **Snapshot Evidence SHA-256**: `bb1baad17f25e98bb4b931d87e07ae41dfb39d735dc548d8a7a8d5c4176cf988`
- **데이터베이스 무결성 검증 (`verify-recovery-point.py`)**:
  - `worker-state.sqlite3`: size `7,467,991,040` bytes, `quick_check: ok`, `wal_bytes: 0`
  - `backup-ledger.sqlite3`: size `27,185,152` bytes, `quick_check: ok`, `wal_bytes: 0`
  - `verified`: `true`

### B. 보존 및 롤백 정책 (`snapshot-retention.json`, `recovery-point.json`)

- **보존 스냅샷**: 직전 스냅샷 `021-ocr-logging-20261029` 및 구버전 참조 스냅샷 `019-fix33-current-20261022`를 모두 삭제 없이 보존(retained).
- **Code Rollback 지점**: `/opt/cardrag/021-ocr-logging-20261029` (Image: `cardrag-worker:021-ocr-log-d01f2d908acc`, ID: `sha256:983b...`).
- **원자적 심볼릭 링크 전환**:
  ```bash
  ln -s /opt/cardrag/022-woori-session-auth-20261030 /opt/cardrag/current.022-next
  mv -Tf /opt/cardrag/current.022-next /opt/cardrag/current
  ```
  현재 활성 릴리스: `/opt/cardrag/current -> /opt/cardrag/022-woori-session-auth-20261030`

### C. Startup Check

신규 릴리스에서 `worker startup-check`를 수행하여 모든 프로바이더와 용량 사전 점검이 정상 통과함을 확인하였습니다.
- `status`: `ready`
- `stage`: `completed`
- `quality_status`: `passed`
- `ocr_provider`: `codex-exec` (`gpt-6.1-terra`, high)
- `ocr_fallback_provider`: `local-paddleocr` (`PaddleOCR-VL-1.6`)
- `serving_pointer`: 기존 stable generation 유지 확인

---

## 4. 실패 Run 재개(Resume) 및 완주 검증

### A. 컨테이너 구동 정보

- **명령어**:
  ```bash
  /opt/cardrag/current/operations/worker-compose.sh run -d --no-deps \
    --name cardrag-worker-022-woori-resume-20261030 \
    worker run --resume df29d8e5dbb34048942a5296bb2d6ac8
  ```
- **Container ID**: `5ba73486655028382dc4b9e4bb47e81ecb4daf3c5c031522abc31e6c177389e5`
- **시작 시각**: 2026-10-30 22:41:40 KST (13:41:40 UTC)
- **종료 시각**: 2026-10-31 00:19:02 KST (15:19:02 UTC)
- **소요 시간**: 5,837.98초 (약 1시간 37분)
- **종료 코드 (Exit Code)**: `0` (Success)

### B. 파이프라인 수행 결과

- **Run ID**: `df29d8e5dbb34048942a5296bb2d6ac8`
- **최종 상태**: `status: succeeded`, `quality_status: passed`, `published: true`
- **우리카드 수집(Discovery & Download)**:
  - Discovery 레코드 수: 922건 (경고 0건)
  - PDF 성공 건수: 918건 (`pdf_succeeded_count: 918`)
  - 다운로드 건수: 220건 (`pdf_downloads: 220`)
  - PDF 캐시 히트: 916건 (`pdf_cache_hits: 916`)
  - 캐시 재검증: 218건 (`pdf_cache_revalidations: 218`)
  - 격리/스킵: 4건 (`pdf_skipped_count: 4` - 지원되지 않는 부속서류 정책에 따름)
  - 우리카드 원본 서비스 오류(`issuer_origin_service_error`) 및 세션 인증 실패: **0건 (완전 해결)**
- **OCR 처리**:
  - 기보유 OCR 캐시 재사용: 981건 (`ocr_cache_reused_count: 981`)
  - 신규 OCR 프로바이더 호출: 1건 (`ocr_provider_called_count: 1`)
- **문서 및 임베딩 검증**:
  - 전체 유효 문서 수: 5,669건 (8개 카드사 전체 커버)
  - 전체 임베딩 뷰 검증: 631,845 / 631,845 (100.0%)
  - 계약 개정 이력 커버리지: 5,669 / 5,669 (100.0%)
  - 구조화 실패 문서: 0건
  - 미해결 개정 이력: 0건

---

## 5. 서빙 게시(Publishing) 및 MCP 반영 확인

### A. Worker Stable Pointer 갱신

`/var/lib/cardrag-serving/v1/channels/stable.json` 파일이 재개 완료된 run의 generation으로 안전하게 갱신되었습니다.
```json
{
  "generation_id": "g-df29d8e5dbb34048942a5296-5a3a99224f92",
  "manifest_sha256": "19103fe6ccb7f372fa3c0645371b54a7a749f598b45aec5198cc01988914ff9c",
  "ready_sha256": "a5452ce4cfac2ad76c0f359c7b00b9988d9ba085ada2d1f4fce12003869a776f",
  "schema_version": "cardrag.generation-pointer.v1"
}
```

### B. MCP 라이브 서빙 반영 (`test-live-mcp.sh`)

라이브 `cardrag-mcp` 컨테이너에 대해 헬스체크 및 도구 호출 검증을 수행하여 최신 generation이 활성화되었음을 확인하였습니다.
```json
{
  "live_mcp_generation": "g-df29d8e5dbb34048942a5296-5a3a99224f92",
  "matches_worker_stable": true,
  "ocr_page_calls_succeeded": 2,
  "read_only": true
}
```

### C. 타 카드사(7사) 및 개정 이력 보존 검증

| 카드사 프로파일 | 문서 수 (Documents) | 상태 |
|---|---|---|
| BC카드 (`bc.generic.v1`) | 125 | 보존 확인 |
| 하나카드 (`hana.generic.v1`) | 745 | 보존 확인 |
| 현대카드 (`hyundai.generic.v1`) | 607 | 보존 확인 |
| KB국민카드 (`kb.v1`) | 757 | 보존 확인 |
| 롯데카드 (`lotte.generic.v1`) | 725 | 보존 확인 |
| 삼성카드 (`samsung.v1`) | 769 | 보존 확인 |
| 신한카드 (`shinhan.v1`) | 959 | 보존 확인 |
| **우리카드 (`woori.v1`)** | **982** | **정상 갱신 반영** |
| **총계** | **5,669** | **완전 보존** |

- `historical_pdf_cache_hits`: 64건
- `historical_revision_unresolved_count`: 0건
- `superseded_revision_count`: 588건

---

## 6. 백업 상태 및 Scope 완결 검증

### A. 백업 Scope 상태 확인

- 명령어:
  ```bash
  /opt/cardrag/current/operations/worker-compose.sh run --rm -T --no-deps worker \
    backup status --run df29d8e5dbb34048942a5296bb2d6ac8 --issuer woori
  ```
- 결과:
  - `backup_in_scope_remaining`: 0
  - `backup_in_scope_conflicts`: 0
  - `backup_in_scope_missing`: 0
  - `backup_pending_out_of_scope`: 4 (2026-10-21자 과거 타 scope 대기 항목으로 보존됨)

### B. 백업 Flush 수행 (`backup flush --run ... --issuer woori`)

- 명령어:
  ```bash
  /opt/cardrag/current/operations/worker-compose.sh run --rm -T --no-deps worker \
    backup flush --run df29d8e5dbb34048942a5296bb2d6ac8 --issuer woori
  ```
- 결과:
  ```json
  {
    "backup_scope_complete": true,
    "error": null,
    "in_scope_remaining": 0,
    "scoped_issuers": ["woori"],
    "scoped_run_id": "df29d8e5dbb34048942a5296bb2d6ac8",
    "status": "unchanged"
  }
  ```
- `backup_scope_complete: true`로 대상 run 및 우리카드 범위의 백업 완결이 최종 확인되었습니다.

---

## 7. 시스템 잔여 자원 및 타이머 상태

- **Systemd Timer 상태**:
  ```text
  ● cardrag-worker.timer - Run CardRAG Worker daily at 10:00 Asia/Seoul
       Loaded: loaded (/etc/systemd/system/cardrag-worker.timer; enabled; preset: enabled)
       Active: active (waiting) since Wed 2026-10-28 15:46:19 KST; 2 days ago
      Trigger: Sat 2026-10-31 10:00:00 KST; 9h left
     Triggers: ● cardrag-worker.service
  ```
  타이머는 단 한 번도 중단되지 않고 활성 상태를 유지하고 있으며, 다음 정기 배치는 2026-10-31 10:00:00 KST에 정상 트리거 예정입니다.
- **디스크 여유 공간**:
  - 루트 및 도커 볼륨 파일시스템 가용량: **76 GB 가용** (사용률 80%)
  - 안정적 운영 여유 공간 확보 확인 완료.

---

## 8. 결론

`022_woori-raonk-session-auth` 작업은 다음과 같이 모든 완료 조건을 완벽하게 달성하였습니다:
1. `REVIEW_01.md`에서 승인된 우리카드 RAONK 세션 인증 및 쿠키 추적 구현체가 신규 022 릴리스(`cardrag-worker:022-woori-auth-47ea2207b173`)로 안전하게 빌드 및 배포되었습니다.
2. 배포 과정에서 `cardrag-worker.timer`를 온전히 유지하면서 파일시스템 및 SQLite exclusive lock을 통해 스냅샷 생성 및 릴리스 교체를 무장애로 완수하였습니다.
3. 중단되었던 우리카드 실패 run `df29d8e5dbb34048942a5296bb2d6ac8`이 성공적으로 재개되어 정상 완주(exit 0, `status=succeeded`)하였습니다.
4. 신규 generation `g-df29d8e5dbb34048942a5296-5a3a99224f92`가 게시되었고, 라이브 MCP 서버에서 즉시 로딩되어 조회 기능이 검증되었습니다.
5. 대상 run 및 우리카드 scope의 백업이 완전하게 체결(`backup_scope_complete: true`)되었습니다.
