# 019 FIX_17_REPORT — 시스템 용량 누적 원인 규명, 안전한 가용공간 13.7GiB 확보 및 사전 용량 점검 체계 구축

- 작성일: 2026-10-20
- 역할: Executor (Antigravity)
- 근거: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_17.md` 및 `LIVE_REVIEW_05.md`
- 사용자 요청 반영: "executor 그리고 안전한 가용공간 확보까지 진행해. 전체적으로 시스템 용량 왤케많이 쌓였냐"
- Git 기준: HEAD `aaeb2c4` 위의 019 미커밋 작업 트리 (과거 handoff 문서 및 기존 변경 보존)

---

## 1. 시스템 용량 누적 원인 심층 분석 (왜 이렇게 많이 쌓였는가?)

사용자의 "전체적으로 시스템 용량 왤케많이 쌓였냐" 질의에 대해 실제 파일시스템 및 Docker 볼륨 실측 조사를 기반으로 분석한 결과입니다.

### 1.1 CardRAG 파이프라인의 구조적 특성

- **선택 실행이라도 전체 8사 통합 corpus 산출물(17.7GB) 생성**:
  - 특정 1개 카드사(예: BC 122건)만 단독 실행하더라도, 벡터 검색 서빙 계약상 전체 8개 카드사 5,543개 문서 전체의 SQLite DB(`corpus.sqlite3`) 및 4096D 임베딩 벡터 sidecar를 통합 생성하여 무결성을 검증합니다.
  - 이로 인해 1회 성공 실행마다 약 **17.7 GiB**의 대규모 런 디렉터리가 생성됩니다.

### 1.2 용도별 실측 점유 현황 (총 310 GiB 사용 중)

| 구성 요소 | 점유 용량 | 누적 원인 및 정책 |
|---|---:|---|
| **Worker runs** (`/state/runs`) | **42.7 GiB** | **정상 보존 정책 준수**: 성공 run 2세대(KB 17.7G + 삼성 17.7G = 35.4G) + 실패 run 2세대(BC 실패 2건 7.3G) |
| **MCP State** (`cardrag-mcp-state`) | **46.4 GiB** | **운영 설정 준수**: `CARDRAG_MCP_RETAIN_GENERATIONS=3` 설정에 따른 최신 게시 3세대 + 공용 CAS 오브젝트 |
| **Serving State** (`cardrag-serving`) | **32.4 GiB** | **운영 설정 준수**: 활성 서빙 2세대 + 공용 CAS 오브젝트 |
| **배포 복구본** (`/state/deployment-backups`) | **27.3 GiB** | 과거 배포마다 DB(7.3GB)를 보존하여 총 5개 릴리스 복구본 누적 (017, 019-4dba, 019-ba, 019-e7, 019-fix16) |
| **Worker DB & PDF Cache** | **11.0 GiB** | Worker SQLite 상태 원장(6.8 GiB) + 원천 PDF 캐시(4.2 GiB) |
| **Docker Images & Build Cache** | **51.4 GiB** | 컨테이너 이미지(43.4 GiB) + 빌드 레이어 캐시(8.0 GiB) |
| **기타 호스트 자원** | **~60 GiB** | `/home` 개발 환경(44 GiB), LibreChat/Postgres 서비스 볼륨(11 GiB), `/opt/researchops`(6.6 GiB) |

> **결론**: 비정상적인 디스크 누수(Memory leak이나 임시 파일 폭증)가 아니라, **(1) 17.7GB 단위의 통합 세대 보존 정책(Worker 2개, Serving 2개, MCP 3개)**과 **(2) 배포 시마다 보존된 독립 7.3GB DB 복구본 5개**가 중첩되면서 여유 공간이 65GiB 수준으로 줄어들어, 필요 용량(68.9GiB)에 도달하지 못해 `capacity_free_space` 게이트에서 차단되었던 것입니다.

---

## 2. [P1] 안전한 가용공간 13.7 GiB 확보 실행

사용자의 "안전한 가용공간 확보까지 진행해" 지시와 `FIX_17.md`의 보호 기준에 따라 안전 정리를 수행했습니다.

### 2.1 보호 집합(Protected Set)과 고립 복구본 검증

- **보호 집합 (절대 삭제 금지)**:
  1. 현재 활성 릴리스: `019-fix16-4108e3dac5f5` 복구본 (자체 7.3GB DB 보유)
  2. 직전 릴리스: `019-e7fb8a0ba60c` 복구본 (27.2MB)
  3. 직전 릴리스의 의존성 DB: `019-ba273106158e` 복구본 (6.8GB DB)
  4. 활성 Serving / MCP 세대 및 최신 성공 런
- **정리 대상 (3세대 이상 경과한 완전 고립 복구본)**:
  1. `/state/deployment-backups/017-fca5ef313a0b`: 017 릴리스 복구본 (nlink=1, symlink 없음, 독립 inode)
  2. `/state/deployment-backups/019-4dba44664659`: 019 초기 릴리스 복구본 (nlink=1, symlink 없음, 독립 inode)

### 2.2 삭제 전/후 실측 결과

```text
=== 삭제 전 (BEFORE) ===
/dev/sda2: 전체 391.6G, 사용 309.5G, 가용 64.6G (83% 사용)

=== 삭제 후 (AFTER) ===
/dev/sda2: 전체 391.6G, 사용 295.9G, 가용 78.3G (79% 사용)
```

- **회수된 물리 공간**: **정확히 13.68 GiB (약 14.68 GB)**
- **현재 루트 파일시스템 가용량**: **84,005,748,736 bytes (78.236 GiB)**
- **BC 실행 필요 공간**: 73,971,502,728 bytes (68.891 GiB)
- **전처리 버퍼(4.0 GiB) 포함 필요 총량**: 78,266,470,024 bytes (72.891 GiB)
- **현재 여유 마진**: **+5,739,278,712 bytes (+5.345 GiB 여유)**
  - 이제 BC 실행 시 전처리 증가(3.7 GiB)가 발생하더라도 68.89 GiB 게이트를 여유 있게 통과할 수 있습니다.

---

## 3. [P2] 사전 용량 점검 체계 구축 및 가시화 개선

### 3.1 수동 실행 전 용량 점검 (`operations/run-issuer.sh`)

- `run-issuer.sh`에서 컨테이너를 기동하기 전, `/state/capacity-observation.json`의 직전 게이트 필요량(`required_free_bytes`)과 전처리 버퍼(4.0 GiB)를 합산하여 현재 가용 공간과 대조하는 사전 점검을 추가했습니다.
- **부족 시**: 수동 실행을 즉각 차단하고 현재 가용량, 필요량, 부족량을 출력하여 26분간의 불필요한 PDF/OCR 낭비를 방지합니다.
- **충분 시**: `[용량 점검 통과] 가용 78.236 GiB >= 필요 72.891 GiB (게이트: 68.891 GiB + 버퍼: 4.0 GiB, 여유 마진: +5.345 GiB)`를 출력하고 정상 실행합니다.

### 3.2 상태 조회 및 요약 가시화 (`status-issuer.sh`, `summary-issuers.sh`)

- `status-issuer.sh`:
  - Worker가 용량 부족으로 비정상 종료했을 때 출력하는 최종 failure exit payload(`status="failed"`, `failure_reason_code="capacity_free_space"`, `capacity_details`)를 파싱하도록 보완했습니다.
  - 해당 컨테이너 및 런(`d4fe5f3f85d344e7b981da0adb35a556`)에 정확히 바인딩하여 `execution_result`에 구조화된 결함 세부 정보를 영속 보존합니다.
- `summary-issuers.sh`:
  - 상태 요약 테이블에서 `failed(capacity_free_space)`로 원인을 즉시 식별할 수 있도록 개선했습니다:
    ```text
    issuer     run_status                  other_changes  backup_status  pending  scope_complete  backup_recorded
    woori      succeeded                                0  deferred       0        None            True
    kb         succeeded                                0  succeeded      0        True            True
    samsung    succeeded                                0  deferred       0        None            True
    bc         failed(capacity_free_space)              0  n/a            0        None            False
    ```

---

## 4. 테스트 및 검증 결과

1. **신규 회귀 테스트 (`apps/cardrag-worker/tests/test_fix_17_capacity_readiness.py`)**:
   - `test_echo_worker_unexpected_failure_formats_capacity_details`: V5CapacityError 발생 시 `capacity_details` 및 `failure_reason_code` 정상 포맷팅 검증.
   - `test_capacity_readiness_insufficient_vs_ready`: 64.6 GiB(부족) vs 78.2 GiB(통과) 판정 로직 검증.
   - `test_failed_run_does_not_record_backup_success`: 실패한 런의 백업 완료 허위 등록 방지 검증.
   - 결과: **3 passed** (0.46s).
2. **전체 코드 품질 검사**:
   - `ruff format --check apps packages`: **222 files already formatted** (통과).
   - `ruff check apps packages`: **All checks passed!**
   - `git diff --check`: 오류 없음.
3. **쉘 스크립트 정합성 검사**:
   - `bash -n run-issuer.sh status-issuer.sh summary-issuers.sh`: 문법 오류 없음.

---

## 5. 사용자 수동 BC 실행 안내

가용 공간이 **78.3 GiB**로 충분히 확보되었으며, 사전 점검 체계가 적용되었습니다. 아래 명령을 복사하여 BC 수동 검증을 진행하실 수 있습니다.

```bash
# 1. BC 카드 수동 실행 (용량 사전 점검 자동 수행 후 백그라운드 기동)
/opt/cardrag/current/operations/run-issuer.sh bc

# 2. 실행 로그 실시간 확인 (Ctrl+C로 빠져나와도 백그라운드 계속 실행됨)
docker logs -f $(python3 -c 'import json; print(json.load(open("/opt/cardrag/current/operations/live/bc.json"))["container"])')

# 3. 완료 후 상태 및 백업 검증
/opt/cardrag/current/operations/status-issuer.sh bc
/opt/cardrag/current/operations/summary-issuers.sh
```
