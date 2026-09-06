# v1.0.18 Worker 운영 효율화

v1.0.18은 기존 generation v5·Serving DB·vectors.f32·MCP 읽기 계약을 유지하면서
중복 검증과 반복 토큰 계산을 줄이고, PDF 수집 및 로컬 후처리를 제한적으로 병렬화한다.
실제 OCR provider 요청은 fallback·재시도를 포함해 항상 한 문서씩 처리한다.

## 처리 순서와 누락 방지

```text
Discovery snapshot
  → PDF 수집·검증·캐시 반영 (전체 8, 카드사별 2, 실제 호스트별 2)
  → 전체 작업 종료 장벽
  → 과거 리비전 구성·중복 검사·불변 문서 목록 확정
  → acquisition.v1.json 원자적 기록
  → 로컬 OCR 캐시 선읽기 / OCR 요청 순차 실행 / 구조·뷰 후처리 최대 4개
  → 모든 후처리 종료 및 문서 ID 집합 대조
  → 임베딩·Export·원격 검증·게시
```

PDF 다운로드 응답만 받은 상태는 수집 완료가 아니다. PDF 형식·해시·페이지 검증과
캐시/SQLite 반영까지 끝나야 완료 장벽을 통과한다. 재시도 중인 마지막 PDF를 기다리며,
허용 목록의 DRM 제외 외 수집 실패는 전체 실행을 중단한다. 실행 중 새로 나타난 상품은
다음 discovery에서 수집한다. 과거 리비전까지 확정한 뒤에는 OCR 입력 목록을 추가하지 않는다.

OCR 입력은 이 확정 목록에서만 나온다. 디렉터리 스캔으로 대상을 판단하지 않는다.
완료 후 성공·명시적 OCR 실패·구조 실패의 문서 ID를 전체 대상과 대조하고, 미처리·중복·
예상 밖 ID가 있으면 임베딩과 게시를 차단한다. 기존 카드사별 95% 성공 기준 및
과거 리비전 OCR 실패/구조 실패의 엄격한 게시 차단 정책을 유지한다.

시스템 OCR 오류가 나면 OCR 잠금을 해제하기 전에 후속 요청을 차단한다. 실패·취소 시
신규 작업 배정을 중단하고, 이미 실행 중인 스레드를 포함한 모든 작업이 종료된 뒤
Worker 잠금과 SQLite를 해제한다. 완료 로그는 입력 순번이 아닌 실제 완료 수를 사용한다.

`runs/<run-id>/checkpoints/acquisition.v1.json`은 수집 결과·문서/PDF 식별자·코퍼스 해시와
자체 payload 해시를 기록한다. 이 파일은 진단 증거이며 재시작 시 검증을 생략하는 허가증이
아니다. 기존 resume 정책에 따라 discovery를 갱신하고 PDF/OCR/구조 체크포인트의 입력과
무결성을 다시 검사한다. `no_change` 역시 기존 원격 아티팩트 전체 검증을 유지한다.

## 성능 변경

- Export 사전 검증은 lazy vector의 메타데이터만 검사한다. 기록 시 행당 한 번 로드해
  SHA-256·길이·유한값·L2 정규화를 검사한다. WorkerState 일반 조회는 엄격한 검증이
  기본이며, 최종 Export 검증에 연결된 두 내부 조회 경로만 빠른 검사를 사용한다.
- SQLite 캐시 기본 256MiB, mmap 요청 기본 2GiB이며 실제 적용값을 보고서에 기록한다.
  SQLite 빌드가 mmap을 제한하면 지원되는 크기 또는 일반 읽기를 사용한다. WAL·잠금·
  용량 검증은 유지하고 `temp_store=MEMORY`는 사용하지 않는다.
- 로컬 OCR 캐시 선읽기는 SQLite·provider·원격 저장소·파일을 변경하지 않는다. 검증된
  결과는 failover 전체 합산 8건/64MiB 한도 내에서 재사용한다. 디코딩 전에 manifest와
  본문·페이지 수를 바탕으로 보수적인 예산을 예약하며, 큰 문서나 예산 부족 시 선읽기를
  건너뛰고 기존 순차 resolve에 맡긴다. 디렉터리·파일 식별자 변경을 검사하고,
  source·contract·해시 불일치나 변조는 기존 엄격한 경로로 돌아간다.
- 문서 후처리는 최대 설정 worker 수만큼 진행하며 수천 개 task나 대기 결과를 만들지 않는다.
  PDFium과 OCR resolver는 순차로 실행하고, 구조/뷰 계산만 스레드에서 처리한다.
  SQLite·stage·보고서·체크포인트 반영은 이벤트 루프에서 수행한다.
- Qwen 토큰 수는 실행별 pinned tokenizer/profile 안에서 최종 입력 SHA-256을 키로
  재사용한다. 텍스트를 메모에 보관하지 않으며 문자 수 추정이나 입력 절단을 하지 않는다.
- PDF 작업자는 독립 HTTP 세션과 연결 풀을 재사용하고 문서 사이 쿠키를 초기화한다.
  카드사 요청 간격, 허용 호스트, 리디렉션 검증과 실제 호스트 상한은 전체 작업자가 공유한다.
- Worker 업로드 청크는 기본 8MiB다. Core publisher의 기존 호출 기본값은 1MiB다.
  파일 해시, PUT, 임시 객체 검증, MOVE, 최종 객체 검증을 유지하며 업로드 동시성은 늘리지 않는다.
  압축 전송과 MCP 데이터 마이그레이션은 포함하지 않는다.

## 설정과 롤백

| 환경변수 | 기본값 | 범위 |
|---|---:|---:|
| `CARDRAG_PDF_CONCURRENCY` | 8 | 1–32 |
| `CARDRAG_PDF_CONCURRENCY_PER_ISSUER` | 2 | 1–8 |
| `CARDRAG_LOCAL_PROCESSING_WORKERS` | 4 | 1–8 |
| `CARDRAG_STATE_SQLITE_CACHE_MIB` | 256 | 1–1024 |
| `CARDRAG_STATE_SQLITE_MMAP_MIB` | 2048 | 0–4096 |
| `CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB` | 8 | 1–16 |

성능 설정은 코퍼스/검색 계약 해시에 포함하지 않는다. 전체 PDF 동시성 `1`, 로컬 worker
`1`로 순차 실행할 수 있다. mmap `0`, SQLite 캐시 `2`, 청크 `1`로 기존 자원 설정에
가깝게 되돌릴 수 있다. 데이터 형식을 변경하지 않으므로 이전 Worker 이미지도 사용할 수 있다.

Compose의 중복 GC 선언을 제거하되 기존 최종 기본값인 `collect=false`, `grace=30`을
유지한다. 명시한 환경변수는 그대로 적용한다. Python 설정 단독 사용 시 기존 grace 기본값
1일도 유지한다. 이번 변경으로 원격 삭제를 실행하거나 활성화하지 않는다.

## 계측과 인수 기준

실행별 `runs/<run-id>/reports/performance.json`은 성공·실패·취소 모두 기록한다.
코퍼스/계약 해시, 적용 설정, 처리·미처리 수, 토큰/캐시 조회 횟수, 단계별 누적 시간,
process 최대 RSS, `/proc/self/io` 차이, PUT과 검증 GET 요청/본문 전송량/시간을 담는다.
계측 오류는 원래 실행 결과를 바꾸지 않는다. 비밀 값이나 OCR 원문은 기록하지 않는다.

`accumulated_seconds`는 병렬 작업 시간이 겹치므로 합산해서 전체 시간으로 사용하면 안 된다.
보고서 `elapsed_seconds`는 Pipeline 범위이며 CLI provider preflight를 제외한다.
CLI의 `Worker startup completed`와 `Worker execution finished` 로그는 시작 준비 및
전체 명령 소요 시간을 별도로 기록한다. 전체 배치 비교는 컨테이너 시작·종료 계측도 함께 사용한다.
`process_peak_rss_bytes`는 해당 프로세스 수명 전체의 high-water mark이며 컨테이너 전체
메모리와 동일하지 않다. 6GiB 기준은 별도 컨테이너 메모리 계측까지 함께 확인한다.

인수 기준은 다음과 같다.

1. 동일 코퍼스·초기 캐시·장비·리소스 제한·전송 조건에서 이전 정상 버전과 비교한다.
   테스트 중 운영과 쓰기 상태 볼륨·WebDAV 게시 채널을 공유하지 않는다.
2. 전체 빌드 대표 배치 2회 연속 75분 이하, 컨테이너 피크 메모리 6GiB 이하, 누락 0건을
   충족한다. 30분은 추가 목표다. 단순 `no_change`를 전체 빌드 단축 실적으로 계산하지 않는다.
3. 신규/변경 PDF, cache hit, 리비전, 지연/실패/재시작을 별도로 검증한다. 순차와 병렬 실행의
   Serving DB/벡터/문서 정합성을 비교한다. 실제 OCR 요청은 최대 동시 실행 1을 유지한다.
4. 회귀·타입·lint 검사를 통과하고 실제 시간·I/O·네트워크 결과를 남긴다. 대역폭 70% 절감을
   압축 없는 이번 릴리스의 검증된 성과로 간주하지 않는다.

현재 변경은 로컬 검증을 위한 구현이다. 운영 배치 75분/6GiB 인수 측정과 이미지 배포는
별도로 수행해야 하며, 단위/통합 테스트 통과만으로 실운영 성능을 달성했다고 판단하지 않는다.

2026-09-06 로컬 검증 결과: 전체 테스트 1,752개 통과(43.50초), mypy 소스 75개 통과,
Ruff lint·format 및 오프라인 lock 검사 통과. OCR fallback 장애 주입 테스트에서 예상된
RuntimeWarning 6건이 출력됐다. 변경 파일의 Gitleaks 검사에서도 비밀 값은 발견되지 않았다.
순차/병렬 실행의 DB·벡터 파일 바이트 일치, PDF 수집 장벽, OCR 최대 동시 실행 1,
미처리 문서의 게시 차단, 취소 시 스레드 종료 대기와 재실행, 선읽기 메모리 예약을 검증했다.

```bash
.venv/bin/python -m ruff check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/python -m ruff format --check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
.venv/bin/python -m mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
.venv/bin/python -m pytest
uv lock --check --offline
```
