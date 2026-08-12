# 레거시 데이터 재사용 가이드

## 1. 문서 목적과 적용 범위

이 문서는 **cardrag-conveyor-hatch-20260712T091451_KST** 데이터 패키지에서 신규 CardRAG MCP 시스템으로 가져갈 자산과 가져가지 않을 자산을 구분하고, 원본을 훼손하지 않는 복사·변환·검증 방향을 정의한다.

- 기준 분석일: 2026-08-11
- 이 문서의 작성일: 2026-08-12
- 레거시 경로 별칭:
  - H: **cardrag-conveyor-hatch-20260712T091451_KST**
  - P: **H/process-kit/cardrag-conveyor**
  - D: **H/data-kit/cardrag-conveyor-data**
- 사실 기준: [레거시 프로젝트 분석 기록](../LEGACY_PROJECT_ANALYSIS.md)
- 현재 작업 범위: 분류와 이전 계획 문서화
- 현재 수행하지 않는 작업: 데이터 복사·이동·삭제, OCR·구조 분석·임베딩 실행, DB 변환, 신규 generation 게시

이 문서에서 “재사용”은 레거시 디렉터리를 신규 서비스에 그대로 마운트한다는 뜻이 아니다. 검증된 내용을 신규 데이터 계약으로 받아들이는 것을 뜻한다.

## 2. 확인된 레거시 기준선

### 2.1 패키지와 corpus 규모

| 항목 | 확인값 | 재사용 판단에 미치는 영향 |
|---|---:|---|
| data-kit | 10,213,000,789 bytes, 약 9.51 GiB | 전체를 Git·Docker image에 포함하지 않음 |
| data-kit 파일 | 9,551개 | 파일별 inventory와 checksum manifest가 필요 |
| 성공 문서 버전 | 1,592건 | 이전 입력의 기준 집합 |
| 최신 문서/상품 | 1,567건 | 초기 서비스 색인 우선 대상 후보 |
| 발급사별 문서 | 우리카드 915, KB국민카드 677 | 모든 키에 issuer 유지 |
| 발급사별 최신 문서 | 우리카드 890, KB국민카드 677 | 최신본과 과거본을 구분 |
| effective date 범위 | 2018-05-08~2026-07-03 | 실시간 corpus가 아닌 2026-07 snapshot |
| raw PDF | 1,634개, 약 1.66 GB, 내용 hash unique 1,405개 | hash 기반 중복 제거 가능 |
| OCR Markdown | manifest 기준 1,592건 모두 존재 | 예외 검증 후 원문 자산으로 활용 가능 |
| 렌더 PNG | 2,943개, 약 2.95 GB | 온라인 runtime에는 불필요 |

레거시 릴리스 생성일은 2026-07-12이다. 따라서 2026-08-12 기준 최신 공시를 보장하지 않는다. 신규 서비스에 게시하기 전 카드사별 증분 discovery가 별도로 필요하다.

### 2.2 검색 데이터 기준선

| 계층 | 현행 row 수 | 해석 |
|---|---:|---|
| evidence chunks | 18,669 | 최신 성공 문서만 반영한 FTS 원천 |
| structured sections | 20,318 | 규칙 기반 section 분류 결과 |
| sentence units | 118,418 | 규칙 기반 문장 단위 결과 |
| 현재 section embedding | 20,318 | 현재 text hash와 4,096차원 일치 |
| 현재 sentence embedding | 118,418 | 현재 text hash와 4,096차원 일치 |
| embedding DB 전체 | 149,703 | 아래 과거 row를 포함하므로 현재 corpus 수가 아님 |
| 과거 section embedding | 1,196 | current index에서 제외해야 함 |
| 과거 sentence embedding | 9,771 | current index에서 제외해야 함 |

현재 embedding provider/model 기록은 **openrouter**, **qwen/qwen3-embedding-8b**, dimension 4,096이다. 이 값은 레거시 snapshot의 provenance이지 신규 시스템의 모델 확정값이 아니다.

현행 inventory·evidence·structured·embedding DB 네 개의 합은 3,432,402,944 bytes, 약 3.20 GiB다. 이 수치만 보아도 검색 데이터는 image layer가 아니라 외부 generation volume으로 관리해야 한다. 별도로 legacy archive가 약 1.97 GiB 있으나 신규 runtime 대상은 아니다.

### 2.3 반드시 보존할 예외 사실

1. master manifest 1,592건의 PDF hash는 모두 로컬 raw PDF 중 하나와 일치한다.
2. manifest가 직접 가리키는 **raw_pdf_rel_path**는 861건이며 모두 hash가 일치한다.
3. 나머지 731건은 **raw_pdf_rel_path**가 비어 있다. 구성은 우리카드 54건과 KB국민카드 677건이다.
4. metadata의 **ocr_md_chars**는 1,592건 모두 실제 OCR 문자 수와 일치한다.
5. master manifest의 **ocr_chars**는 1,537건만 일치하며 KB 55건은 최대 147자 차이가 난다. 이는 OCR 본문 손상으로 단정할 근거가 아니라 manifest counter drift다.
6. metadata의 **ocr_md_sha256**은 1,591건이 실제 파일과 일치한다. imported 우리카드 문서 1건은 불일치하므로 별도 검토 대상이다.
7. raw PDF 외 PNG·OCR·DB·보고서를 포괄하는 릴리스 전체 checksum 목록은 없다.

이 예외는 이전 과정에서 자동 보정해 사라지게 해서는 안 된다. 원래 선언값, 재계산값, 판정과 처리자를 함께 기록한다.

## 3. 재사용 분류 기준

| 등급 | 의미 | 허용되는 처리 |
|---|---|---|
| 그대로 재사용 가능 | 의미·식별 규칙을 바꾸지 않고 신규 계약에 보존할 수 있음 | 검증 후 필드 또는 byte content 보존 |
| 파일명 또는 디렉터리 구조 변경 후 재사용 가능 | 내용은 유지하되 신규 storage 경계와 portable path로 재배치해야 함 | 원본에서 copy, hash 검증, 신규 catalog 연결 |
| 변환 또는 재색인이 필요 | schema·provenance·검색 방식이 신규 운영 조건을 충족하지 않음 | staging에서 변환하고 새 generation 구축 |
| 신규 시스템에서 미사용 | 민감정보, 과거 운영 부산물, 중복·위험한 실행물 또는 runtime 불필요 자산 | 원본 보존만 하고 신규 runtime으로 이전하지 않음 |

어느 등급도 레거시 파일의 제자리 수정이나 이동을 허용하지 않는다.

## 4. 자산별 분류

### 4.1 그대로 재사용 가능한 의미·식별 필드

| 자산 | 판단 | 조건과 근거 |
|---|---|---|
| issuer를 포함한 doc_version_id 개념 | 그대로 재사용 | 카드사 간 product code 충돌 방지의 기본축 |
| card_company, product_code, product_name | 그대로 재사용 | catalog·검색·citation 전 구간에 유지 |
| document_type, effective_date, version | 그대로 재사용 | 버전·as-of 조회에 필요. version 문자열 정렬 로직은 재구현 |
| source_url, source_post_id, 원래 파일명·크기 | 그대로 재사용 | provenance 필드로 유지하되 URL은 실행 입력으로 신뢰하지 않음 |
| 검증된 PDF·OCR SHA-256 | 그대로 재사용 | 증분·중복·무결성 식별자로 유지 |
| OCR 원문 우선 정책 | 그대로 재사용 | downstream 변환의 canonical text 기준 |
| structured section의 도메인 분류 필드 | 그대로 재사용할 계약 후보 | issuer, section type, benefit kind, line 범위 등의 의미는 유용하나 기존 row는 검증·변환 대상 |

portable path의 절대경로·URL·상위 경로 차단 개념도 유지한다. 다만 레거시 validator는 symlink 탈출까지 보장하지 않으므로 신규 구현에서는 실제 resolve 후 data root containment를 추가 검증한다.

### 4.2 경로 변경 후 재사용 가능한 파일

| 자산 | 범위 | 처리 방향 |
|---|---:|---|
| raw PDF byte content | manifest 1,592건과 연결되는 파일 | 원본 byte를 바꾸지 않고 content-addressed object store로 copy |
| OCR 문서 | 1,592건 | 검증된 파일은 byte 보존 copy 후 canonical catalog에 연결 |
| OCR metadata의 원천 provenance | 문서별 metadata | 원본 JSON을 audit copy로 보존하고 canonical metadata는 별도 생성 |
| 과거 문서 버전 | 전체 1,592건 중 최신 외 25건 | 초기 온라인 색인은 최신 1,567건을 우선하되 원본 버전은 보존 |

raw PDF 1,634개를 모두 무차별 복사하지 않는다. manifest 성공 문서와 hash로 연결된 집합을 우선 이전하고, 성공 문서와 연결되지 않은 파일은 provenance 확인 전 quarantine 또는 cold archive 후보로 둔다.

우리카드와 KB국민카드에는 위 레거시 기준선이 있지만 신한카드에는 재사용할 adapter·corpus가 없다. 신한카드 개인 신용·체크카드 상품안내장의 현재본과 과거 이력은 신규 수집 경로로 만들고 레거시 migration 성공 건수에 포함하지 않는다. 신한 법인·선불카드는 1차 BULK 범위에서 제외한다.

### 4.3 변환 또는 재색인이 필요한 자산

| 자산 | 필요한 작업 | 그대로 운영할 수 없는 이유 |
|---|---|---|
| cardrag_master_manifest.v2 | canonical document catalog로 변환 | 731건의 raw 경로 공백, OCR 문자수 55건 drift, atomic write 부재 |
| 혼합 OCR metadata | 공통 schema와 provenance로 변환 | 우리카드 imported 861건과 현행 v2 731건의 metadata 계약이 다름 |
| inventory.sqlite3 | catalog로 변환 | index_state의 needs_reindex가 실제 완료 상태를 의미하지 않음 |
| ocr_inventory.sqlite3 | 별도 source of truth로 사용하지 않음 | inventory와 현재 SHA-256까지 같은 중복 복제본 |
| evidence_inventory.sqlite3 | canonical evidence key로 재구축 | 최신본만 포함하며 기존 chunk ID가 structured ID와 결합되지 않음 |
| structured_sections.sqlite3 | 새 schema로 변환·품질 표본 검증 | 규칙 기반 분류이며 page/source hash/generation 정보가 부족 |
| api_embeddings_structured.sqlite3 | 신규 index로 재임베딩 | exact BLOB scan, 과거 row 10,967건, 모델 교체·ANN 구조 필요 |
| FTS/hybrid index | 공통 evidence key로 재색인 | FTS chunk ID와 vector item ID가 달라 실제 fusion이 성립하지 않음 |
| retry ledger·run report | 필요 필드만 audit history로 변환 | 현재 상태와 누적 이력이 섞이고 issuer/run ID가 불완전 |

기존 embedding의 현재 hash coverage가 완전하더라도 production index를 그대로 승격하지 않는다. 기존 vector는 검색 benchmark나 변환 대조 자료로만 사용할 수 있으며, 신규 canonical text hash·모델·dimension·index engine을 기준으로 재색인한다. **api_chunk_embeddings**는 0건이므로 이전 대상이 아니다.

### 4.4 신규 runtime에서 사용하지 않을 자산

| 자산 | 미사용 범위 | 이유 |
|---|---|---|
| rendered PNG | 온라인 MCP와 기본 generation | 약 2.95 GB이며 신규 runtime으로 이관하지 않음. 페이지 PNG는 원본 PDF에서 요청 시 생성해 7일 cache |
| legacy-archive DB | 전부 | 약 1.97 GiB의 과거 중복 세대이며 경로 혼동 위험 |
| Agent email jobs·mailbox·email outputs | 전부 | sender/body/output 평문과 업무 정보가 포함될 수 있음 |
| Gmail·Hermes 자동화 | 신규 카드 조회 MCP | 카드 정보 검색의 권한·데이터 경계와 무관 |
| stale status JSON | 현재 상태 판정 | 2026-07-08 running과 후속 완료 report가 충돌 |
| demo/report DB와 임시 OCR chunk | production catalog | 성공 기준은 master manifest이며 잔존 파일 존재가 완료를 뜻하지 않음 |
| hatch_attach_data.py | 신규 운영 경로 | 기존 대상을 재귀 삭제·교체할 수 있음 |
| run_5_sample_pipeline.py의 clean 동작 | 신규 운영 경로 | 삭제 동작과 privileged OCR 실행을 일반 서비스에 노출하면 안 됨 |
| 기존 danger-full-access OCR 설정 | 온라인 MCP | 장기 실행 읽기 서비스의 권한 경계와 충돌 |

rendered PNG와 보고서를 레거시 source에서 삭제하라는 뜻은 아니다. 레거시 원본 안에는 읽기 전용으로 유지하되 신규 runtime이나 generation으로 복사하지 않는다. 온라인 페이지 PNG는 원본 PDF exact version에서 요청 시 생성하고 7일 뒤 cache에서 제거한다.

## 5. 원본 보존과 접근 원칙

1. H 전체는 이전 작업 동안 read-only source로 취급한다.
2. 레거시에 대한 write 권한이 있는 bind mount나 sync-back 경로를 만들지 않는다.
3. 신규 파일은 항상 별도 staging과 data volume에 생성한다.
4. copy 성공을 파일 존재나 크기만으로 판정하지 않고 SHA-256 재계산으로 확인한다.
5. DB를 읽을 때도 read-only URI 또는 source snapshot 복사본을 사용한다.
6. 예외를 발견해도 레거시 manifest·metadata를 수정하지 않는다.
7. source snapshot ID, 원래 상대경로, 원래 선언 hash, 재계산 hash, 검증시각을 신규 migration ledger에 기록한다.
8. 카드사 자료의 수집·재배포·서비스 이용 조건이 확인되기 전 외부 공개 범위를 확정하지 않는다.
9. PDF·OCR·DB·OAuth token·API key는 Git과 Docker image layer에 넣지 않는다.

## 6. 목표 데이터 디렉터리

아래는 외부 volume의 논리 구조다. 실제 host 경로와 저장 기술은 구현 단계에서 결정한다. **CARDRAG_DATA_ROOT**는 source checkout과 Docker image 밖에 둔다.

~~~text
CARDRAG_DATA_ROOT/
├── objects/
│   ├── pdf/sha256/<prefix>/<pdf_sha256>.pdf
│   └── ocr/sha256/<prefix>/<ocr_sha256>.md
├── catalog/
│   ├── source-snapshots/<snapshot_id>/manifest.json
│   └── documents/<issuer>/<product_code>/<effective_date>/v<version>/record.json
├── build/
│   └── <run_id>/                       # 게시 전 작업공간
├── generations/
│   └── <generation_id>/
│       ├── generation-manifest.json
│       ├── checksums.sha256
│       ├── inventory/
│       ├── lexical-index/
│       ├── structured/
│       ├── vector-index/
│       └── READY
├── state/
│   ├── jobs/
│   └── migration/
├── quarantine/
│   ├── unresolved-pdf/
│   ├── ocr-mismatch/
│   └── metadata-invalid/
└── current.json                        # 게시된 generation 포인터
~~~

설계 원칙은 다음과 같다.

- PDF와 OCR은 hash 기반 불변 object로 저장하여 같은 내용의 중복을 공유한다.
- 사람이 읽기 쉬운 문서 경로의 record가 issuer·상품·버전과 object hash를 연결한다.
- generation은 inventory, lexical, structured, vector가 같은 입력 snapshot을 가리키는 배포 단위다.
- build는 writable, 게시된 generations는 immutable, 온라인 MCP mount는 read-only다.
- current pointer는 완전히 검증된 generation만 가리키며 임시 파일 작성 후 atomic replace한다.
- quarantine은 오류를 숨기지 않고 정식 generation에서 제외한 채 조사할 수 있게 한다.
- backup·restore 경로는 v1 구현에 포함하지 않고 추후 개선 과제에서 별도 failure domain과 함께 설계한다.

## 7. PDF 연결과 중복 처리 규칙

### 7.1 직접 경로가 있는 861건

1. **raw_pdf_rel_path**를 portable path 규칙으로 검증한다.
2. 실제 resolve 결과가 레거시 data root 안에 있는지 확인한다.
3. 파일 SHA-256이 manifest의 **pdf_sha256**과 일치하는지 재검증한다.
4. content-addressed 목표 경로로 copy한 뒤 목표 파일 hash를 다시 계산한다.
5. catalog record에 **mapping_method=direct_path_and_hash**와 원래 상대경로를 남긴다.

### 7.2 직접 경로가 비어 있는 731건

경로를 filename이나 product name으로 추측하지 않는다.

1. manifest의 **pdf_sha256**을 raw PDF 전체의 사전 계산 hash inventory에서 찾는다.
2. 같은 hash의 파일이 여러 개면 하나의 content object로 수렴시키되 모든 legacy 상대경로를 migration ledger에 남긴다.
3. 문서 record에는 **mapping_method=hash_lookup**, **legacy_raw_pdf_rel_path=null**을 명시한다.
4. hash에 맞는 파일이 없으면 **unresolved-pdf**로 보내고 generation 입력에서 제외한다.
5. hash가 같아 byte content는 결정되지만 문서 provenance가 충돌하면 자동 승격하지 않고 수동 검토한다.

현재 읽기 전용 조사에서는 1,592건의 manifest PDF hash가 모두 로컬 raw PDF와 일치했다. 이는 migration 구현 후 동일 결과를 재검증해야 한다는 기준선이지, 복사 작업이 이미 끝났다는 뜻이 아니다.

### 7.3 중복 PDF

1,634개 PDF 중 content hash unique는 1,405개다. 229개 중복 인스턴스는 삭제 대상으로 간주하지 않고, 신규 저장소에서 동일 hash object를 참조하도록 한다. 문서 버전 record는 각각 보존해야 하므로 object deduplication과 catalog deduplication을 혼동하지 않는다.

## 8. OCR과 metadata 변환 규칙

1. 실제 OCR Markdown byte에서 SHA-256과 문자 수를 다시 계산한다.
2. metadata의 **ocr_md_sha256**, **ocr_md_chars**, master manifest의 **ocr_chars**를 각각 원래 값으로 보존한다.
3. 재계산값과 일치하는 OCR은 content-addressed object로 copy한다.
4. master manifest 문자 수만 다른 KB 55건은 **counter_drift** 경고를 기록하되 metadata hash와 실제 내용이 맞으면 별도 품질 gate를 거쳐 사용할 수 있다.
5. metadata hash가 다른 imported 우리카드 1건은 **ocr-mismatch**에 격리한다. 실제 hash로 선언값을 덮어 쓰지 않고, PDF 대조 또는 승인 후에만 canonical OCR로 채택한다.
6. imported metadata와 **ocr_result_manifest.v2**는 공통 schema로 정규화하되 원래 schema와 payload 위치를 provenance로 남긴다.
7. 실제 OCR model이 확인되지 않은 imported 문서는 **model_id=unknown**으로 기록한다. 추정값을 채우지 않는다.
8. **CARDRAG_OCR_MODEL** label만 있고 실제 Codex 명령에 model option이 없었던 문서는 “실제 모델 검증 안 됨”으로 표시한다.

canonical OCR record에는 최소 다음이 필요하다.

- issuer, product code/name, document/version/effective date
- source PDF hash와 OCR text hash
- source snapshot ID와 원래 metadata schema
- page count, 문자 수, 언어·완전성 품질 결과
- OCR provider/model/version 또는 unknown
- prompt/version, 렌더 설정과 처리시각(확인 가능한 경우)
- 검증 상태, 예외 코드, 승인자와 승인시각

## 9. 구조화와 색인 재생성 원칙

기존 structured row는 도메인 taxonomy와 평가 표본으로 활용할 수 있지만 신규 generation의 정답 데이터로 자동 승격하지 않는다.

1. canonical OCR hash를 입력 키로 section/sentence를 다시 생성한다.
2. stable evidence ID는 issuer, document version, page/line 또는 source span과 연결한다.
3. section·sentence·FTS·vector가 같은 evidence key를 사용하도록 한다.
4. page/source URL/text hash/generation을 citation에 포함한다.
5. 모델이나 규칙, chunk 정책이 바뀌면 새 processing version으로 전체 또는 영향 범위를 재생성한다.
6. 신규 embedding은 현재 canonical text hash와 선택한 model/dimension/index version을 키로 생성한다.
7. 레거시의 historical embedding 10,967건은 신규 current index로 복사하지 않는다.
8. 기존 embedding DB 전체 row 수를 coverage로 사용하지 않는다. current text hash+model+dimension 일치율을 사용한다.
9. publish 전 검색 품질 평가와 원문 인용 대조를 수행한다.

레거시 DB의 **PRAGMA integrity_check=ok**는 파일 구조가 읽힌다는 뜻이지 신규 검색 계약과 품질이 적합하다는 뜻은 아니다.

## 10. 계획된 이전 절차

아래 절차는 향후 migration 구현 순서다. 현재 실행된 절차가 아니다.

### 단계 A — 이전 기준 고정

- 레거시 source를 read-only로 mount하고 source snapshot ID를 발급한다.
- source 전체 file inventory와 가능한 SHA-256을 별도 ledger에 기록한다.
- 신규 canonical schema와 라이선스·보존정책을 확정한다. 기본 조회는 latest, 과거본은 명시적 version/as-of 조회라는 범위 정책을 적용한다.
- 대상 volume의 여유 공간을 확인한다.

완료조건: source snapshot과 대상 schema가 승인되고 레거시에 write가 발생하지 않았다는 증거가 있다.

### 단계 B — 불변 원본 copy

- PDF를 직접 경로 또는 hash lookup으로 staging object store에 copy한다.
- OCR을 재계산 hash 기준으로 staging에 copy한다.
- 중복 object는 hash로 합치고 document catalog record는 별도로 유지한다.
- 불일치·미해결 항목은 quarantine ledger에 남긴다.

완료조건: copy 대상마다 source/target hash가 같고 1,592개 문서의 mapping 결과가 성공·격리·제외 중 하나로 빠짐없이 집계된다.

### 단계 C — catalog 변환

- manifest와 metadata를 canonical record로 변환한다.
- 731건의 빈 raw path, KB 55건 counter drift, OCR hash 불일치 1건을 명시적 exception으로 유지한다.
- latest를 날짜와 숫자 version 규칙으로 다시 계산하고 레거시 결과와 차이를 검토한다.

완료조건: issuer-scoped ID가 유일하고 모든 record가 PDF/OCR object 또는 명시적 exception을 가리킨다.

### 단계 D — 구조화·재색인

- 승인된 OCR만 사용해 structured/evidence 데이터를 새로 만든다.
- lexical/vector index를 별도 build 경로에 생성한다.
- current text hash와 embedding coverage를 검증한다.

완료조건: 같은 generation 안의 catalog·structured·lexical·vector가 동일 입력 hash 집합을 가리키고 품질 gate를 통과한다.

### 단계 E — 검증과 게시

- generation manifest와 전체 checksum을 만들고 DB/index 무결성을 확인한다.
- 표본 문서에 대해 PDF→OCR→section→검색 근거를 역추적한다.
- 온라인 smoke test는 read-only candidate mount로 수행한다.
- 모든 gate 통과 후에만 READY marker와 current pointer를 게시한다.

완료조건: 게시 승인 기록, generation ID, checksum, 검증 결과와 rollback 대상 generation이 남는다.

## 11. 검증 gate

| gate | 검증 항목 | 실패 시 처리 |
|---|---|---|
| 원본 inventory | 파일 수·크기·hash·legacy 상대경로 | staging 중단, source 재확인 |
| PDF mapping | manifest 1,592건 각각의 hash 연결 | unresolved 또는 provenance conflict 격리 |
| OCR mapping | 실제 hash·문자 수·metadata 비교 | mismatch 격리, 무단 보정 금지 |
| catalog | ID uniqueness, issuer 유지, 필수 필드, portable path | record 제외 후 변환 오류 수정 |
| latest 계산 | 문서별 version 정렬과 1,567건 기준선 비교 | 차이 목록 수동 검토 |
| structured | source span·text hash·taxonomy·표본 정확성 | 해당 processing version 재생성 |
| embedding | current hash+model+dimension coverage | generation 게시 금지 |
| index | FTS/vector count, 공통 evidence ID, 검색 평가 | generation 게시 금지 |
| generation | checksum, schema version, DB integrity, READY | pointer 변경 금지 |
| 보안 | secret·mail/job 데이터·절대경로 미포함 | 산출물 삭제 후 재생성 |

기준선과 수치가 다르면 차이를 자동으로 “정상화”하지 않는다. 신규 discovery로 인한 합리적 증가인지, 변환 누락인지, 중복 제거 결과인지 설명 가능한 reconciliation report가 있어야 한다.

## 12. Rollback과 원상복구 방향

레거시 원본을 수정하지 않으므로 이전 작업 자체의 rollback은 신규 staging을 폐기하는 방식이다.

- 게시 전 실패: 실패한 build run만 격리하고 이전 current generation은 유지한다.
- 게시 직후 실패: current pointer를 검증된 이전 generation으로 atomic하게 되돌린다.
- object copy 오류: 잘못된 목표 object만 quarantine하고 source에서 hash 검증 후 다시 copy한다.
- catalog 변환 오류: 새 catalog/generation을 만들며 이미 게시된 generation을 in-place 수정하지 않는다.
- 모델·색인 회귀: 이전 image digest와 이전 data generation을 독립적으로 선택할 수 있게 한다.
- 레거시 source: 최종 인수와 보존기간 승인 전까지 삭제·정리하지 않는다.

rollback 후에는 실패 generation ID, 원인, 영향 문서, pointer 변경시각과 실행자를 감사 로그에 남긴다. 이전 generation을 재게시할 수 있다는 이유로 mutable job state까지 되돌리지는 않는다.

## 13. 결정 필요 항목

- 기본 검색은 최신 1,567건을 대상으로 하고 과거 25개 버전은 보존한다. 명시적 version/as-of 조회를 단일 filter 또는 별도 이력 색인 중 어떻게 구현할지는 검색 설계에서 정한다.
- rendered PNG는 신규 runtime으로 이관하지 않고 페이지 요청 시 생성해 7일 cache한다.
- OCR hash 불일치 1건의 canonical 채택 여부
- 신규 구조 분석 방식과 taxonomy version
- OpenRouter embedding model과 index engine
- 기술적으로는 승인된 `source_pdf` scope 사용자의 명시적 요청에 exact version·hash의 보존 원본 PDF 전체를 streaming file로 제공한다. 100 MB 상한과 HTTP Range를 적용하고 다운로드 감사 metadata를 90일 보존한다. 페이지 OCR text는 `search`, 요청 시 생성해 7일 cache하는 PNG는 `source_pdf` scope를 사용하고 분할 PDF는 생성하지 않는다.
- 카드사 공시 PDF의 저장·재배포·서비스 이용 조건과 허용 사용자 범위
- 검색 generation은 최소 3개 보존한다. 이를 초과한 보존 기간은 결정 필요이며 backup은 v1 후속 개선 과제다.

## 14. 이 문서 작성 시점의 상태

| 항목 | 상태 |
|---|---|
| 레거시 자산 읽기 전용 조사 | 검증 완료 |
| 재사용 분류와 목표 구조 문서화 | 문서화 완료 |
| source snapshot/checksum ledger 생성 | 미착수 |
| PDF/OCR copy | 미수행 |
| manifest/metadata 변환 | 미수행 |
| structure·embedding·index 재생성 | 미수행 |
| exception 수동 검토 | 미착수 |
| generation 검증·게시 | 미수행 |

이 문서만으로 기존 corpus가 신규 시스템에 이전되었거나 사용할 준비가 끝났다고 판단해서는 안 된다.
