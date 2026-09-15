# 데이터와 검색 계약

CardRAG는 원본 PDF, 검증된 OCR, 구조화된 문장과 검색 벡터를 서로 다른 산출물로 보존합니다.
이 문서는 저장 형식과 호환성 경계를 설명합니다. 도구의 요청·응답은 [MCP API](MCP_API.md),
운영 설정은 [운영 안내](OPERATIONS.md), 평가 산출물은 [평가 안내](EVALUATION.md)를 참고하세요.
애플리케이션 버전과 아래 schema·policy 버전은 독립적입니다.

## 상품, 개정과 날짜

지원 카드사 코드는 `bc`, `hana`, `hyundai`, `kb`, `lotte`, `samsung`, `shinhan`, `woori`입니다.
코드 지원 여부, 이번 generation에 적재된 카드사, 실제 검색 가능한 상품 수는 구분합니다.
적재되지 않은 카드사의 결과가 없다는 사실만으로 공식 상품이 없다고 판단하지 않습니다.

`product_lineage_id`는 카드사, 안정적인 상품 코드와 문서 종류에 결속됩니다.
`contract_revision_id`는 lineage, source identity와 PDF SHA-256에 결속됩니다.
lineage마다 `current`는 최대 하나이며, 최신 순서를 입증할 수 없으면 `ambiguous`로 보존합니다.
상품명은 고유 키가 아니므로 같은 이름의 여러 상품을 임의로 합치지 않습니다.

| 값 | 의미와 처리 |
|---|---|
| `launch_date` | 공식 자료에서 확인한 상품 출시일. 최근 출시 조회의 기준입니다. |
| `effective_date` | 수집 source가 제공한 문서 날짜. source의 `date_basis`를 함께 해석해야 합니다. |
| 출시일 `confirmed` | 유효한 출시일 근거가 하나로 일치합니다. |
| 출시일 `missing` / `invalid` / `conflicting` | 근거 없음 / 잘못된 날짜 / 서로 다른 날짜. 추정하지 않고 `[확인 필요]`로 표시합니다. |

출시일을 문서 개정일로 대체하지 않습니다. BC 수집기의 `effective_date`는
`date_basis=product_launch`로 표시되므로 문서 개정일이라고 설명해서도 안 됩니다.
공식 미래 출시일은 유효한 날짜일 수 있지만 현재까지의 최근 출시 목록에는 포함하지 않습니다.
최근 기간은 서울의 현재 달력 날짜로 해석합니다. 날짜 미확인 수는 현재 revision 기준이며,
그 수를 최근 출시 상품 수에 더하지 않습니다.

카드사별 수집 목록의 범위도 source provenance입니다. 현대·하나·롯데의 공식 목록은
개인·법인을 포함할 수 있고, BC는 개인 상품안내장 열을 사용합니다. 전체 결과를 모두 개인카드로
일괄 설명하지 않습니다. 현대의 날짜 sentinel `99999999`와 빈 첨부는 경고와 함께 건너뛰며,
잘못된 자료나 서로 충돌하는 최신 중복 자료를 임의로 최신 문서로 선택하지 않습니다.

DRM 등으로 처리할 수 없는 문서는 다운로드한 PDF의 source identity, SHA-256와 크기가
일치할 때만 해당 disposition으로 기록합니다. `unsupported_products`, `ocr_failed_products`
등의 수집 상태를 정상적으로 검색 가능한 문서와 구분하며, 실패를 조용히 상품 0건으로 바꾸지 않습니다.

## 원본과 구조

현재 serving schema는 `cardrag.serving-db.v5`입니다. v2/v3/v4 호환 reader도 유지하지만
v5 전용 구조·coverage를 지원한다고 표시하지 않습니다. v3는 bounded `unsupported_drm`
disposition을, v4는 검증된 PDF와 실패 상태만 있고 OCR page/evidence는 없는 OCR 실패 상품을
표현할 수 있습니다. 구 schema의 데이터가 없다는 사실과 기능 미지원은 구분합니다.

```text
product_lineage
└─ contract_revision
   └─ ROOT
      └─ MAJOR_SECTION (BENEFIT | NOTICE | MIXED | UNKNOWN)
         └─ ITEM
            ├─ PARAGRAPH | LIST_ITEM | FOOTNOTE
            ├─ TABLE ─ TABLE_ROW
            └─ BOILERPLATE | UNCLASSIFIED
```

canonical leaf span은 revision, 1부터 시작하는 page, 반개구간 문자 offset
`[source_start, source_end)`와 원문 SHA-256을 가집니다. 비공백 OCR 문자는 canonical span에
정확히 한 번 포함되어야 합니다. 모든 parent와 link는 같은 revision 안에서만 허용합니다.
파싱되지 않은 원문은 `UNCLASSIFIED`로 보존하고 표의 row는 원래 header 문맥을 유지합니다.
인용은 `document_pages.text[source_start:source_end]`와 정확히 일치해야 합니다.

OCR은 canonical page marker, 페이지 수, page hash와 processor/reuse identity로 검증합니다.
전사할 문자가 없는 그림·로고 페이지는 정해진 sparse control prefix만 남을 수 있습니다.
원문·페이지·credential 검사 없이 기존 OCR이나 원격 캐시를 재사용하지 않습니다.

문서별 parser 또는 derived view 생성이 실패하면 `cardrag.structure-unclassified-fallback.v1`은
중립 `ITEM` 아래에서 완전한 source-line 경계의 `UNCLASSIFIED` leaf로 원문을 보존합니다.
source coverage 100%, page/range/SHA와 revision 경계는 유지하며 fallback 목록의 count와
artifact SHA를 SQLite metadata와 seal metrics에 결속합니다.
한 줄 자체가 모델 한도를 넘어 fallback view도 만들 수 없으면
`cardrag.structure-failed-ledger.v1`에 PDF/OCR/source-pages SHA를 기록합니다.
다른 문서의 checkpoint는 계속 만들지만 최종 게시는 중단합니다. 성공한 generation에는
`structure_failed_document_count=0`과 canonical empty-ledger SHA가 양쪽에 있어야 합니다.

주요 봉인 테이블은 `issuers`, `product_lineages`, `contract_revisions`, `document_pages`,
`structure_nodes`, `node_spans`, `node_links`, `revision_coverage`, `embedding_profiles`,
`embedding_views`, `embedding_views_fts`, `metadata`입니다. schema와 논리 해시 계산에 포함되는
disposition 테이블도 함께 검증해야 하며, 임의 열 추가·삭제 후 과거 seal을 재사용할 수 없습니다.

## 임베딩과 벡터

기본 임베딩 profile은 다음 값 전체에 결속됩니다.

| 항목 | 계약 |
|---|---|
| Provider / model | OpenRouter / `qwen/qwen3-embedding-8b` |
| Route | `deepinfra` 또는 `nebius` 중 고정한 하나; provider fallback 금지 |
| Vector | 4,096차원, little-endian float32, row-major, L2 정규화 |
| Document / query policy | `cardrag.structure-views.v1` / `cardrag.qwen3-query.v1` |
| 입력 제한 | 정확한 token 수 검사, 초과 입력의 자동 truncation 금지 |

질의에는 아래 instruction을 사용하고 문서에는 instruction을 붙이지 않습니다.

```text
Instruct: Given a Korean financial product disclosure search query, retrieve relevant passages from Korean credit card product disclosure sections and sentence units that answer the query
Query:<한국어 질의>
```

route나 maximum token limit 변경은 profile ID와 cache identity 변경입니다. 차원이 다른
legacy 벡터를 새 profile로 재해석할 수 없습니다. 요청 model과 응답 model은 대소문자 차이만
허용하고, 응답 provider와 4,096개의 유한한 vector 값을 다시 검사합니다.
endpoint의 `max_prompt_tokens`가 null이면 양의 정수 `context_length`를 한도로 사용합니다.

view는 `TITLE`, `RAW_ITEM`, `CONTEXTUAL_ITEM`, `DETAIL`, `MAJOR_SECTION`, `CONTRACT`입니다.
`display_text`는 원문 span에서만 만들며, metadata breadcrumb를 포함한 `embedding_input`을
사용자에게 원문 인용으로 제시하지 않습니다. `cardrag.structure.v2`는 발견한 상품명과
source version을 임의로 정규화하거나 대체하지 않습니다. 두 값은 trim된 비어 있지 않은
문자열이며 control character가 없어야 합니다. 날짜는 `YYYY-MM-DD` 또는 JSON null입니다.

`cardrag.contextual-item-context.v1`의 label 순서는 `issuer`, `product_name`, `product_code`,
`source_version`, `effective_date`, `contract_revision_id`, `major_class`, 반복 가능한 `heading`입니다.
metadata만 바뀌어도 해당 structure checkpoint와 corpus identity가 달라지지만 인용 원문은 바뀌지 않습니다.

`vectors.f32`는 SQLite 밖에 저장하며 `embedding_views.row_index`의 연속 행 번호와 결속됩니다.
manifest와 READY는 파일 hash·크기, row 수, 차원, dtype과 정규화 계약을 검증합니다.
vector 파일을 같은 경로에서 바꾸거나 잘라서는 안 됩니다. 실행 중인 mmap reader에는
새 generation을 검증한 뒤 handle을 교체합니다.

v5 sidecar 한도와 메모리 상주 한도는 다릅니다. `CARDRAG_MCP_MAX_VECTOR_SIDECAR_BYTES`는
검증한 mmap 파일 크기를, `CARDRAG_MCP_MAX_RESIDENT_VECTOR_BYTES`는 active/candidate/pinned
handle의 heap 기반 구형 행렬과 norm 배열을 제한합니다. mmap 주소 범위 전체를 즉시 상주한
RAM으로 계산하지 않습니다. 구형 inline 행렬 한도 `CARDRAG_MCP_MAX_VECTOR_BYTES`는
resident 한도의 호환 fallback이므로 삭제하거나 같은 의미로 합치지 않습니다.

## 검색과 게시

주 검색은 상품·시간 필터를 적용한 뒤 대상 활성 embedding row 전체를 block 단위로 exact 채점합니다.
ANN, row pruning, quantization으로 전수검색을 대체하지 않습니다. lexical과 reranker shadow는
별도 평가 결과이며 주 검색 순위를 변경하지 않습니다. 문서 집계 profile의 선택과 검증은
[평가 안내](EVALUATION.md)의 봉인 절차를 따릅니다.

각 요청은 하나의 generation handle을 고정합니다. cursor와 `expected_generation_id`는
generation/query identity가 바뀌면 명시적으로 실패하며, 이전 결과와 새 세대를 섞지 않습니다.
출시일·요약 metadata cache도 generation, revision과 parser version에 결속하고 용량을 제한합니다.

Worker는 immutable CAS와 generation DB/vector를 검증한 후 manifest, READY, channel pointer
순서로 게시합니다. pointer가 마지막 commit 경계입니다. MCP는 staging에 내려받아 hash·schema를
검증하고 fsync 후 원자적으로 활성 handle을 교체합니다. 검증 실패 시 이미 준비된 이전 세대를 유지합니다.
백업과 복구 중에도 이 불변성은 유지해야 합니다. [복구 안내](RECOVERY.md)를 참고하세요.
