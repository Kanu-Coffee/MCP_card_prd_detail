# CardRAG v1.0.10 구조·임베딩·검색 계약

이 문서는 v1.0.10 generation의 구조 보존, Qwen 임베딩, exact 전수검색 계약을
정의합니다. 기존 generation과의 이름 연속성을 지키기 위해 계획 초안의
`cardrag.serving.v5`는 `cardrag.serving-db.v5`로 정정합니다. v1.0.9의
`cardrag.serving-db.v4` reader는 rollback을 위해 제거하지 않습니다.

## 원문과 구조

OCR processor, prompt, page marker와 reuse key는 v1.0.9 계약을 유지합니다. 구조화는
검증된 OCR Markdown 뒤에서 실행되며 다음 계층을 만듭니다.

logo/pictogram만 있고 전사할 visible character가 없는 페이지는 sparse control prefix만
canonical body로 보존할 수 있습니다. fee8f65의 provider/checkpoint validator는 이
prefix-only 응답을 거부했지만, v1.0.9의 sealed local/remote cache consumer는 별도의 공통
`verify_ocr_bytes` 경계를 사용하며 prefix-only page를 포함한 canonical page bytes를
수용합니다. [실데이터 GET-only compatibility evidence](../release-evidence/v1.0.10/v109-prefix-only-cache-compatibility.json)는
실제 v1.0.9 Worker image로 두 cache entry의 READY/manifest/CAS, reuse key, page hash와
local/remote byte 일치를 검증했습니다. 그러므로 이 provider/checkpoint 수정 때문에 OCR
processor version이나 cache namespace를 바꾸지 않습니다. cache consumer 계약은 바뀌지
않았고 namespace bump는 v1.0.9 immutable OCR seed 재사용만 불필요하게 차단합니다.

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

`product_lineage_id`는 issuer, stable product code, document type의 canonical
identity입니다. `contract_revision_id`에는 lineage, source identity와 PDF hash가
결속됩니다. 증명할 수 없는 최신 순서는 임의로 선택하지 않고 `ambiguous`로
보존합니다. lineage마다 `current`는 최대 하나입니다.

각 canonical leaf span은 `contract_revision_id`, page, source character range와
SHA-256을 가집니다. 한 revision의 비공백 OCR 문자는 canonical span에 정확히 한
번 포함되어야 합니다. 파싱하지 못한 원문은 삭제하지 않고 `UNCLASSIFIED`에
포함합니다. parent와 모든 link는 같은 contract revision 안에서만 허용합니다.
표 container와 각 row를 함께 저장하며 row는 원래 header 문맥을 유지합니다.

일반 parser 또는 derived-view 생성이 한 문서에서 실패하면 Worker는
`cardrag.structure-unclassified-fallback.v1`을 적용합니다. 이 fallback은 같은
contract revision 아래에 중립 `ITEM` 하나를 만들고, 모든 OCR 원문을 완전한 source-line
경계의 `UNCLASSIFIED` canonical leaf로 보존합니다. 따라서 page/range/SHA, 원문 재구성,
비공백 coverage 100%와 contract 경계는 그대로 유지되며, 다음 문서 처리를 중단하지
않습니다. fallback으로 생성한 문서 목록과 artifact SHA는 Worker seal metrics 및 SQLite
metadata의 count/SHA에 결속됩니다.

한 source line 자체가 Qwen token/character 한도를 넘는 등 fallback view도 만들 수 없는
경우에는 해당 문서를 `cardrag.structure-failed-ledger.v1`에 PDF/OCR/source-pages SHA와
함께 기록합니다. Worker는 남은 문서의 OCR/구조 checkpoint까지 계속 만들지만 최종
publication은 fail-closed하며, run error에 failure count와 ledger SHA를 남깁니다. 성공한
v5 generation은 언제나 `structure_failed_document_count=0`과 canonical empty-ledger SHA를
seal metrics와 SQLite metadata 양쪽에서 증명해야 합니다.

v5 SQLite의 봉인된 논리 테이블은 다음과 같습니다.

- `issuers`, `product_lineages`, `contract_revisions`, `document_pages`
- `structure_nodes`, `node_spans`, `node_links`, `revision_coverage`
- `embedding_profiles`, `embedding_views`, `embedding_views_fts`
- `metadata`

## Qwen 임베딩 profile

기본 profile은 다음 값 전체로 식별합니다.

- provider: `openrouter`
- model: `qwen/qwen3-embedding-8b`
- provider route: allowlist의 `deepinfra` 또는 `nebius`
- dimension/dtype/layout: 4,096D, little-endian FP32, row-major
- normalization: L2
- document policy: `cardrag.structure-views.v1`; document instruction 없음
- query policy: `cardrag.qwen3-query.v1`
- truncation: error
- provider fallback: forbidden

질의는 아래 고정 영어 instruction을 사용하고 문서에는 instruction을 붙이지
않습니다.

```text
Instruct: Given a Korean financial product disclosure search query, retrieve relevant passages from Korean credit card product disclosure sections and sentence units that answer the query
Query:<한국어 질의>
```

provider route나 maximum token limit가 바뀌면 profile ID와 cache namespace가 함께
바뀝니다. v1.0.9 1,536D cache와 별도 legacy Qwen vectors는 v5 cache에서 읽지
않습니다. 입력은 provider 호출 전 exact token counter로 검사하고 자동 truncation을
허용하지 않습니다.

OpenRouter의 live 계약은 다음과 같이 해석합니다. request의 lower-case registry slug와
response의 canonical capitalization은 case-only 동일성으로 검증하며 별칭이나 다른
경로는 허용하지 않습니다. endpoint metadata가 `max_prompt_tokens=null`이면 양의 정수
`context_length`만 maximum token limit로 사용합니다. allowlisted endpoint metadata가
`dimensions`를 광고하지 않으므로 routing의 `require_parameters`는 false이지만,
`order=only=<한 provider>`, `allow_fallbacks=false`와 응답 provider 재검증으로 route를
고정합니다. metadata 선언 대신 실제 응답을 정확히 4,096D finite vector인지 검증합니다.

구조에서 만드는 view는 `TITLE`, `RAW_ITEM`, `CONTEXTUAL_ITEM`, `DETAIL`,
`MAJOR_SECTION`, `CONTRACT` 여섯 종류입니다. 계획의 exact lane 목록에서 빠져 있던
`DETAIL`도 “모든 활성 view” 원칙에 따라 dense lane에 포함합니다. `display_text`는
항상 OCR source span에서 만들고, breadcrumb가 포함된 `embedding_input`은 인용이나
사용자 응답 원문으로 사용하지 않습니다.

`cardrag.structure.v2`는 discovery에서 받은 `product_name`, `source_version`을 정리하거나
대체하지 않고 그대로 보존하며, 두 값은 trim된 비어 있지 않은 문자열이고 control
character가 없어야 합니다. `effective_date`는 canonical `YYYY-MM-DD` 또는 JSON `null`만
허용합니다. `CONTEXTUAL_ITEM`과 이를 분할한 `DETAIL`의 문서 입력 문맥은
`cardrag.contextual-item-context.v1`로 봉인하며 각 줄은 `{label}: {value}` 형식으로 다음
순서를 사용합니다.

```text
issuer
product_name
product_code
source_version
effective_date
contract_revision_id
major_class
heading (상위 구조 순서로 0회 이상 반복)
```

날짜가 없으면 `effective_date: null`로 명시합니다. 이 metadata와 label은
`embedding_input` 및 그 input/cache SHA-256에만 들어갑니다. OCR `display_text`, source
span, 사용자 인용에는 절대 합성하지 않습니다. SourceRecord의 metadata-only 관찰도
structure checkpoint와 corpus identity를 바꾸며, label 순서·null 정책은 Worker contract와
structure parser policy SHA-256에 포함됩니다. Qwen profile ID, cache namespace, 최대 token
계약 및 “document instruction 없음” 정책은 바꾸지 않습니다.

벡터는 SQLite BLOB이나 JSON이 아니라 generation의 `vectors.f32`에 저장합니다.
`embedding_views.row_index`가 sidecar row를 가리킵니다. manifest와 READY에는
sidecar SHA-256, byte size, row count, dimension, dtype, normalization이 결속됩니다.

## exact 검색

v5 기본 경로는 temporal filter 뒤의 모든 활성 embedding row를 block 단위로 읽어
정규화된 query와 정확 내적합니다. ANN, lexical prefilter, 차원 축소, 양자화와
문서 선행 candidate pruning을 사용하지 않습니다. 각 view lane의 점수를 보존하고
node 및 contract 점수로 집계한 뒤에만 결과 수를 제한합니다.

`SearchCoverage`는 최소한 expected/scored contract와 embedding row 수, block 수,
소요 시간, temporal scope를 반환합니다. 다음 값은 항상 봉인된 의미를 가집니다.

- `approximate=false`
- `lexical_influenced_ranking=false`
- `reranker_influenced_ranking=false`
- `scored_embedding_rows == expected_embedding_rows`

FTS는 숫자, 비율, 부정 표현과 exact 문자열의 추가 evidence를 찾는 shadow lane입니다.
FTS 결과는 dense contract 순위를 바꾸거나 dense evidence를 제거하지 못합니다.
RRF는 v4 compatibility에만 남습니다. v1.0.10의 reranker는 운영 ordering에
관여하지 않습니다. reranker shadow를 candidate에서 명시적으로 켜면 exact가 이미
전수 채점한 dense evidence 중 설정 상한만 결정적 dense 순서로 보내며, Fireworks
`qwen/qwen3-reranker-8b`만 `order`/`only`로 고정하고 fallback을 금지합니다. 결과는
별도 immutable artifact로만 기록하고 primary bundle 객체에는 적용하지 않습니다.
`SearchCoverage`의 optional shadow status/candidate/rank-change/artifact 진단과 무관하게
`reranker_influenced_ranking=false`는 유지됩니다. provider 오류나 잘못된 model,
provider, result count/index/finite score는 bounded shadow failure로 격리됩니다.

적중 node는 같은 contract 안에서 parent item, child detail/table row, footnote,
`APPLIES_TO` notice와 필요한 이웃으로 확장됩니다. 다른 contract의 parent, notice,
footnote는 절대 bundle에 섞지 않습니다. 상품 lineage가 하나로 명시되고 current
revision이 하나이면 최신 계약 전체를 원문 순서로 반환할 수 있습니다.

공통 조건·제외 `NOTICE` section은 parser가 그 container에서 앞선 각 `BENEFIT` item으로
`APPLIES_TO`를 명시하고, item 단위 조건은 해당 `NOTICE` item에서 직전 `BENEFIT` item으로
연결합니다. exact bundle은 각 링크가 명시한 notice container의 descendant만 보존합니다.
`FOOTNOTE_OF`, `CONTINUATION_OF`, `PREVIOUS`, `NEXT`는 기존처럼 endpoint의 가장 가까운
item 문맥만 한 hop 확장하며, 새로 포함된 link를 다시 순회하는 transitive closure는
허용하지 않습니다.

## public API와 promotion gate

v5 전용 MCP tools는 다음 세 개이며 기존 다섯 tools도 유지합니다.

- `search_contracts`
- `get_contract_bundle`
- `list_product_revisions`
- `search_evidence`, `get_evidence`, `get_product`, `get_source_pdf`, `get_source_page`

v5 promotion은 다음 중 하나라도 어긋나면 실패합니다.

- 모든 page/span SHA와 revision/aggregate coverage ledger
- 비공백 source coverage 100%
- cross-contract parent/link 0건, lineage 중복 current 0건
- view/node/profile/contiguous row index 결속
- sidecar hash, size=`rows × 4096 × 4`, finite와 L2 norm
- DB, manifest, READY의 schema/generation/count/policy/sidecar identity
- allowlist 밖 provider 또는 FP8 route

MCP updater는 DB와 sidecar를 별도 staging에서 hash 검증하고 fsync한 뒤 새 handle을
원자 활성화합니다. 실패하면 마지막 정상 generation을 유지합니다. request pin이
끝날 때까지 이전 v4/v5 handle을 보존하여 v4→v5→v4 rollback을 지원합니다.
