# 015 실제 기동·신규 OCR·증분백업 점검

- 작성: 2026-10-15 17:29 KST, Codex.
- 요청: Worker 실기동 완료 후 정상 판정 여부, 신규 OCR 상품 및 증분백업 확인.
- 판정: **08시 복구 배치는 성공·게시·백업 완료, 수집은 degraded. 최종 수정 이미지의 전체 배치 완료는 아직 관측되지 않음.**

## 1. 실행별 구분

| 실행 | 이미지·명령 | 실제 관측 |
|---|---|---|
| 10/15 03:00 예약 | 수정 전 Worker, systemd `run` | 용량 gate 실패, exit 1. 현재 systemd Result=exit-code는 이 실패 기록 |
| 10/15 08:01:30–09:38:45 | `015-c635353`, `cardrag-worker-015-recovery`, `run` | exit 0, status succeeded, local_generation_published/published true. 실제 전체 배치 성공 |
| 10/15 13:19:33–13:19:57 | `015-741cfe0`, `cardrag-worker-015-741cfe0-startup`, `startup-check` | exit 0, ready. 초기화 검사만 실행했으며 전체 배치 성공 증거로 사용하지 않음 |

현재 실행 중 Worker 없음. 최종 수정 이미지 `015-741cfe0`의 full run을 별도로 새로 시작하지 않았다. 승인 문서의 인수는 코드/운영 반영/startup-check 기준이며 최종 이미지 full run의 정상 판정이 아니다.

전체 성공 run: `ec1255d924ab4c87a8a12491b762fc02`.
게시 generation: `g-ec1255d924ab4c87a8a12491-8f4256acf3e6`.
현재 MCP pointer도 이 generation이며 `/health/ready` 응답 `{"ready":true}` 확인.

## 2. OCR 처리와 상품 식별

- 전체 OCR 5,521 문서: succeeded 5,521 / failed 0 / missing 0.
- cache reused 5,520, provider called documents **1**. 이 지표는 provider를 호출한 문서 수이며 API 요청 횟수와 다르다.
- 해당 문서 ID: `doc_89498adcd38ba444b1478f39e105157c0b8b1ed05ba29c232fca09f41da57f20`.
- 카드사/상품: **우리카드 / 오케이몰 우리카드+ / 상품코드 103480**.
- 문서 유형: 상품설명서, **2쪽**, source version 3, effective date **2026-10-14**, 상품 출시일 2024-06-28.
- PDF: `https://pc.wooricard.com/upload/cardClause/2026/10/14/a1a6d662-5026-4409-8ab4-ff956c64cfb9.pdf`.
- PDF SHA256 `adf72c0021a76996555fa235992f9a0e75ec7f38a19806a861587fbef669c6a3`, 203,048 B.
- run 내 primary/native-manifest: succeeded, provider **codex-exec**, model **gpt-6.1-sol**, reasoning **medium**, 생성 **10/15 08:41:26 KST**, output 15,501 B / 7,108 chars.
- OCR SHA256 `1e8523461967f669747ff17ff1c6e8592a999fde5619dee010276137adc69020`.
- `corpus-diff.json`: new_products=0, successor_sources=1, replaced_predecessors=1, unchanged=5,059. 따라서 **신규 상품이 아니라 기존 상품설명서 개정에 따른 OCR**이다. 이전 document `doc_ca5a1ae48eb46dc993fc05dd3ef0432ff27b015c970a72eb8184b305670ba630`은 이력으로 유지.
- 현재 5,060 문서 + historical 461 = 5,521. pdf_downloads=0이므로 이 복구 실행에서 원본 PDF를 새로 다운로드한 것은 아니다.

## 3. 증분백업 점검

전체 배치 JSON과 backup ledger 대조:

- backup_status **succeeded**, backup_bytes_uploaded **235,835 B**, backup_requests **8**.
- last_backup_at **10/15 09:38:39 KST**, runs_since_last_backup=0.
- 현재 `backup_pending` 전체 행 수 **0**: pending뿐 아니라 lost_source 상태 행도 없음. oldest_pending_at=null.
- 이번 시간대 receipt **5개**, 합계 **235,835 B**:

| 항목 | bytes |
|---|---:|
| 원본 PDF (`adf72c…`) | 203,048 |
| 이번 OCR 본문 (`1e8523…`) | 15,501 |
| 동일 문서의 다른 OCR 본문 (`be681c…`) | 15,545 |
| native OCR manifest | 1,456 |
| native OCR READY | 285 |

5개 파일의 receipt는 모두 동일 상품/PDF와 연결된다. 두 OCR 본문이 보존되어 있으나 이를 이번 run에서 OCR 문서 2건을 처리한 것으로 해석하지 않는다. 다른 본문의 정확한 생성 실행은 이번 조사에서 별도 확정하지 않았다.

**독립 원격 검증:** production Compose의 secret/env를 사용하는 임시 점검 컨테이너에서 WebDAV GET만 수행했다. 위 5개 파일의 원격 byte 수와 SHA256이 모두 ledger와 일치했다. `v1/backup/index.json`(schema `cardrag.backup-index.v1`, updated 10/15 09:38:34 KST, 16,790 items)에도 5개 경로/크기/해시가 모두 등재됨을 확인했다. 업로드/flush/추론/전체 배치는 실행하지 않았다.

이는 해당 증분 OCR/PDF 백업의 정상 처리를 입증한다. 전체 serving DB/vector에 대한 원격 disaster restore 시험을 수행했다는 의미는 아니다.

## 4. 남은 문제와 정상 판정의 범위

- collection_status **degraded**, 8개 카드사 중 6개 수집 성공.
- 신한: `issuer_origin_network`, 4회 시도 후 실패, 기존 **931건** carry.
- 롯데: `issuer_source_invalid`, 4회 시도 후 실패, 기존 **647건** carry.
- 따라서 배치 완료·MCP 게시·OCR·이번 증분백업은 정상이나 **모든 카드사 최신 수집이 정상인 상태는 아니다**.
- structure_failed_document_count=0, structure_fallback_document_count=2. fallback은 기존 정책의 수용 결과이며 전 문서가 정밀 parser로 처리됐다는 뜻은 아니다.
- 10/16 03시 예약 전체 실행은 미래 관측 항목이다. 해당 실행 전에는 최종 수정 이미지 전체 배치가 정상 완료했다고 판정하지 않는다.

## 5. 근거와 조사 방식

- Docker inspect/logs의 recovery/startup 실행·종료 시각, 명령, exit code 및 최종 JSON.
- state run의 `reports/performance.json`, `reports/corpus-diff.json`, `reports/issuer-collection.json`, 대상 OCR primary/native-manifest.
- MCP current pointer 및 sealed generation의 SQLite contract_revisions/product_lineages 읽기 전용 조회.
- backup ledger receipt/meta/pending 읽기 전용 조회 + 대상 5개 원격 객체/인덱스 GET 검증.
- 기존 운영 데이터/설정/이미지를 수정하지 않고 이 점검 문서만 추가했다.
