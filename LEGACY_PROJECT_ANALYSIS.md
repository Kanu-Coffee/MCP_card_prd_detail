# CardRAG 레거시 프로젝트 분석 기록

## 0. 문서 목적과 분석 범위

- 분석일: 2026-08-11 (Asia/Seoul)
- 분석 대상 릴리스: `cardrag-conveyor-hatch-20260712T091451_KST`
- 목적: 이후 별도 MCP 서버 프로젝트를 설계·구현할 때 참조할 수 있도록 레거시의 실제 구조, 데이터 계약, 처리 흐름, 운영 상태, 재사용 가능 요소와 위험을 기록한다.
- 이번 단계에서 수행한 작업: 소스·테스트·manifest·운영 보고서·SQLite 스키마와 집계값을 읽기 전용으로 조사하고 이 문서만 신규 작성했다.
- 이번 단계에서 수행하지 않은 작업: 레거시 코드 수정, 데이터 attach, 의존성 설치, 외부 카드사/OpenRouter/Codex/Gmail 호출, MCP/Docker 구현.

이 문서에서 사용하는 경로 별칭은 다음과 같다.

```text
H = cardrag-conveyor-hatch-20260712T091451_KST
P = H/process-kit/cardrag-conveyor
D = H/data-kit/cardrag-conveyor-data
```

파일 근거는 `P/...:행번호` 또는 `D/...` 형식으로 적었다.

## 1. 한눈에 보는 결론

1. 현재 폴더는 일반 Git 소스 저장소가 아니라 코드와 약 9.51 GiB의 데이터가 분리된 이식용 `hatch-kit` 백업이다. `.git` 이력은 포함되어 있지 않다.
2. 레거시의 본질은 `카드사 공시 수집 → PDF 다운로드 → 고해상도 PNG 렌더링 → Codex CLI 비전 OCR → SQLite FTS/구조화/임베딩 구축 → CLI·이메일 Agent 검색`으로 이어지는 배치형 데이터 파이프라인이다.
3. 실행 표면은 Typer/argparse CLI와 운영 스크립트다. MCP 서버, MCP SDK, HTTP 서버, Dockerfile, Compose, health/readiness, 인증·인가 코드는 전혀 없다.
4. 현재 검색용 corpus는 우리카드와 KB국민카드 두 발급사만 지원한다. 성공 문서 1,592건 중 최신 문서 1,567건이 검색 인덱스에 반영되어 있다.
5. 구조화 데이터와 임베딩의 현재 hash coverage는 완전하지만, 임베딩 DB에는 과거 row 10,967건이 추가로 남아 있다.
6. 현행 vector 검색은 ANN 인덱스가 아니라 SQLite BLOB 전체를 Python에서 읽어 cosine을 계산한다. 문장 검색은 요청마다 최대 128,189개의 4,096차원 벡터를 순회하므로 온라인 MCP 요청 경로로 그대로 옮기기 어렵다.
7. `hybrid-search`는 FTS chunk ID와 structured section/unit ID가 서로 달라 실제 RRF 합산이 거의 일어나지 않는다. 현재 가중치와 후보 제한에서는 FTS 후보가 충분할 경우 vector 결과가 모두 잘릴 수 있다.
8. evidence/structured DB는 운영 중 테이블을 `drop`한 뒤 같은 파일에 전면 재구축한다. 온라인 읽기 서버와 ingestion 작업을 같은 DB 파일에서 병행할 수 있는 구조가 아니다.
9. 좋은 재사용 후보는 발급사 포함 문서 ID, portable path 검증, OCR-only 계약, canonical structured schema, content hash, 근거 ID 검증과 OpenRouter 재시도/응답 검증이다.
10. 신규 프로젝트에서는 읽기 전용 MCP 검색 런타임과 수집·OCR·재색인 배치를 별도 프로세스/권한/스토리지로 분리하는 것이 핵심 경계가 된다.

## 2. 백업 패키지의 성격과 디렉터리 구조

### 2.1 최상위 구조

```text
H/
├── HATCH_GUIDE.md
├── HATCH_PREFLIGHT_REPORT.md
├── RELEASE_MANIFEST.json
├── BACKUP_MANIFEST.md
├── hatch_rclone_backup_filter.txt
├── process-kit/cardrag-conveyor/       # Python 코드, 테스트, 운영 스크립트
└── data-kit/cardrag-conveyor-data/     # PDF, PNG, OCR, DB, 로그, 보고서, 산출물
```

`H/HATCH_GUIDE.md:7-22`와 `H/RELEASE_MANIFEST.json`도 동일한 process-kit/data-kit 분리 구조를 선언한다.

### 2.2 현재 물리 상태

| 항목 | 확인 결과 |
|---|---:|
| process-kit 크기 | 855,619 bytes |
| data-kit 크기 | 10,213,000,789 bytes, 약 9.51 GiB |
| data-kit 파일 수 | 9,551개 |
| `artifacts/` | 4,640,777,000 bytes |
| `data/` | 5,570,995,799 bytes |
| `reports/`, `logs/`, `outputs/` | 약 1.2 MB |

현재 `P/` 아래에는 `data`, `artifacts`, `reports`, `logs`, `outputs` 링크가 없다. 즉 기본 경로로 CLI를 바로 실행할 수 있는 상태가 아니며, `hatch/hatch_attach_data.py`로 data-kit을 symlink 또는 copy해야 한다.

주의할 점은 attach 유틸리티가 대상 디렉터리가 이미 있으면 재귀 삭제 후 교체한다는 것이다(`P/hatch/hatch_attach_data.py:11-24`). 신규 서버의 일반 요청 경로에서 호출할 성격의 도구가 아니다.

### 2.3 Git·릴리스 무결성

- 현재 폴더와 process-kit에는 `.git`이 없다. 변경 이력, tag, 원 커밋, blame 정보는 복구할 수 없다.
- `H/RELEASE_MANIFEST.json`은 릴리스 시각, 구조, 기준 건수와 제외 항목을 기록하지만 전체 파일의 checksum 목록이나 서명은 없다.
- master manifest의 PDF hash는 개별 원본을 검증하는 데 유효하지만, OCR/PNG/DB/control 문서를 모두 포괄하는 파일별 checksum manifest는 없다. 따라서 현재 패키지 전체를 byte-for-byte 재현하거나 변조 여부를 일괄 검증할 수는 없다.
- 별도 라이브러리 `P/src/wooricard_rag/release_bundle.py:25-92`에는 tar.gz checksum 검증 기능이 있으나, 현재 hatch 릴리스 manifest 형식에는 적용되어 있지 않다.
- `LICENSE` 파일도 발견되지 않았다. 코드와 카드사 공시 원문/PDF의 재배포·서비스 사용 조건은 신규 프로젝트 착수 전에 별도로 확인해야 한다.

## 3. 시스템 구성요소

| 영역 | 주요 경로 | 역할 | 현재 형태 |
|---|---|---|---|
| 발급사 공통 계약 | `P/src/cardrag_conveyor/` | 문서 정규화, ID, 경로, retry ledger | Python 라이브러리 |
| 발급사 adapter | `P/src/cardrag_conveyor/issuers/` | 우리카드/KB 수집·다운로드 | 동기식 `urllib` + 외부 명령 |
| RAG 코어 | `P/src/wooricard_rag/` | chunk, FTS, 구조화, embedding, vector 검색 | SQLite + Python |
| 요청 Agent | `P/src/cardrag_agent/` | 검색 façade, 근거 검증, queue, email, exporter | Typer CLI/backend scaffold |
| 배치 파이프라인 | `P/scripts/` | discovery, OCR, 전면 rebuild, daily run | argparse 스크립트 |
| 운영 보조 | `P/scripts/ops/` | KB 반복 실행·상태·완료 감시 | shell supervisor 전제 |
| 이식 도구 | `P/hatch/` | data attach, 설치/DB 검증 | 수동 CLI |
| corpus | `D/artifacts`, `D/data` | 원본·OCR·인덱스·임베딩 | 대용량 로컬 파일 |

`wooricard_rag`라는 옛 패키지명이 남아 있지만, README는 이를 다중 발급사 CardRAG로 확장한 상태라고 설명한다(`P/README.md:3-10`). 이 때문에 이름과 실제 책임이 완전히 일치하지 않는다.

## 4. 실제 end-to-end 데이터 흐름

```text
우리카드 공개 JSON / KB 공개 HTML
             │
             ▼
     issuer discovery adapter
             │ discovery JSON
             ▼
   공통 문서 정규화·manifest diff
             │ 신규/변경 문서만
             ▼
        PDF 다운로드·검증
             │
             ▼
   PyMuPDF 6배율 PNG 페이지 렌더
             │ 2페이지 단위
             ▼
    Codex CLI vision OCR → ocr.md
             │
             ▼
 master manifest → inventory SQLite
             │
             ▼
 OCR Markdown cache → evidence chunk + FTS
             │
             ▼
 규칙 기반 structured section/sentence + FTS
             │
             ▼
 OpenRouter Qwen3 4096d embedding SQLite
             │
             ├── cardrag CLI
             └── cardrag-agent CLI/email worker
```

운영 진입점은 `P/scripts/run_daily_monitor.py:230-314,343-392`다. 발급사 adapter를 선택하고, 발견 문서를 기존 master manifest 및 terminal ledger와 비교한 뒤 다운로드/OCR하고 전체 검색 계층을 다시 만든다.

### 4.1 현재 존재하지만 운영 경로와 분리된 코드

- `P/src/wooricard_rag/ocr_worker.py`와 `codex_backend.py`에는 prompt preset, portable artifact, invocation metadata를 위한 비교적 정돈된 계약이 있다.
- 실제 daily monitor는 이 계약을 사용하지 않고 `scripts/run_5_sample_pipeline.py`의 렌더/OCR 함수를 직접 import한다(`P/scripts/run_daily_monitor.py:16-25`).
- `pipeline_state.py`는 stage run, document version, artifact/event 상태 모델을 제공하지만 실제 daily monitor에서는 호출되지 않는다. 현재 운영 source of truth는 manifest, retry JSON, run report, 여러 status 파일로 분산되어 있다.

따라서 신규 프로젝트가 레거시 클래스를 그대로 묶는 방식보다, 실제 운영 경로와 계약 코드를 먼저 하나의 service layer로 정리할 필요가 있다.

## 5. 핵심 도메인 계약

### 5.1 원천 공시와 정규 문서

`SourceDisclosureRecord`의 주요 필드는 다음과 같다(`P/src/cardrag_conveyor/contracts.py:10-25`).

- `card_company`
- `product_code`, `product_name`
- `document_type`
- `effective_date`, `version`
- `source_url`, `source_post_id`, `source_file_name`, `source_file_size`
- `file_path`, `download_hint`

정규화된 `DisclosureDocument`는 다음 ID를 사용한다.

```text
{card_company}:{product_code}:{document_type}:{effective_date}:v{version}
```

근거: `P/src/cardrag_conveyor/contracts.py:105-123`.

artifact 경로도 발급사/상품/문서종류/버전 단위로 정규화된다.

```text
artifacts/raw-pdfs/{issuer}/products/{code}__{name}/{doc_type}/{date}__v{version}__{file}
artifacts/rendered-pages/{issuer}/products/{code}__{name}/{doc_type}/{date}__v{version}/
artifacts/ocr/{issuer}/products/{code}__{name}/{doc_type}/{date}__v{version}/ocr.md
```

`P/src/cardrag_conveyor/paths.py:31-71`은 절대경로, URL, NUL, `..` segment를 거부한다. 이 portable manifest용 lexical guard는 재사용 가치가 높다. 다만 `resolve_project_path()`는 정규화한 경로를 root와 단순 결합할 뿐 `resolve()` 후 root containment나 symlink 탈출을 검사하지 않으므로, 완전한 filesystem sandbox로 간주해서는 안 된다(`P/src/wooricard_rag/path_portability.py:20-49`).

다만 실제 OCR 운영 스크립트는 이 공통 함수를 일관되게 거치지 않고 `doc.product_name`을 경로에 직접 보간한다(`P/scripts/run_5_sample_pipeline.py:137-165`). 따라서 카드사 응답에 경로 구분자나 `..` 같은 값이 들어오면 공통 방어를 우회할 수 있다. 신규 코드에서는 artifact path 생성 경로를 하나로 강제해야 한다.

또한 `contracts.py`와 과거 `wooricard_rag/metadata.py`, `disclosure_crawler.py`에 날짜·버전·문서 정규화가 중복되어 있어 향후 drift 가능성이 있다.

### 5.2 발급사 adapter 계약

`IssuerAdapter`는 다음 callable 집합이다(`P/src/cardrag_conveyor/issuers/base.py:9-20`).

- `discover`
- `build_discovery_payload`
- `normalize`
- `download_pdf`
- 선택적 `sample_discovery`

registry에 등록된 코드는 `wooricard`, `kbcard` 두 개뿐이다(`P/src/cardrag_conveyor/issuers/registry.py:5-22`). Agent의 issuer 별칭에는 `samsungcard`도 있지만 실제 adapter/corpus는 없으므로 일관되지 않는다(`P/src/cardrag_agent/worker.py:19-29`).

### 5.3 최신본 의미

- inventory는 같은 `(card_company, product_code, doc_type)` 중 `effective_date`, `version`, `completed_at`의 문자열 최대값을 최신으로 표시한다(`P/src/wooricard_rag/inventory.py:68-80`).
- evidence DB는 `is_latest=1`, `status=done`, `product_description`만 인덱싱한다(`P/scripts/build_evidence_rag.py:158-177`).
- 버전을 문자열로 비교하므로 같은 날짜의 `v9`와 `v10`처럼 자릿수가 다른 값은 잘못 정렬될 가능성이 있다.
- 검색 응답에는 corpus generation이나 snapshot 시각이 없어 “어느 시점의 최신본인가”를 외부 호출자가 명확히 알기 어렵다.

## 6. 발급사별 수집과 다운로드

### 6.1 우리카드

- 공개 페이지/JSON을 이용해 상품과 상품안내장 이력을 수집한다.
- live PDF 다운로드는 RAONK 형식의 payload를 만들고 시스템 `openssl`로 AES-CBC 처리한 뒤 POST한다(`P/src/cardrag_conveyor/issuers/wooricard.py:64-115`).
- 소스에 고정 프로토콜 키와 sample용 사전 계산 download payload가 포함되어 있다. 일반 사용자 credential로 보이지는 않지만 외부 공개·재사용 전에는 성격과 허용 범위를 재확인해야 한다.
- `openssl` 실행파일은 실제 필수인데 `pyproject.toml`이나 target requirements에 명시되어 있지 않다.
- adapter가 `scripts.discover_wooricard_pdfs`와 `scripts.run_5_sample_pipeline`을 동적으로 import한다(`P/src/cardrag_conveyor/issuers/wooricard.py:26-45`). 그런데 wheel에는 `src/...` 세 패키지만 포함된다(`P/pyproject.toml:28-29`). source checkout에서는 동작해도 wheel 설치형 컨테이너에서는 live/sample 기능이 깨질 수 있다.

### 6.2 KB국민카드

- 개인신용, 개인체크, 기업신용, 기업체크, 국제브랜드의 5개 분류를 순회한다.
- listing/detail HTML을 정규식으로 파싱한다(`P/src/cardrag_conveyor/issuers/kbcard.py:117-179`). 사이트 markup 변경에 취약하다.
- 기본 discovery는 현재본만, `include_history` 사용 시 과거 등록 PDF까지 수집한다(`P/src/cardrag_conveyor/issuers/kbcard.py:295-329`).
- discovery가 제공한 URL을 직접 열고 전체 응답을 메모리에 읽은 뒤 `%PDF` magic만 확인한다(`P/src/cardrag_conveyor/issuers/kbcard.py:337-350`). 향후 외부 입력 tool로 노출한다면 도메인 allowlist, redirect 검증, 크기 제한이 필수다.

### 6.3 공통 다운로드 한계

- 동기식 네트워크 호출이다.
- HTTP 응답 크기 제한, 다운로드 streaming, 최종 redirect host 검증, 세밀한 MIME/PDF 구조 검사가 없다.
- 카드사 endpoint 호출의 rate limit/backoff 정책이 명시적이지 않다.
- 공개 웹 페이지 파싱·다운로드의 이용 조건을 나타내는 문서가 없다.

## 7. 렌더링과 OCR

### 7.1 실제 동작

- PyMuPDF로 기본 `6.0` 배율 PNG를 만든다(`P/scripts/run_5_sample_pipeline.py:27-30,145-157`).
- 기본 2페이지씩 Codex CLI에 순차 제출한다.
- chunk당 기본 timeout은 600초, reasoning effort는 `high`, sandbox는 `danger-full-access`다(`P/scripts/run_5_sample_pipeline.py:199-250`).
- 성공 조건은 chunk 첫 페이지의 `## Page N` 표식과 500자 이상 여부가 핵심이다. 페이지 누락률, 표 구조, 숫자 정확도 같은 품질 검사는 없다.
- 모든 chunk가 성공하면 `ocr.md`를 합치고 SHA-256, 문자 수, 페이지 수, 렌더 배율 등을 `metadata.json`에 기록한다(`P/scripts/run_5_sample_pipeline.py:256-295`).

### 7.2 provenance 한계

- 환경변수 `CARDRAG_OCR_MODEL` 값은 metadata에 기록되지만 실제 operational Codex 명령에는 `-m` 옵션이 없다. 기록된 label과 Codex CLI가 실제 선택한 모델이 다를 수 있다.
- metadata는 `reasoning_effort_requested`만 기록하고 실제 적용값을 검증하지 않는다.
- 우리카드 문서 중 861건은 외부 자산 import schema다. OCR 모델·방식·렌더 배율·렌더 이미지 목록은 없지만, 페이지 수(`pages`)와 raw PDF/OCR SHA-256 필드는 보존되어 있다. 실제 OCR hash 검증 결과는 9.3절에 별도로 기록했다.

### 7.3 corpus 내 렌더/OCR 현황

| 구분 | 우리카드 | KB국민카드 |
|---|---:|---:|
| 성공 OCR metadata | 915 | 677 |
| 현 파이프라인 `ocr_result_manifest.v2` | 54 | 677 |
| 외부 import metadata | 861 | 0 |
| metadata가 가리키는 렌더 이미지가 모두 존재 | 54 | 677 |

추가 관찰:

- 렌더 PNG는 총 2,943개, 약 2.95 GB다.
- 우리카드 렌더 252개는 현재 issuer-scoped 구조가 아닌 옛 top-level product 폴더에 있다.
- KB OCR 경로에는 성공 manifest 외의 `ocr.md` 및 chunk 임시 산출물이 남아 있다. directory 존재 여부보다 master manifest를 성공 여부의 기준으로 삼아야 한다.

## 8. manifest, 증분 처리와 실패 정책

### 8.1 master manifest

- schema: `cardrag_master_manifest.v2`
- 성공 entry: 1,592건, 모두 `status=done`
- key: `{issuer}:{product_code}:{doc_type}:{effective_date}:v{version}`
- PDF hash가 양쪽에 있으면 hash를 비교하고, 없으면 정규화한 filename과 filesize로 변경 여부를 판정한다(`P/src/wooricard_rag/daily_monitor.py:67-106,130-146`).
- live discovery에 size/hash가 없으면 같은 filename을 변경 없음으로 볼 수 있어 원본 내용이 교체된 경우를 놓칠 수 있다.
- 저장은 임시 파일 후 atomic rename이 아니라 대상 JSON에 직접 `write_text`한다(`P/src/wooricard_rag/daily_monitor.py:124-127`). 중단 시 manifest 손상 가능성이 있다.
- 처리 성공 시 upsert만 하며 discovery에서 사라진 과거 entry를 제거하지 않는다.

### 8.2 retry/terminal policy

기본 retry budget은 다음과 같다(`P/src/cardrag_conveyor/retry_state.py:13-19,35-55`).

| 분류 | terminal 전 시도 수 |
|---|---:|
| `download_not_pdf` | 2 |
| `ocr_incomplete` | 3 |
| `transient` | 5 |
| `unknown` | 3 |
| `infrastructure` | 제한 없음 |

ledger 저장 자체는 `.tmp` 후 `replace` 방식이지만 프로세스 간 file lock은 없다. 여러 monitor가 동시에 실행되면 read-modify-write lost update 가능성이 있다.

현재 KB discovery 724건의 최종 accounting은 다음과 같다.

| 상태 | 건수 |
|---|---:|
| 성공 manifest | 677 |
| terminal failure | 47 |
| 합계 | 724 |
| 최신 run의 pending retryable | 0 |

terminal 47건은 `ocr_incomplete` 42건, `download_not_pdf` 5건이다. terminal key는 이후 자동으로 건너뛰며 정상 재시도/해제를 위한 공용 API는 없다.

retry ledger에는 terminal 47건 외에 과거 `transient/retryable` 1건이 남아 있지만 최신 완료 report의 pending은 0이다. 따라서 retry JSON 자체도 현재 실행 상태라기보다 누적 이력으로 해석해야 한다.

## 9. 실제 데이터와 SQLite 계층

### 9.1 corpus 범위

| 지표 | 우리카드 | KB국민카드 | 합계 |
|---|---:|---:|---:|
| inventory 문서 버전 | 915 | 677 | 1,592 |
| 최신 문서/상품 | 890 | 677 | 1,567 |
| evidence chunks | 13,354 | 5,315 | 18,669 |
| structured sections | 14,123 | 6,195 | 20,318 |
| sentence units | 77,451 | 40,967 | 118,418 |

- 전체 effective date 범위는 2018-05-08부터 2026-07-03까지다.
- 릴리스 생성일은 2026-07-12이고 분석일은 2026-08-11이다. 즉 현재 data-kit은 최신 실시간 정보가 아니라 명시적인 과거 snapshot이다.
- 우리카드 discovery JSON은 892건, KB discovery JSON은 724건이다. master manifest는 과거 버전과 성공 이력을 유지하므로 discovery 수와 inventory 수가 단순히 같지 않다.

### 9.2 주요 DB

| 파일 | 역할 | 핵심 row | 크기 |
|---|---|---:|---:|
| `inventory.sqlite3` | 문서 버전·latest·index 상태 | documents 1,592 | 1,593,344 B |
| `ocr_inventory.sqlite3` | inventory 복제본 | documents 1,592 | 1,593,344 B |
| `evidence_inventory.sqlite3` | OCR chunks, token, FTS5 | chunks 18,669 / tokens 853,113 | 256,241,664 B |
| `structured_sections.sqlite3` | section/sentence와 각 FTS5 | sections 20,318 / units 118,418 | 606,199,808 B |
| `api_embeddings_structured.sqlite3` | 4,096d float32 BLOB | 149,703 | 2,568,368,128 B |
| `cardrag_agent_jobs.sqlite3` | email job queue | jobs 9 | 36,864 B |

모든 주요 DB의 journal mode는 `DELETE`이며 schema migration version인 `PRAGMA user_version`은 0이다.

`inventory.sqlite3`와 `ocr_inventory.sqlite3`는 현재 SHA-256까지 동일한 별도 복제 파일이다. 향후 두 파일을 독립 source of truth로 취급하면 drift가 발생할 수 있으므로 하나의 canonical inventory와 명확한 snapshot 규칙이 필요하다.

### 9.3 원본·artifact 무결성 관찰

- raw PDF는 1,634개이며 내용 hash 기준 unique 파일은 1,405개다. 즉 같은 내용의 중복 인스턴스가 229개 있다.
- master manifest 1,592건의 `pdf_sha256`은 모두 로컬 raw PDF 중 하나와 일치했다. manifest가 직접 가리키는 raw PDF 경로 861건도 전부 해당 hash와 일치했다.
- 나머지 731건(우리카드 54, KB 677)은 master manifest에 `raw_pdf_rel_path`가 없다. 실제 처리 entry 생성도 다운로드 경로를 저장하지 않으므로(`P/scripts/run_daily_monitor.py:75-100`), PDF hash와 source URL은 있어도 manifest만으로 로컬 원본을 직접 resolve할 수 없다. 신규 generation manifest는 모든 artifact의 self-contained provenance를 가져야 한다.
- manifest가 가리키는 `ocr.md`와 `metadata.json`은 1,592건 모두 존재했다.
- metadata의 `ocr_md_chars`는 실제 OCR text 길이와 1,592건 모두 일치했다. 반면 master manifest의 `ocr_chars`는 1,537건만 일치하고 KB 문서 55건은 최대 147자 범위에서 차이가 있었다. 이는 OCR 본문 훼손의 직접 증거라기보다 master manifest counter drift다.
- metadata의 `ocr_md_sha256`은 실제 `ocr.md`와 1,591/1,592건 일치했고, imported 우리카드 문서 1건이 불일치했다. 신규 snapshot 검증에서는 이 예외를 해소하고 OCR content hash를 canonical 기준으로 삼아야 한다.
- raw PDF 외 PNG, OCR, DB, 보고서까지 포함하는 전체 checksum 목록이 없으므로 위 검증만으로 data pack 전체 무결성을 보증할 수는 없다.

### 9.4 structured schema

`structured_sections`는 다음 검색 메타데이터를 보존한다(`P/src/wooricard_rag/structured_sections.py:11-91`).

- issuer, product code/name, document/effective date
- section type, canonical group, benefit kind, impact scope
- title/normalized title, parent section
- source chunk, line 범위, 원문, canonical text, source path
- confidence, extraction method

분류는 LLM이 아니라 제목/본문 keyword와 정규식 규칙이다. `performance_exclusion`도 문장 단위 regex 추출이며 confidence는 규칙별 고정값이다(`P/src/wooricard_rag/structured_sections.py:224-345`). 이를 확정적 금융 규칙 parser로 해석하면 안 된다.

### 9.5 embedding 상태

| item type | 현재 structured row | embedding DB row | 현재 text hash+dim 일치 | 과거 extra |
|---|---:|---:|---:|---:|
| section | 20,318 | 21,514 | 20,318 | 1,196 |
| sentence | 118,418 | 128,189 | 118,418 | 9,771 |
| 합계 | 138,736 | 149,703 | 138,736 | 10,967 |

- provider: `openrouter`
- model: `qwen/qwen3-embedding-8b`
- dimension: 4,096
- `api_chunk_embeddings`는 0건이며 raw chunk vector path는 폐기된 상태다.

현재 text hash coverage는 완전하다. 다만 search는 hash가 현재 structured text와 맞는지 확인하지 않고 과거 row까지 먼저 점수 계산하므로 stale vector가 top candidate 공간을 차지할 수 있다.

공식 `structured_embedding_counts()`도 current ID/text hash와 join하지 않고 provider/model/item type의 전체 embedding row를 `done`으로 센다(`P/src/wooricard_rag/api_embeddings.py:244-267`). 현재 snapshot에 적용하면 section pending은 `-1,196`, sentence pending은 `-9,771`이 된다. daily report/readiness는 단순 row 수가 아니라 current hash+model+dimension coverage를 기준으로 계산해야 한다.

### 9.6 legacy archive와 잔존 데이터

- `D/data/db/legacy-archive-20260707T212249_KST/`가 약 1.97 GiB를 차지한다.
- 현재 runtime DB와 이름이 같은 과거 inventory/evidence/structured/embedding DB가 들어 있다.
- 테스트는 legacy DB fallback을 금지하지만, Docker volume을 넓게 마운트하거나 경로를 잘못 지정하면 혼동 가능성이 있다.
- `DATA_PACK_MANIFEST.json`의 `sqlite_dbs` 목록은 주요 4개만 적고 실제 `ocr_inventory`, agent jobs, legacy archive, report용 DB들은 열거하지 않는다. data pack 내용을 완전한 보안 inventory로 간주하면 안 된다.

### 9.7 상태 필드 해석 주의

`inventory.index_state` 1,592건이 모두 `needs_reindex`다. 이는 OCR-only entry에 guide가 없으면 그렇게 매핑하는 레거시 규칙(`P/src/wooricard_rag/metadata.py:100-111`) 때문이며, 실제 evidence/structured/embedding 생성 완료 여부와 일치하지 않는다. 서비스 readiness에 이 필드를 그대로 사용하면 안 된다.

## 10. 검색 계층

### 10.1 FTS 검색

`cardrag search-chunks`는 다음 순서다.

1. optional YAML 사전으로 query expansion
2. SQLite FTS5 `unicode61` 검색
3. section type, exact term, OCR source에 수동 boost
4. 상위 결과 출력

근거: `P/src/wooricard_rag/cli.py:90-115`, `chunk_index.py:214-237`, `ranking.py:49-77`.

현재 `P/config/`에는 `.gitkeep`만 있고 `user_dictionary.yaml`이 없다. 따라서 실제 기본 실행에는 사용자 사전 확장이 없다.

또한 Kiwi noun과 dictionary token은 `chunk_tokens`에 저장되지만 검색 SQL은 `chunk_fts`만 조회한다. CLI 설명의 `FTS/Kiwi`와 달리 저장된 Kiwi token이 ranking에 직접 사용되지 않는다.

### 10.2 vector 검색

동작은 다음과 같다(`P/src/wooricard_rag/api_vector_search.py:131-207`).

1. OpenRouter 등으로 query vector 1개 생성
2. 해당 item type의 모든 embedding BLOB을 SQLite에서 순차 읽기
3. Python에서 vector norm과 cosine 계산
4. heap으로 후보를 고른 뒤 structured DB를 별도 connection으로 조회
5. metadata join 단계에서 section type 적용

ANN index나 vector extension은 없다.

대략적인 매 요청 scan 규모:

- section: 21,514 × 4,096 float32, 약 336 MiB의 raw vector 값
- sentence: 128,189 × 4,096 float32, 약 2.0 GiB의 raw vector 값
- 둘 모두: 149,703 × 4,096 = 613,183,488차원 연산 대상

이는 동시 요청, CPU 사용량, 파일 page cache, container memory/IO에 큰 부담이 된다.

### 10.3 `cardrag-agent` 검색은 hybrid가 아니다

`CardRAGService.search`는 기본 `item_type=section`인 structured vector 검색만 호출한다(`P/src/cardrag_agent/service.py:97-166`). FTS fallback이나 hybrid 조합은 없다. 따라서 기본 Agent 검색은 `OPENROUTER_API_KEY`와 외부 query embedding 호출에 의존한다.

section type은 vector heap 후보 선정 뒤 structured metadata join에서 적용되고, issuer는 `CardRAGService.search()`가 그 결과에 다시 후처리한다(`P/src/wooricard_rag/api_vector_search.py:131-189`, `P/src/cardrag_agent/service.py:123-161`). 따라서 특정 issuer/section의 정확한 top-k를 보장하지 않으며, 후보 배수로만 완화한다.

요청의 `item_type`과 `limit`에도 명시적인 model-level 제약이 없다. 하위 검색기는 `section`이 아닌 item type을 모두 sentence 경로로 취급하므로 MCP 입력 schema에서 enum과 범위를 강제해야 한다(`P/src/cardrag_agent/service.py:16-29`, `P/src/wooricard_rag/api_vector_search.py:167-170`).

### 10.4 현행 hybrid의 구조적 결함

`cardrag hybrid-search`는 FTS 결과와 section+sentence vector 결과를 RRF로 합친다(`P/src/wooricard_rag/cli.py:118-195`). 그러나:

- FTS 식별자는 chunk ID다.
- vector 식별자는 structured section ID 또는 sentence unit ID다.
- 서로 다른 ID 공간이므로 같은 의미의 근거여도 RRF 점수가 같은 key에 합산되지 않는다.
- source weight는 FTS 4.0, vector 0.75다.
- `k=60`, fusion 후보 100일 때 FTS 100위 점수는 `4/160=0.025`, vector 1위는 `0.75/61≈0.0123`이다.

또한 section과 sentence 검색이 각각 embedder wrapper를 호출해 동일 query embedding을 두 번 OpenRouter에 요청한다(`P/src/wooricard_rag/cli.py:140-160`, `P/src/wooricard_rag/api_vector_search.py:210-231`). 즉 hybrid 요청 하나가 외부 embedding 2회와 vector full scan 2회를 유발한다. 신규 검색 계층은 query vector를 한 번 생성해 두 index에 재사용해야 한다.

따라서 FTS 후보가 100개 이상이면 상위 100개가 전부 FTS로 채워져 vector 후보가 최종 product dedupe 전에 사라질 수 있다. 현재 unit test는 동일 ID를 두 branch에 넣는 synthetic case라 이 실제 통합 결함을 잡지 못한다.

### 10.5 multi-issuer와 evidence 계약 문제

- FTS `ChunkSearchResult`와 hybrid output에는 `card_company`가 없다.
- product dedupe key는 `(issuer, product_code)`가 아니라 `product_code` 하나다(`P/src/wooricard_rag/hybrid_search.py:81-91`). 카드사 간 코드가 같으면 충돌할 수 있다.
- structured result에는 issuer가 있지만 CLI의 search-like 변환 과정에서 버린다.
- Agent evidence의 `quote`는 full source가 아니라 canonical/context text 앞 420자다.
- sentence 결과의 `source_path`는 빈 문자열이다.
- page 번호, 정확한 line 범위, source URL, text hash, corpus generation이 evidence 응답에 없다.
- `evidence_id`는 stable section ID가 아니라 issuer+item ID+현재 rank의 SHA-1 일부다. 순위가 바뀌면 같은 근거의 ID도 바뀐다(`P/src/cardrag_agent/service.py:169-171`).
- ID로 전체 원문을 다시 읽는 공용 API가 없다.

MCP resource/citation 계약으로 사용하기 전에 보완해야 할 부분이다.

### 10.6 기타 검색 품질 한계

- chunk는 Markdown heading 기준이며 명시적 최대 길이/token budget이 없다.
- ranking boost는 `monthly_limit`, `benefit_exclusion`을 기대하지만 taxonomy는 주로 `benefit_limit`, `benefit_notice`를 만든다.
- 평가 코드는 expected product code의 recall@k 정도만 계산하며 실제 batch/CI와 연결되지 않는다.
- 검색 score threshold, abstention 정책, 재현 가능한 index generation ID가 없다.

## 11. CardRAG Agent와 이메일 자동화

### 11.1 요청 처리

1. 제목과 본문, 첨부 파일명만 `RequestContext`로 합친다. 첨부 내용은 읽지 않는다.
2. 제목이 `[hermes cardrag]`로 시작할 때만 CardRAG route다.
3. keyword seed planner가 issuer, 검색문, 출력 형식을 추론한다.
4. vector evidence bundle을 수집한다.
5. DB 자체가 아닌 evidence bundle을 `codex exec` reasoner에 전달한다.
6. LLM JSON 결과의 evidence ID를 검증한다.
7. Markdown과 선택적 XLSX/DOCX를 만든다.
8. Gmail 요청이면 원 `From`의 단일 주소에만 reply한다.

근거: `P/src/cardrag_agent/worker.py:50-164,167-300`, `P/src/cardrag_agent/mail.py:167-215`.

### 11.2 긍정적인 방어

- CardRAG reasoner는 DB를 직접 받지 않고 제한된 evidence bundle만 받는다.
- JSON Schema로 `summary`와 `conclusions[]` 형식을 제한한다.
- 결론의 evidence ID가 bundle 내부에 있는지 fail-closed 방향으로 검사한다.
- Gmail은 original requester 한 명에게만 reply하고 CC/BCC를 지원하지 않는다.
- subprocess는 shell 문자열이 아닌 argv list를 주로 사용한다.
- stdlib 기반 XLSX/DOCX exporter라 별도 office 라이브러리가 필요 없다.

### 11.3 한계와 위험

- summary의 근거성은 검증하지 않는다.
- `confidence`에 enum 제한이 없다.
- 최종 validation 실패 fallback을 다시 검증하지 않는다.
- Codex reasoner는 `read-only` sandbox지만 project root 전체를 cwd로 제공한다. 발신자 allowlist/auth가 없는 비신뢰 email과 결합하면 prompt injection을 통한 내부 파일 노출 위험이 있다.
- `process-next`와 `run-gmail-once`의 `send_reply` 기본값은 `True`다.
- JSON mailbox와 Gmail 모두 queue에는 `source=email`로 들어간다. JSON fixture도 message ID가 있으면 Gmail reply 경로로 들어갈 수 있다.
- Gmail body를 `gws-api --body <본문>` argv에 넣어 process list나 실패 로그에 본문이 노출될 수 있다.
- attachment는 파일명 metadata만 planner에 전달하지만, exporter가 만든 파일 경로는 실제 메일 첨부 인자로 전달한다.
- `telegram_alert`는 반환 JSON 필드일 뿐 실제 Telegram 전송 구현이 아니다.
- Codex reasoner의 임시 schema/output 파일 정리는 `subprocess.run` 이후 구간에 있어 timeout 예외가 나면 임시 파일이 남을 수 있다(`P/src/cardrag_agent/worker.py:180-213,225-257`).

### 11.4 queue 동시성

jobs 상태는 `queued → running → sent|completed|failed`를 기대한다. 그러나:

- `next_queued()` SELECT와 `mark_running()` UPDATE가 별도 transaction이다(`P/src/cardrag_agent/queue.py:92-116`).
- 다중 worker가 같은 job을 동시에 claim할 수 있다.
- lease, worker ID, heartbeat, attempt count, stuck-running recovery가 없다.
- 상태 컬럼에 DB CHECK가 없고 전이 규칙도 강제하지 않는다.
- queue DB에는 sender, subject, body, metadata가 평문으로 저장된다.

현재 data-kit에는 sent 상태의 email job 9건과 여러 mailbox/job demo DB, email 산출물이 함께 들어 있다. 카드 공시 검색용 runtime volume과 분리해야 할 데이터다.

## 12. DB rebuild와 온라인 동시성

daily monitor는 신규 문서를 처리한 뒤 다음을 수행한다.

1. inventory DB의 `documents`, `index_state`를 drop/recreate
2. OCR cache 디렉터리 전체 삭제·복사
3. evidence DB의 chunk/token/FTS 테이블 drop/recreate
4. structured DB의 section/sentence/FTS 테이블 drop/recreate
5. embedding DB에 pending hash를 추가 저장

근거: `P/src/wooricard_rag/inventory.py:102-123`, `P/scripts/run_daily_monitor.py:103-120`, `P/scripts/build_evidence_rag.py:181-220`, `P/src/wooricard_rag/structured_sections.py:446-479`.

이 구조의 영향:

- 같은 DB를 읽는 MCP 요청이 table 없음, lock, 부분 데이터, 세대 불일치를 볼 수 있다.
- sections DB와 embeddings DB를 서로 다른 connection/시점에 열어 snapshot 일관성도 보장되지 않는다.
- rebuild가 no-change daily run에서도 기본 활성화되어 비용이 크다.
- 실패 시 이전 세대로 원자 rollback하는 generation/symlink swap이 없다.
- 여러 replica가 shared writable volume에서 동시에 monitor를 돌릴 수 없다.
- embedding 증분 loop도 batch 256개마다 structured source 전체를 다시 읽고 모든 ID를 거대한 `IN (...)`으로 hash 조회한 뒤 batch만 자른다(`P/src/wooricard_rag/api_embeddings.py:166-216`, `P/scripts/run_daily_monitor.py:142-161`). 대규모 corpus에서는 반복 전수 비교와 SQLite parameter limit가 병목/실패 요인이 될 수 있다.

신규 온라인 검색 서버는 완성·검증된 immutable DB generation을 읽기 전용으로 열고, 배치가 새 generation을 별도 경로에서 만든 뒤 원자적으로 교체하는 형태가 필요하다.

## 13. 설정과 외부 의존성

### 13.1 Python package

- Python `>=3.11`
- runtime dependencies: Pydantic, Typer, Rich, PyYAML, KiwiPiePy, PyMuPDF
- optional `local-embeddings`: SentenceTransformers 계열
- dev: pytest, Ruff
- lock file: `uv.lock`, 총 74 package entry

근거: `P/pyproject.toml:1-33`.

Pydantic은 현재 src/scripts에서 실질적으로 사용되지 않으며 request/result는 dataclass 위주다.

FTS 검색은 SQLite의 optional compile feature인 FTS5에 의존한다(`P/src/wooricard_rag/chunk_index.py:39-51`, `structured_sections.py:62-90`). 이는 Python dependency 목록만으로 보장되지 않으므로 Docker base image 선정 및 readiness에서 실제 FTS5 사용 가능 여부를 검사해야 한다.

### 13.2 외부 실행파일·서비스

| 의존성 | 용도 | 현재 필요 조건 |
|---|---|---|
| `uv` | 설치, CLI, 운영 subprocess | 설치/운영 전반 |
| `codex` CLI | 신규 OCR, email reasoner, optional embedding | 해당 기능 사용 시 인증 필요 |
| `openssl` | 우리카드 RAONK download payload | 우리카드 live download |
| OpenRouter | query/document embedding | vector 검색·증분 embedding |
| `gws-api` | Gmail 읽기, label, 발송 | 이메일 자동화 |
| `rclone`/WebDAV | 과거 자산 import·backup transport | runtime 검색에는 불필요 |
| 카드사 공개 HTTP endpoint | discovery/PDF | ingestion 배치 |

### 13.3 환경변수와 불일치

| 변수 | 사용 목적 | 비고 |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter embedding | process env 또는 HOME 계열 `.env`를 직접 탐색 |
| `CARDRAG_OCR_MODEL` | OCR metadata label | 실제 Codex `-m`에는 연결되지 않음 |
| `CARDRAG_RENDER_SCALE` | PDF 렌더 배율 | 기본 6.0 |
| `CARDRAG_CODEX_TIMEOUT` | OCR chunk timeout | 기본 600초 |
| `CARDRAG_OCR_CHUNK_SIZE` | OCR 페이지 묶음 | 기본 2 |
| `GWS_API_BIN` | template상 Gmail command | 실제 코드는 기본 문자열 `gws-api` 사용 |
| `CODEX_BIN` | template상 Codex command | 실제 코드는 `codex` 하드코딩 |

`P/.env.template`은 project `.env` 복사를 안내하지만 일반 dotenv loader는 없다. OpenRouter key 탐색은 process env와 `HERMES_HOME`, `HERMES_REAL_HOME`, `HOME`의 `.env`만 본다(`P/src/wooricard_rag/api_embeddings.py:270-297`). 컨테이너에서는 secret env/file 경계를 명시적으로 다시 정의해야 한다.

### 13.4 cwd와 경로 전제

대부분의 기본 DB 경로는 `data/db/...` 같은 cwd-relative 값이다. 현재 process-kit에는 data link가 없으므로:

- 실행 위치가 달라지면 새 빈 SQLite 파일이 엉뚱한 곳에 생길 수 있다.
- 검색 함수 중 일부는 read path에서도 `CREATE TABLE IF NOT EXISTS`를 실행한다.
- read-only filesystem/volume 정책과 충돌할 수 있다.

신규 서버에서는 모든 storage root를 명시적 설정으로 받고, query path는 SQLite URI의 read-only/immutable mode로 여는 편이 안전하다.

## 14. 운영 상태와 이번 검증 결과

### 14.1 릴리스 문서가 주장하는 기준

`H/HATCH_PREFLIGHT_REPORT.md`와 `H/HATCH_GUIDE.md`는 다음을 기록한다.

- 테스트: 199 passed
- KB accounting: 724/724
- pending retryable: 0
- KB terminal: 47
- 주요 SQLite integrity: ok

### 14.2 이번 분석에서 읽기 전용으로 재검증한 내용

| 검사 | 결과 |
|---|---|
| Python 파일 AST parse | 조사 대상 전체 parse 성공 |
| main manifest schema/count | v2, 1,592건 |
| manifest artifact 경로 | `ocr.md`/`metadata.json` 1,592건 모두 존재 |
| manifest PDF hash | 1,592건 모두 로컬 raw PDF와 일치; 직접 경로 861건도 일치 |
| metadata `ocr_md_chars` vs 실제 OCR 문자 수 | 1,592건 모두 일치 |
| master manifest `ocr_chars` vs 실제 OCR 문자 수 | 1,537건 일치, KB 55건 불일치(최대 147자 차이) |
| metadata `ocr_md_sha256` vs 실제 OCR hash | 1,591건 일치, imported 우리카드 1건 불일치 |
| inventory/evidence/structured row | 릴리스 baseline과 일치 |
| embedding current hash+dim coverage | section 20,318/20,318, sentence 118,418/118,418 |
| 현행 핵심 6 DB와 legacy archive 5 DB `PRAGMA integrity_check` | 모두 `ok` |
| 별도 로컬 주요 4 DB `PRAGMA quick_check` | 모두 `ok` |
| forbidden secret filename scan | `.env`, OAuth token, rclone config 등 발견되지 않음 |
| 실제 MCP/Docker 관련 파일/의존성 | 없음 |

### 14.3 테스트를 재실행하지 않은 이유

현재 host에는 `uv`가 없고 system Python 환경에는 Pydantic, Typer, KiwiPiePy, PyMuPDF, pytest 등이 설치되어 있지 않다. 이번 단계의 “내용 파악만” 범위를 지키기 위해 의존성 설치나 `.venv` 생성을 하지 않았다.

따라서 `199 passed`는 이 릴리스가 만든 preflight 기록이며 이번 host에서 독립 재현한 결과는 아니다. 현재 test source에는 60개 파일, 정적 집계상 189개의 `test_*` 함수가 있고 parametrization을 포함한 과거 실행 결과가 199 case인 것으로 해석된다.

### 14.4 status 파일의 시간성

`D/reports/kbcard_conveyor/status.json`은 2026-07-08 시점 `running` snapshot을 담고 있지만 이후 2026-07-11 run report는 `accounted_total=724`, `pending_retryable=0`이다. status JSON을 현재 live process 상태로 해석하면 안 된다. 신규 상태 API에는 run ID, issuer, generation, started/finished timestamps와 terminal 상태를 명시해야 한다.

또한 daily run report 자체에는 issuer와 명시적 run ID가 없고, KB 반복 runner는 공용 `reports/daily_runs`에서 최신 파일을 issuer filter 없이 읽는다(`P/scripts/run_daily_monitor.py:249-268`, `P/scripts/ops/kbcard_full_conveyor_runner.py:30-50`). 여러 카드사 배치를 병행하면 다른 issuer의 report를 KB 상태로 오인할 수 있다.

## 15. 보안·개인정보·운영 경계

### 15.1 확인된 긍정 요소

- 실제 `.env`, Google OAuth token, rclone config, Codex/Hermes auth 파일은 백업 제외 정책에 포함되어 있고 현재 금지 filename도 발견되지 않았다.
- portable path validator가 존재한다.
- PDF 응답은 최소한 `%PDF` magic을 검사한다.
- Gmail은 단일 original requester reply 정책이다.
- evidence ID validation이 존재한다.

### 15.2 신규 서비스 전에 해결할 위험

| 위험 | 근거/영향 |
|---|---|
| OCR `danger-full-access` | 비신뢰 MCP 요청에서 직접 호출 금지 |
| sender authorization 부재 | 제목 prefix만 맞으면 email 자동화 후보가 됨 |
| prompt injection 경계 부족 | Codex reasoner가 project root를 read-only로 볼 수 있음 |
| SSRF/대용량 download | 외부 discovery URL을 그대로 받는 API로 노출하면 위험 |
| 외부 데이터 전송 | query는 OpenRouter로, OCR image/request는 Codex backend로 전달됨 |
| 평문 job/mail 데이터 | sender/body/output이 data-kit과 여러 SQLite/JSON/MD에 존재 |
| 파일 권한 | 조사한 job DB와 email 산출물이 mode `0644` |
| secret 탐색 범위 | OpenRouter loader가 HOME 계열 `.env`까지 탐색 |
| subprocess argv 노출 | Gmail body가 command argument가 될 수 있음 |
| checksum/signature 부족 | 현재 hatch 전체의 cryptographic manifest 없음 |
| 감사·보존 정책 부재 | query log, email 본문, 생성 파일의 retention/redaction 정의 없음 |

신규 카드 정보 MCP에 email/general Hermes 자동화까지 자동으로 포함시키지 않는 편이 안전하다. 최소한 별도 privileged service와 별도 mutable volume이 필요하다.

## 16. MCP/Docker 관점의 현재 공백

| 기능 | 현재 상태 |
|---|---|
| MCP SDK dependency | 없음 |
| MCP server entrypoint | 없음 |
| stdio/Streamable HTTP transport | 없음 |
| tool/resource/prompt schema | 없음 |
| HTTP framework/ASGI server | 없음 |
| authentication/authorization | 없음 |
| rate limit/tenant isolation | 없음 |
| Dockerfile/Compose | 없음 |
| `.dockerignore` | 없음 |
| health/readiness/liveness | 없음 |
| graceful shutdown/cancellation | 없음 |
| structured logging/metrics/tracing | 없음 |
| DB generation/version API | 없음 |
| concurrent request control | 없음 |
| Docker integration/load/security test | 없음 |
| SQLite FTS5 runtime capability check | 없음 |

현재 코드를 “MCP 서버로 포장”하는 수준으로는 충분하지 않다. 검색 알고리즘, 데이터 generation, 설정, 보안 경계를 먼저 server-grade로 바꾸어야 한다.

## 17. 향후 신규 프로젝트에서의 재사용 판단표

### 17.1 비교적 그대로 가져갈 가치가 큰 요소

| 요소 | 이유 |
|---|---|
| issuer 포함 `doc_version_id` | 카드사 간 충돌 방지, provenance 기본축 |
| portable artifact path validator | manifest의 절대경로·URL·`..`를 막는 lexical guard; resolved containment/symlink 정책은 추가 필요 |
| master manifest v2 기본 필드 | 수집 결과와 artifact 연결에 유용 |
| OCR-only primary artifact 정책 | downstream source를 단순화 |
| SHA-256 text/PDF hash | 증분 처리·검증·generation 비교에 유용 |
| structured section/sentence schema | 카드 혜택/실적/유의사항 검색 메타데이터가 풍부 |
| OpenRouter retry와 vector validation | 429/5xx backoff, count/dim/finite 검증 |
| evidence bundle과 citation validation | grounded answer의 좋은 출발점 |
| requester-only mail 정책 | 별도 mail service를 유지할 경우 유용 |
| release bundle path/checksum primitive | 배포 artifact 검증 기반으로 재사용 가능 |

### 17.2 인터페이스는 참고하되 재구현이 필요한 요소

| 요소 | 필요한 변경 |
|---|---|
| `CardRAGService.search` | issuer/section을 검색 전 필터, stable citation, full evidence fetch, generation 포함 |
| vector store | exact BLOB scan 대신 ANN/vector extension/상주 index 검토 |
| hybrid fusion | 공통 document/evidence key로 fusion하고 실제 integration 평가 |
| FTS | issuer/as-of/filter, read-only connection, Kiwi/dictionary 사용 여부 정리 |
| queue | atomic claim, lease, retry, heartbeat, crash recovery, schema migration |
| daily monitor | library service화, generation build/verify/atomic publish |
| issuer adapter | `scripts.*` 의존 제거, timeout/rate limit/allowlist/streaming |
| OCR runner | 실제 model provenance, 제한 권한, isolated worker, durable job |
| config | cwd/HOME 암묵값 제거, typed settings와 secret injection |
| status | 분산 JSON 대신 issuer/run/generation 기반 단일 상태 모델 |

### 17.3 일반 MCP 요청에 직접 노출하지 말아야 할 요소

- `hatch_attach_data.py`의 삭제·교체 동작
- `run_5_sample_pipeline.py --clean`
- `danger-full-access` Codex OCR
- 임의 URL PDF download
- DB `drop/rebuild`
- Gmail 발송
- terminal ledger 직접 편집
- 임의 filesystem output path를 받는 exporter

## 18. 신규 MCP에서 예상되는 최소 도메인 표면

아래는 구현 완료 목록이 아니라 레거시 분석에서 도출한 후보 계약이다.

### 18.1 읽기 전용 tool 후보

- `search_evidence(query, issuer?, section_type?, item_type?, as_of?, limit?)`
- `get_evidence(stable_evidence_id)`
- `get_product(issuer, product_code, as_of?)`
- `list_product_versions(issuer, product_code)`
- `get_index_status()`
- `list_issuers()`

각 evidence에는 최소한 다음이 필요하다.

- stable issuer-scoped ID
- corpus/index generation
- product/document version/effective date
- full quote 또는 pagination 가능한 resource
- page/line/source chunk 범위
- source URL/path와 content hash
- retrieval method와 score

### 18.2 resource 후보

- `cardrag://catalog/issuers`
- `cardrag://catalog/index-status`
- `cardrag://products/{issuer}/{product_code}`
- `cardrag://documents/{doc_version_id}`
- `cardrag://evidence/{stable_id}`
- `cardrag://sources/{doc_version_id}/ocr`

### 18.3 별도 관리자/배치 영역 후보

- discovery 실행
- conveyor job 제출/조회
- terminal failure 재시도/해제
- index generation build/verify/publish

이 작업들은 장시간 외부 네트워크, OCR, 대용량 DB 변경을 수반하므로 동기식 MCP tool 호출 안에서 직접 수행하기보다 별도 durable job으로 처리해야 한다.

## 19. Docker 운영 시 데이터 경계

권장 경계는 다음과 같다.

```text
[offline/privileged ingestion worker]
  public card sites + Codex/OpenRouter
  writable raw/render/OCR/build workspace
                 │ 검증된 generation publish
                 ▼
[online/read-only MCP server]
  immutable evidence/structured/vector snapshot
  bounded query concurrency
  no Gmail, no PDF download, no Codex danger-full-access
```

스토리지도 최소 다음처럼 분리할 필요가 있다.

1. read-only 검색 snapshot volume
2. ingestion build workspace
3. queue/run state용 mutable volume
4. output/export용 제한된 mutable volume
5. 외부 secret injection

전체 9.51 GiB data-kit을 애플리케이션 image layer에 bake할 필요는 없다. 온라인 검색에 현재 직접 필요한 핵심 DB만 해도 약 3.2 GiB이고, raw PDF/PNG/legacy archive/email report는 별도 보존 목적이다. image와 corpus lifecycle을 분리해야 build/push/rollback이 가능하다.

## 20. 우선순위별 확인 사항

### P0 — 신규 서버 착수 전에 반드시 결정/해결

1. MCP transport와 인증 경계
2. read-only query 서버와 ingestion/OCR 분리
3. exact vector full scan 대체 또는 명시적 성능 한도
4. live DB drop/rebuild 제거와 atomic generation publish
5. issuer를 모든 ID/filter/dedupe/응답에 유지
6. wheel에서 누락되는 `scripts.*` 의존 제거
7. corpus snapshot/version을 응답에 노출
8. hybrid를 MVP에서 제외할지, 공통 evidence key로 바로잡아 제공할지 결정

### P1 — 첫 운영 배포 전 해결

1. hybrid를 제공한다면 fusion ID 공간·가중치를 수정하고 실제 integration 평가
2. stale embedding 정리 또는 current hash join
3. stable evidence resource와 full quote/page/line provenance
4. queue atomic claim/lease/crash recovery
5. OCR 실제 model provenance와 isolated permissions
6. URL allowlist, download size/redirect/PDF 검증
7. typed settings, secret 관리, read-only SQLite mode
8. health/readiness와 schema/model/dim/hash coverage 검사
9. 개인정보/메일 산출물 retention과 volume 분리

### P2 — 품질·유지보수 개선

1. 중복 정규화 모듈 통합
2. chunk/token upper bound
3. Kiwi/dictionary token의 실제 사용 여부 정리
4. taxonomy와 ranking boost 명칭 통일
5. latest version 자연 정렬
6. 실제 card-domain retrieval benchmark와 grounded-answer 평가
7. structured logging, metrics, trace, audit event
8. LICENSE/third-party source 이용 조건과 SBOM
9. embedding 증분 조회를 DB-side anti-join/cursor 방식으로 변경

## 21. 아직 결정되지 않은 사항

레거시만으로는 다음을 알 수 없다. 신규 프로젝트 요구사항 단계에서 명시적으로 결정해야 한다.

- MCP는 stdio 전용인지, 원격 Streamable HTTP인지
- 사용자는 단일 내부 시스템인지, 다중 사용자/tenant인지
- 인증 방식과 tool별 권한
- 검색 대상은 최신본만인지, 과거 버전/as-of 검색도 필요한지
- OpenRouter를 계속 사용할지, 로컬/다른 embedding으로 재생성할지
- raw PDF/OCR 원문을 MCP resource로 공개할 범위
- 기대 latency, QPS, 동시성, 가용성, 데이터 갱신 주기
- 카드사 추가 순서와 issuer plugin 계약
- 이메일 Agent와 exporter를 신규 MCP 범위에 포함할지
- 원문/PDF/메일 데이터의 보존·삭제·감사 정책
- 카드사 공개 자료의 수집·재배포·상업적 서비스 허용 범위

## 22. 주요 근거 파일 색인

| 주제 | 파일 |
|---|---|
| 릴리스 구조·기준치 | `H/HATCH_GUIDE.md`, `H/HATCH_PREFLIGHT_REPORT.md`, `H/RELEASE_MANIFEST.json` |
| package/entrypoint/dependency | `P/pyproject.toml`, `P/uv.lock` |
| 공통 문서 계약 | `P/src/cardrag_conveyor/contracts.py`, `paths.py` |
| issuer adapter | `P/src/cardrag_conveyor/issuers/*.py` |
| daily orchestration | `P/scripts/run_daily_monitor.py`, `run_5_sample_pipeline.py` |
| manifest diff | `P/src/wooricard_rag/daily_monitor.py` |
| retry/terminal | `P/src/cardrag_conveyor/retry_state.py` |
| inventory/evidence | `P/src/wooricard_rag/inventory.py`, `P/scripts/build_evidence_rag.py` |
| structured data | `P/src/wooricard_rag/structured_sections.py` |
| embedding/vector | `P/src/wooricard_rag/api_embeddings.py`, `api_vector_search.py` |
| FTS/hybrid/ranking | `P/src/wooricard_rag/chunk_index.py`, `hybrid_search.py`, `ranking.py` |
| Agent 검색 계약 | `P/src/cardrag_agent/service.py` |
| Agent reasoner/validation | `P/src/cardrag_agent/worker.py`, `validation.py` |
| queue/mail/export | `P/src/cardrag_agent/queue.py`, `mail.py`, `exporters.py` |
| 설치·data 검증 | `P/hatch/hatch_attach_data.py`, `hatch_verify_install.py` |
| 실제 corpus | `D/artifacts/manifests/`, `D/data/db/`, `D/reports/daily_runs/` |

## 23. 최종 판단

이 레거시는 카드 공시 수집과 OCR corpus 구축, 카드 혜택 중심 구조화에 상당한 자산을 갖고 있다. 특히 1,592개 문서와 현재 hash가 맞는 138,736개 structured embedding 단위, issuer-aware 문서 ID, evidence 검증 개념은 신규 프로젝트의 좋은 기반이다.

반면 현재 실행 구조는 단일 작업 디렉터리에서 CLI와 배치가 대용량 SQLite 파일을 직접 읽고 다시 만드는 형태다. MCP/Docker 운영에 필요한 장기 실행 서버, 전송 계층, 인증, immutable generation, 동시성, 관측성, 안정적인 vector index는 아직 없다. 따라서 신규 프로젝트는 레거시를 그대로 컨테이너화하기보다 도메인 계약과 corpus를 선별 재사용하고, online read path와 offline write path를 분리해 새로 구성하는 것이 적합하다.
