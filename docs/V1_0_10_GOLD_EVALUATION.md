# CardRAG v1.0.10 gold evaluation

이 evaluator는 production MCP 검색을 호출하지 않는 offline 도구입니다. 별도 candidate
환경에서 미리 캡처한 다섯 비교군 JSONL과 익명 pairwise artifact만 읽으며 네트워크,
generation pointer, 운영 volume을 변경하지 않습니다.

실제 release gold set은 이 저장소의 합성 fixture와 구분합니다. 비민감 한국어 질문
300~500개와 source-span 정답을 별도 immutable artifact로 봉인하고 SHA-256을 release
명령에 반드시 전달합니다. 테스트 편의를 위한 `--fixture-mode`는 300개 gate와 정식
release 판정을 비활성화하므로 release 증거로 사용할 수 없습니다.

## 파일 계약

Gold JSONL은 한 줄에 `cardrag.gold-query.v1` 객체 하나를 둡니다. 파일은 UTF-8이고
각 줄은 UTF-8 key 정렬·공백 없는 canonical JSON이어야 하며 마지막 줄도 newline으로
끝나야 합니다. gold/run/blind 모두 중복 JSON key, NaN/Infinity, 비canonical encoding,
중복 `query_id`, 한도 초과, symlink와 실행 중 변경을 fail-closed합니다.

```json
{"condition_groups":[{"at_k":10,"span_ids":["benefit-1","condition-1"]}],"contracts":[{"contract_revision_id":"contract_current","relevance":3}],"expected_numeric_facts":["월 10,000원"],"expected_revision_ids":["contract_current"],"high_risk":true,"no_answer":false,"query_id":"gold-001","question":"이 카드의 혜택과 월 한도는?","schema_version":"cardrag.gold-query.v1","slices":["benefit","issuer:kb","limit"],"spans":[{"contract_revision_id":"contract_current","page":1,"relevance":3,"roles":["benefit"],"source_end":20,"source_start":0,"span_id":"benefit-1","text_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},{"contract_revision_id":"contract_current","page":2,"relevance":3,"roles":["condition","numeric","revision"],"source_end":45,"source_start":21,"span_id":"condition-1","text_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}]}
```

`no_answer=true`이면 positive contract/span/numeric/revision/condition 정답을 함께 둘 수
없습니다. 그 외 질문은 contract와 정확한 page/source range/hash를 가진 span을 최소
하나씩 가져야 합니다. `condition_groups`는 혜택 span과 조건·제외·유의사항 span을
묶으며 그룹별 `at_k`에서 모두 회수됐는지 평가합니다. numeric fact는 사람이 봉인한
표현을 exact set으로 비교하므로 생성 시 단위와 표기를 일관되게 유지합니다.

각 비교군 결과는 별도 JSONL이고 모든 gold `query_id`를 정확히 한 번 포함해야 합니다.
첫 줄은 `cardrag.gold-run-artifact.v1` manifest, 나머지는
`cardrag.gold-run-result.v1` query 결과입니다. manifest의 `gold_sha256`, query count,
source commit, generation/manifest hash가 실제 artifact를 고정합니다. `answer.text`는
블라인드 평가에 실제 제시한 완결된 답변 원문이며 비어 있거나 앞뒤 공백·제어문자가
있을 수 없습니다. evaluator는 이 UTF-8 bytes의 SHA-256을 블라인드 pair와 대조합니다.

v1.0.9 baseline manifest는 model/dimension/schema뿐 아니라 당시 구현의 RRF 상수까지
봉인합니다. 각 query result의 `v109_baseline`에는 RRF 결합 전 exact dense 원순위를
`dense_contracts`와 `dense_spans`로 별도 보존해야 합니다. evaluator는 모든 질의에 이
trace가 없으면 중단하며 report의 `baseline_trace.dense_raw_query_count`로 전건 포함을
증명합니다. lane 이름만 `v109_baseline`으로 적는 것으로는 baseline 증거가 되지 않습니다.

```json
{"embedding_dimension":1536,"embedding_model":"openai/text-embedding-3-small","generation_id":"GENERATION_ID","generation_manifest_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","gold_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","lane":"v109_baseline","primary_lane":null,"profile_id":"cardrag.eval.v109-small-rrf.v1","query_count":300,"retrieval_policy":"small_rrf","rrf_k":60,"schema_version":"cardrag.gold-run-artifact.v1","serving_schema":"cardrag.serving-db.v4","shadow_model":null,"shadow_only":false,"source_commit":"fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113","source_version":"v1.0.9"}
{"answer":{"citation_span_ids":[],"no_answer":false,"numeric_facts":[],"selected_revision_ids":[],"text":"봉인된 v1.0.9 답변 원문"},"contracts":[{"contract_revision_id":"contract_current","rank":1,"score":0.8}],"lane":"v109_baseline","query_id":"gold-001","schema_version":"cardrag.gold-run-result.v1","spans":[{"contract_revision_id":"contract_current","rank":1,"score":0.8,"span_id":"chunk-1"}],"v109_baseline":{"dense_contracts":[{"contract_revision_id":"contract_current","rank":1,"score":0.91}],"dense_spans":[{"contract_revision_id":"contract_current","rank":1,"score":0.91,"span_id":"chunk-2"}],"kind":"v109_small_rrf","rrf_k":60}}
```

Qwen lane은 `qwen/qwen3-embedding-8b`, 4,096D와 각 profile/schema를 정확히
요구합니다. lexical/reranker artifact는 exact artifact와 동일 generation manifest 및
source commit을 가리켜야 합니다. 각 query의 primary contracts/spans/answer도
`qwen_structure_exact`와 byte-equivalent model 값이어야 하며 차이가 있으면
`shadow_changed_primary_result`로 중단합니다. 별도의 shadow 순위만 `shadow` 필드에
둡니다. 따라서 `influenced_primary_ordering=false`라는 주장만으로 불변성을 통과할 수
없습니다.

`v109_baseline`은 위 historical commit에 계속 고정하지만 나머지 네 candidate lane은
생성·재검증·release에서 모두 명시적인 `--expected-source-commit`과 일치해야 합니다.
release workflow는 필수 기술 입력 `candidate_source_commit`을 이 값으로 전달하므로,
내부적으로 서로 일관되더라도 다른 candidate commit에서 생성한 run·score·attestation은 새
tag의 evidence로 재사용할 수 없습니다.

```json
{"answer":{"citation_span_ids":["benefit-1","condition-1"],"no_answer":false,"numeric_facts":["월 10,000원"],"selected_revision_ids":["contract_current"],"text":"월 10,000원 한도와 적용 조건을 함께 확인해야 합니다."},"contracts":[{"contract_revision_id":"contract_current","rank":1,"score":0.93}],"lane":"lexical_shadow","query_id":"gold-001","schema_version":"cardrag.gold-run-result.v1","shadow":{"contracts":[{"contract_revision_id":"contract_current","rank":1,"score":0.93}],"influenced_primary_ordering":false,"kind":"lexical","spans":[{"contract_revision_id":"contract_current","rank":1,"score":1.0,"span_id":"benefit-1"},{"contract_revision_id":"contract_current","rank":2,"score":0.9,"span_id":"condition-1"}]},"spans":[{"contract_revision_id":"contract_current","rank":1,"score":0.93,"span_id":"benefit-1"},{"contract_revision_id":"contract_current","rank":2,"score":0.91,"span_id":"condition-1"}]}
```

rank는 1부터 빈틈없이 정렬되어야 하고 ID는 한 결과 안에서 중복될 수 없습니다. answer
citation과 selected revision은 해당 결과가 회수한 span/contract만 참조할 수 있습니다.
필수 lane 이름은 다음과 같습니다.

- `v109_baseline`: v1.0.9 Small + RRF
- `qwen_page`: Qwen + 기존 page/1,600자 방식
- `qwen_structure_exact`: Qwen + structure view + exact scan
- `lexical_shadow`: structure exact + lexical shadow 결과
- `reranker_shadow`: structure exact + reranker shadow 결과

## 실제 lane capture와 raw provenance

`evaluation.py`는 이미 봉인된 결과만 읽으므로 lane JSONL을 사람이 직접 작성해서는 안
됩니다. `cardrag_mcp.gold_capture`가 producer와 evaluator 사이의 fail-closed 경계입니다.
모든 입력과 출력은 symlink/special file을 거부하고 `O_NOFOLLOW`로 읽으며, canonical
JSON/JSONL, 읽는 중 inode·size·mtime 변경, source generation/DB/sidecar SHA-256,
gold/query 순서와 전건 coverage를 검증합니다. 출력은 임시 파일을 fsync한 뒤 hard-link로
한 번만 publish합니다. 기존 출력이 있으면 byte-identical 재실행만 허용합니다.

v5의 세 lane은 native producer가 실제 `V5ExactRepository.search`, FTS lexical audit,
`RerankerShadowLane.observe`를 호출합니다. 먼저 `cardrag-aggregation-capture`가 만든 네 파일을
요구합니다.

- `document-aggregation-scores.jsonl`: v2 manifest와 query별 offset/count/segment SHA
- `document-aggregation-corpus-inventory.jsonl`: 고정 row provenance를 정확히 한 번 기록
- `document-aggregation-score-matrix.f32`: query-major/row-major little-endian float32 점수
- `document-aggregation-query-vectors.f32`: 4,096D little-endian float32 query 벡터

각 파일은 95,000,000 bytes 이하이고, `score_count == query_count * corpus_row_count <=
20,000,000`이므로 점수 행렬은 최대 80,000,000 bytes입니다. manifest가 나머지 세 파일의
SHA-256과 크기를 결속합니다. native capture는 `expected_rows == scored_rows`, DB/sidecar와
exact-row corpus binding을 확인하고, 실제 scorer를 512행(`VECTOR_BLOCK_ROWS`) 블록으로
한 번 더 실행해 각 row의 provenance와 `<f4` 점수 bytes를 sidecar segment와 exact
비교합니다. 전체 fresh
row tuple은 만들지 않고 계약별 집계도 CONTRACT 값 하나와 child 상위 3개만 유지합니다.
release 최소 300질의와 전체 20,000,000-score 한도에서 live corpus는 최대 66,666행이며,
shared capture도 이 상한을 명시적으로 거부 경계로 사용합니다.

exact 결과를 primary로 한 번만 만든 뒤 lexical/reranker JSONL에는 byte-equivalent primary
model을 복사하고 shadow 순위만 별도 저장합니다. query별 immutable shard 때문에 중단 후
재개할 수 있으며, 완료된 shard는 embedding/reranker provider를 다시 호출하지 않습니다.
source DB는 시작 시 SHA-256과 inode identity를 잡고 모든 pathname 재사용 뒤 다시
rehash/re-stat하므로 중간 atomic replacement도 성공으로 봉인하지 않습니다.

aggregation score artifact의 `raw_*` coverage는 집계 profile 비교를 위한 unscoped current
전수 행을 뜻합니다. 실제 exact API의 `expected/scored_*` coverage는 같은 질의에서 catalog
resolution과 temporal scope를 적용한 runtime 집합입니다. product-specific 질의에서는
runtime 수가 raw보다 작을 수 있으며, capture는 raw row를 해당 active revision 집합으로
결정적으로 제한해 run 순위를 만들고 API coverage/rank prefix와 대조합니다. discovery
질의에서는 두 집합이 동일해야 합니다.

답변은 retrieval과 분리한 두 단계로 만듭니다. 첫 단계 `bootstrap_retrieval`은
`validation_profile=release_grade`, `release_eligible=false`, `answer_evidence=null`이며 gold answer
label이나 producer answer evidence를 입력받지 않습니다. Native capture는 answer artifact 자체를
받지 않고 고정 abstention을 내부에서 만듭니다. External observation은 run schema의 answer 필드를
채우기 위해 별도의 canonical no-answer bootstrap artifact를 사용하지만, 모든 query에
`제공된 검색 근거에서 답을 확인할 수 없습니다.`와 빈 evidence tuple만 넣고 gold label은 읽지
않습니다. 생성 명령은 `V1_0_10_EXTERNAL_GOLD_PRODUCER.md`의 create-only 예제를 따릅니다. 이
단계에서 source/DB/raw scores/run/attestation을 모두 검산합니다. 그 ranking으로 answer input을
만든 뒤 producer는
`cardrag.gold-answer-artifact.v1`, call ledger, state identity, immutable `state-bundle.jsonl`,
producer receipt와 선택적 decision artifact를 봉인합니다. state bundle verifier는 reservation,
request, shard, decision, ledger와 최종 `AnswerRecord`를 의미적으로 replay합니다.

마지막 native `finalize-native-v5`는 bootstrap run의 contracts/spans/shadow를 그대로 두고 answer
필드와 관련 hash만 교체합니다. provider client를 만들지 않으며 embedding/reranker/answer
provider 호출 수는 0입니다. 세 native lane은 `qwen_structure_exact`의 동일 answer chain을
공유합니다. external final은 최종 answer artifact로 observation을 다시 만들되 bootstrap과 다른
immutable observation/sidecar/run/receipt 경로를 사용합니다. final
receipt만 `capture_phase=final_release`, `validation_profile=release_grade`,
`release_eligible=true`가 될 수 있습니다.

v1.0.9와 Qwen 1,600-char page runtime은 v1.0.10 MCP에 존재하지 않으므로 current runtime을
그 lane처럼 가장해 실행하지 않습니다. 두 lane은 `external_reproducible` 입력만 허용합니다.
입력 manifest와 query record는 다음을 모두 가져야 합니다.

- `synthetic=false`, sealed gold/source commit/generation manifest/DB/sidecar/profile hash
- corpus inventory 전 행의 row/evidence/contract/span/input/vector SHA
- little-endian float32 dense-score/query-vector sidecar의 shape, offset, segment SHA
- corpus inventory를 한 번만 기록하고 query별 전체 row score는 sidecar에만 보존
- v1.0.9의 경우 실제 v4 FTS에서 재계산한 top-250 lexical rank, dense top-250 cutoff 및
  `RRF_K=60`
- `expected_rows == scored_rows`, `expected_contracts == scored_contracts`, query exact-once

Qwen page 입력은 별도 `cardrag.evaluation-page-generation.v2` manifest와 read-only SQLite
`cardrag.evaluation-page.v1` DB를 요구합니다. DB의 `evaluation_chunks` row provenance,
정확한 10-column 계약, source text/range proof, parent v5 source commit/generation
manifest/serving DB binding, `cardrag.page-window-1600.v1`, `maximum_chars=1600`,
`overlap_chars=160`, 4,096D normalized `vectors.f32`를 전부 재검산합니다. 이 명시 계약을
구현한 실제 artifact가 없으면 `qwen_page`를 만들 수 없습니다.
v1.0.9도 v4 DB의 inline 1,536D vectors와 FTS를 직접 재계산하므로 raw dense trace가 없는
기존 report나 lane 이름만 적은 JSONL은 봉인되지 않습니다.

정식 v1.0.9 seal은 source commit `fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113`뿐 아니라
preserved generation `g-2208f0c6076649c4be915be1-d11f80f9af71`, generation manifest
SHA-256 `dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002`/
542,209 bytes, serving DB SHA-256
`d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f`/58,466,304
bytes의 고정 anchor도 검사합니다.

테스트의 `--fixture-mode`는 작은 schema fixture만 허용할 뿐 `synthetic=true`를 허용하지
않습니다. fixture receipt와 capture-set receipt는 `release_eligible=false`로 봉인되므로
release evidence로 인정되지 않습니다.

### Capture 명령

native v5 세 lane은 다음처럼 생성합니다. API key 파일은 regular file이어야 하고 값은
출력하지 않습니다. provider base URL은 credential을 읽거나 embedding/reranker client를 만들기
전에 검증하며 HTTPS가 아니거나 userinfo, query, fragment, 제어문자를 포함하면 중단합니다.

```bash
SOURCE_COMMIT=$(git rev-parse HEAD)
uv run cardrag-gold-capture native-v5 \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --score-artifact /candidate-evidence/document-aggregation-scores.jsonl \
  --score-corpus-inventory /candidate-evidence/document-aggregation-corpus-inventory.jsonl \
  --score-matrix /candidate-evidence/document-aggregation-score-matrix.f32 \
  --score-query-vector-matrix /candidate-evidence/document-aggregation-query-vectors.f32 \
  --expected-score-artifact-sha256 SCORE_SHA256 \
  --generation-manifest /candidate-generation/manifest.json \
  --generation-dir /candidate-generation/GENERATION_ID \
  --object-root /candidate-state/objects \
  --source-commit "$SOURCE_COMMIT" \
  --expected-source-commit "$SOURCE_COMMIT" \
  --openrouter-api-key-file /run/secrets/openrouter_api_key \
  --state-dir /candidate-evidence/gold-capture-state \
  --output-dir /candidate-evidence
```

v1.0.9 또는 Qwen page bootstrap observation은 각각 다음 명령으로 독립 검산·봉인합니다.
Qwen page는 `--vectors`와 parent v5의 `--source-generation-manifest`/`--source-database`가
필수이고 v1.0.9는 세 인자가 모두 금지됩니다. 아래 observation/run/receipt/sidecar는 모두
`bootstrap/` 전용 경로이며 final 단계에서 덮어쓰지 않습니다.

```bash
uv run cardrag-gold-capture external \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --observation /candidate-evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-observation-sha256 BOOTSTRAP_OBSERVATION_SHA256 \
  --inventory /candidate-evidence/raw/qwen-page-corpus.jsonl \
  --expected-inventory-sha256 INVENTORY_SHA256 \
  --generation-manifest /candidate-evidence/raw/qwen-page-manifest.json \
  --database /candidate-evidence/raw/qwen-page.sqlite3 \
  --source-generation-manifest /candidate-generation/manifest.json \
  --source-database /candidate-generation/GENERATION_ID/index.sqlite3 \
  --vectors /candidate-evidence/raw/qwen-page-vectors.f32 \
  --score-matrix /candidate-evidence/bootstrap/qwen_page.dense-scores.f32 \
  --query-vector-matrix /candidate-evidence/bootstrap/qwen_page.query-vectors.f32 \
  --output /candidate-evidence/bootstrap/qwen_page.jsonl \
  --receipt /candidate-evidence/bootstrap/qwen_page.capture-receipt.json
```

v1.0.9 bootstrap/final observation 생성에는 lexical sidecar와 preserved run/generation/manifest/DB
네 anchor가 모두 필요합니다. Qwen page에는 lexical sidecar를 전달하지 않습니다.

native 결과는 network 없이 `validate-native-v5`로 score/answer/generation/DB/sidecar/run/
query shard/reranker artifacts를 원점에서 다시 읽을 수 있습니다.

```bash
uv run cardrag-gold-capture validate-native-v5 \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --score-artifact /candidate-evidence/document-aggregation-scores.jsonl \
  --score-corpus-inventory /candidate-evidence/document-aggregation-corpus-inventory.jsonl \
  --score-matrix /candidate-evidence/document-aggregation-score-matrix.f32 \
  --score-query-vector-matrix /candidate-evidence/document-aggregation-query-vectors.f32 \
  --generation-manifest /candidate-generation/manifest.json \
  --generation-dir /candidate-generation/GENERATION_ID \
  --object-root /candidate-state/objects \
  --attestation /candidate-evidence/native-v5-attestation.jsonl \
  --reranker-state-root /candidate-evidence/gold-capture-state \
  --run qwen_structure_exact=/candidate-evidence/qwen_structure_exact.jsonl \
  --run lexical_shadow=/candidate-evidence/lexical_shadow.jsonl \
  --run reranker_shadow=/candidate-evidence/reranker_shadow.jsonl \
  --receipt qwen_structure_exact=/candidate-evidence/qwen_structure_exact.capture-receipt.json \
  --receipt lexical_shadow=/candidate-evidence/lexical_shadow.capture-receipt.json \
  --receipt reranker_shadow=/candidate-evidence/reranker_shadow.capture-receipt.json
```

답변 producer가 완료된 뒤 native bootstrap을 offline final로 승격합니다. 아래 answer retrieval
경로는 모두 위 bootstrap을 가리키며, `--answer-retrieval-corpus-inventory`, dense matrix,
query-vector matrix도 producer receipt와 다시 대조됩니다. decision artifact를 봉인한 profile이면
`--answer-decision`과 `--expected-answer-decision-sha256` 쌍도 추가합니다.

```bash
uv run cardrag-gold-capture finalize-native-v5 \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --score-artifact /candidate-evidence/document-aggregation-scores.jsonl \
  --score-corpus-inventory /candidate-evidence/document-aggregation-corpus-inventory.jsonl \
  --score-matrix /candidate-evidence/document-aggregation-score-matrix.f32 \
  --score-query-vector-matrix /candidate-evidence/document-aggregation-query-vectors.f32 \
  --generation-manifest /candidate-generation/manifest.json \
  --generation-dir /candidate-generation/GENERATION_ID \
  --object-root /candidate-state/objects \
  --bootstrap-attestation /candidate-evidence/bootstrap/native-v5-attestation.jsonl \
  --bootstrap-run qwen_structure_exact=/candidate-evidence/bootstrap/qwen_structure_exact.jsonl \
  --bootstrap-run lexical_shadow=/candidate-evidence/bootstrap/lexical_shadow.jsonl \
  --bootstrap-run reranker_shadow=/candidate-evidence/bootstrap/reranker_shadow.jsonl \
  --bootstrap-receipt qwen_structure_exact=/candidate-evidence/bootstrap/qwen_structure_exact.capture-receipt.json \
  --bootstrap-receipt lexical_shadow=/candidate-evidence/bootstrap/lexical_shadow.capture-receipt.json \
  --bootstrap-receipt reranker_shadow=/candidate-evidence/bootstrap/reranker_shadow.capture-receipt.json \
  --expected-bootstrap-receipt-sha256 qwen_structure_exact=EXACT_BOOTSTRAP_RECEIPT_SHA256 \
  --expected-bootstrap-receipt-sha256 lexical_shadow=LEXICAL_BOOTSTRAP_RECEIPT_SHA256 \
  --expected-bootstrap-receipt-sha256 reranker_shadow=RERANKER_BOOTSTRAP_RECEIPT_SHA256 \
  --reranker-state-root /candidate-evidence/gold-capture-state \
  --answer-input /candidate-evidence/answers/qwen_structure_exact.input.jsonl \
  --expected-answer-input-sha256 ANSWER_INPUT_SHA256 \
  --answer-producer-receipt /candidate-evidence/answers/qwen_structure_exact.producer-receipt.json \
  --expected-answer-producer-receipt-sha256 ANSWER_RECEIPT_SHA256 \
  --answer-artifact /candidate-evidence/answers/qwen_structure_exact.answers.jsonl \
  --expected-answer-artifact-sha256 ANSWER_SHA256 \
  --answer-call-ledger /candidate-evidence/answers/qwen_structure_exact.call-ledger.jsonl \
  --answer-state-identity /candidate-evidence/answers/qwen_structure_exact.state-identity.json \
  --answer-state-bundle /candidate-evidence/answers/qwen_structure_exact.state-bundle.jsonl \
  --answer-profile-id ANSWER_PROFILE_ID \
  --answer-retrieval-run /candidate-evidence/bootstrap/qwen_structure_exact.jsonl \
  --expected-answer-retrieval-run-sha256 EXACT_BOOTSTRAP_RUN_SHA256 \
  --answer-retrieval-capture-receipt /candidate-evidence/bootstrap/qwen_structure_exact.capture-receipt.json \
  --expected-answer-retrieval-capture-receipt-sha256 EXACT_BOOTSTRAP_RECEIPT_SHA256 \
  --answer-retrieval-attestation /candidate-evidence/bootstrap/native-v5-attestation.jsonl \
  --expected-answer-retrieval-attestation-sha256 NATIVE_BOOTSTRAP_ATTESTATION_SHA256 \
  --answer-retrieval-raw-score /candidate-evidence/document-aggregation-scores.jsonl \
  --expected-answer-retrieval-raw-score-sha256 SCORE_SHA256 \
  --answer-retrieval-corpus-inventory /candidate-evidence/document-aggregation-corpus-inventory.jsonl \
  --expected-answer-retrieval-corpus-inventory-sha256 CORPUS_SHA256 \
  --answer-retrieval-dense-score-matrix /candidate-evidence/document-aggregation-score-matrix.f32 \
  --expected-answer-retrieval-dense-score-matrix-sha256 SCORE_MATRIX_SHA256 \
  --answer-retrieval-query-vector-matrix /candidate-evidence/document-aggregation-query-vectors.f32 \
  --expected-answer-retrieval-query-vector-matrix-sha256 QUERY_VECTOR_MATRIX_SHA256 \
  --output-dir /candidate-evidence/final
```

외부 lane finalization은 같은 경로를 재실행하는 작업이 아닙니다. 최종 answer artifact로
observation을 다시 만들고 `final/`의 새 immutable observation/sidecar/run/receipt 경로를
사용합니다. Qwen page final seal의 전체 answer evidence 인자는 다음과 같습니다. 모든
`--answer-retrieval-*` 경로는 AnswerInput을 만든 `bootstrap/` artifact를 가리킵니다.

```bash
uv run cardrag-gold-capture external \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --observation /candidate-evidence/final/qwen_page.capture-attestation.jsonl \
  --expected-observation-sha256 FINAL_PAGE_OBSERVATION_SHA256 \
  --inventory /candidate-evidence/raw/qwen-page-corpus.jsonl \
  --expected-inventory-sha256 PAGE_INVENTORY_SHA256 \
  --generation-manifest /candidate-evidence/raw/qwen-page-manifest.json \
  --database /candidate-evidence/raw/qwen-page.sqlite3 \
  --source-generation-manifest /candidate-generation/manifest.json \
  --source-database /candidate-generation/GENERATION_ID/index.sqlite3 \
  --vectors /candidate-evidence/raw/qwen-page-vectors.f32 \
  --score-matrix /candidate-evidence/final/qwen_page.dense-scores.f32 \
  --query-vector-matrix /candidate-evidence/final/qwen_page.query-vectors.f32 \
  --output /candidate-evidence/final/qwen_page.jsonl \
  --receipt /candidate-evidence/final/qwen_page.capture-receipt.json \
  --answer-input /candidate-evidence/answers/qwen_page.input.jsonl \
  --expected-answer-input-sha256 PAGE_ANSWER_INPUT_SHA256 \
  --answer-producer-receipt /candidate-evidence/answers/qwen_page.producer-receipt.json \
  --expected-answer-producer-receipt-sha256 PAGE_ANSWER_RECEIPT_SHA256 \
  --answer-artifact /candidate-evidence/answers/qwen_page.answers.jsonl \
  --expected-answer-artifact-sha256 PAGE_ANSWER_SHA256 \
  --answer-call-ledger /candidate-evidence/answers/qwen_page.call-ledger.jsonl \
  --answer-state-identity /candidate-evidence/answers/qwen_page.state-identity.json \
  --answer-state-bundle /candidate-evidence/answers/qwen_page.state-bundle.jsonl \
  --answer-profile-id cardrag.answer.extractive-k8.v1 \
  --answer-retrieval-run /candidate-evidence/bootstrap/qwen_page.jsonl \
  --expected-answer-retrieval-run-sha256 PAGE_BOOTSTRAP_RUN_SHA256 \
  --answer-retrieval-capture-receipt /candidate-evidence/bootstrap/qwen_page.capture-receipt.json \
  --expected-answer-retrieval-capture-receipt-sha256 PAGE_BOOTSTRAP_RECEIPT_SHA256 \
  --answer-retrieval-attestation /candidate-evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-answer-retrieval-attestation-sha256 PAGE_BOOTSTRAP_OBSERVATION_SHA256 \
  --answer-retrieval-raw-score /candidate-evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-answer-retrieval-raw-score-sha256 PAGE_BOOTSTRAP_OBSERVATION_SHA256 \
  --answer-retrieval-corpus-inventory /candidate-evidence/raw/qwen-page-corpus.jsonl \
  --expected-answer-retrieval-corpus-inventory-sha256 PAGE_INVENTORY_SHA256 \
  --answer-retrieval-dense-score-matrix /candidate-evidence/bootstrap/qwen_page.dense-scores.f32 \
  --expected-answer-retrieval-dense-score-matrix-sha256 PAGE_BOOTSTRAP_DENSE_SHA256 \
  --answer-retrieval-query-vector-matrix /candidate-evidence/bootstrap/qwen_page.query-vectors.f32 \
  --expected-answer-retrieval-query-vector-matrix-sha256 PAGE_BOOTSTRAP_QUERY_VECTOR_SHA256
```

v1.0.9 final observation은 `V1_0_10_EXTERNAL_GOLD_PRODUCER.md`에 고정한 preserved run,
generation, manifest SHA, DB SHA 네 anchor와 final lexical path를 모두 다시 받습니다. final seal은
core `--lexical-ranks`와 bootstrap `--answer-retrieval-lexical-ranks`/expected SHA를 추가하고,
Qwen page는 이 lexical 인자들을 금지합니다. decision artifact를 쓴 lane은 decision path/SHA
쌍도 추가합니다.

마지막으로 다섯 final run·receipt·attestation을 `validate-set`에 전달하고 각 receipt의 SHA를
명시하여 `gold-capture-set-receipt.json`을 봉인합니다. 세 native lane의 `--attestation`은 동일한
final `native-v5-attestation.jsonl`을 가리켜야 합니다. `validate-set`은 모든 gold query의 순서와
전건 coverage, compact corpus/score/query-vector segment hash, expected/scored equality, source
generation/DB/sidecar, run 결과 hash, source commit/profile 및 shadow primary 불변성의 canonical
cross-binding을 다시 확인합니다. 이 명령은 binding validator이지 source replay가 아니며, 대용량
source DB/vector를 다시 열거나 provider를 다시 호출하지 않습니다. 따라서 `validate-set` 출력만으로는
source가 실제로 재생·재검산되었다는 사실을 증명하지 못합니다. source 재검산은 바로 앞의
distinct-path external final seal과 `validate-native-v5`가 담당합니다. 그 절차를 실제로 수행해
생성한 set receipt의 canonical full-file SHA를 release workflow에 전달하고 workflow가 다시 계산해
일치시킬 때 그 digest가 명시적 trust root가 됩니다. source replay를 생략한 self-asserted
receipt는 단순 binding receipt일 뿐 release evidence가 될 수 없습니다.

```bash
uv run cardrag-gold-capture validate-set \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --native-score-artifact /candidate-evidence/document-aggregation-scores.jsonl \
  --native-score-corpus-inventory /candidate-evidence/document-aggregation-corpus-inventory.jsonl \
  --native-score-matrix /candidate-evidence/document-aggregation-score-matrix.f32 \
  --native-score-query-vector-matrix /candidate-evidence/document-aggregation-query-vectors.f32 \
  --external-inventory v109_baseline=/candidate-evidence/raw/v109_baseline.corpus.jsonl \
  --external-inventory qwen_page=/candidate-evidence/raw/qwen-page-corpus.jsonl \
  --external-score-matrix v109_baseline=/candidate-evidence/final/v109_baseline.dense-scores.f32 \
  --external-score-matrix qwen_page=/candidate-evidence/final/qwen_page.dense-scores.f32 \
  --external-query-vector-matrix v109_baseline=/candidate-evidence/final/v109_baseline.query-vectors.f32 \
  --external-query-vector-matrix qwen_page=/candidate-evidence/final/qwen_page.query-vectors.f32 \
  --external-lexical-ranks v109_baseline=/candidate-evidence/final/v109_baseline.lexical-ranks.jsonl \
  --output /candidate-evidence/final/gold-capture-set-receipt.json \
  --run v109_baseline=/candidate-evidence/final/v109_baseline.jsonl \
  --run qwen_page=/candidate-evidence/final/qwen_page.jsonl \
  --run qwen_structure_exact=/candidate-evidence/final/qwen_structure_exact.jsonl \
  --run lexical_shadow=/candidate-evidence/final/lexical_shadow.jsonl \
  --run reranker_shadow=/candidate-evidence/final/reranker_shadow.jsonl \
  --receipt v109_baseline=/candidate-evidence/final/v109_baseline.capture-receipt.json \
  --receipt qwen_page=/candidate-evidence/final/qwen_page.capture-receipt.json \
  --receipt qwen_structure_exact=/candidate-evidence/final/qwen_structure_exact.capture-receipt.json \
  --receipt lexical_shadow=/candidate-evidence/final/lexical_shadow.capture-receipt.json \
  --receipt reranker_shadow=/candidate-evidence/final/reranker_shadow.capture-receipt.json \
  --attestation v109_baseline=/candidate-evidence/final/v109_baseline.capture-attestation.jsonl \
  --attestation qwen_page=/candidate-evidence/final/qwen_page.capture-attestation.jsonl \
  --attestation qwen_structure_exact=/candidate-evidence/final/native-v5-attestation.jsonl \
  --attestation lexical_shadow=/candidate-evidence/final/native-v5-attestation.jsonl \
  --attestation reranker_shadow=/candidate-evidence/final/native-v5-attestation.jsonl \
  --expected-receipt-sha256 v109_baseline=V109_RECEIPT_SHA256 \
  --expected-receipt-sha256 qwen_page=PAGE_RECEIPT_SHA256 \
  --expected-receipt-sha256 qwen_structure_exact=EXACT_RECEIPT_SHA256 \
  --expected-receipt-sha256 lexical_shadow=LEXICAL_RECEIPT_SHA256 \
  --expected-receipt-sha256 reranker_shadow=RERANKER_RECEIPT_SHA256
```

정식 `validate-set`에는 위 명령에 answer lane 세 개(`v109_baseline`, `qwen_page`,
`qwen_structure_exact`) 각각의 다음 `LANE=value` 옵션도 정확히 한 번씩 전달합니다:
`--answer-input`, `--expected-answer-input-sha256`, `--answer-producer-receipt`,
`--expected-answer-producer-receipt-sha256`, `--answer-artifact`,
`--expected-answer-artifact-sha256`, `--answer-call-ledger`, `--answer-state-identity`,
`--answer-state-bundle`, `--answer-profile-id`, `--answer-retrieval-run`,
`--expected-answer-retrieval-run-sha256`, `--answer-retrieval-capture-receipt`,
`--expected-answer-retrieval-capture-receipt-sha256`, `--answer-retrieval-attestation`,
`--expected-answer-retrieval-attestation-sha256`, `--answer-retrieval-raw-score`,
`--expected-answer-retrieval-raw-score-sha256`, `--answer-retrieval-corpus-inventory`,
`--expected-answer-retrieval-corpus-inventory-sha256`,
`--answer-retrieval-dense-score-matrix`,
`--expected-answer-retrieval-dense-score-matrix-sha256`,
`--answer-retrieval-query-vector-matrix`,
`--expected-answer-retrieval-query-vector-matrix-sha256`. v1.0.9에만
`--answer-retrieval-lexical-ranks`와 그 expected SHA 옵션이 추가되고, decision을 사용한
lane에만 decision path/SHA 쌍을 추가합니다.

release checkout의 `validate-set`은 대용량 generation DB나 Qwen page DB를 요구하지 않습니다.
각 answer chain에는 portable semantic verifier를 사용해 input/receipt/state-bundle/decision/
answer/ledger를 replay하고, bootstrap run·receipt·attestation·raw-score manifest·corpus·binary
sidecar 파일은 shared validator가 직접 rehash하여 producer receipt와 final lane receipt에
있는 binding과 대조합니다. 반면 최초 seal과 `finalize-native-v5`는 DB와 generation을 여는
strict source-replay 단계입니다. portable 검증은 source-replay를 대체하거나 self-asserted
bootstrap을 승격하는 경로가 아닙니다.

## 익명 pairwise 블라인드 평가

별도 `cardrag.blind-evaluation-artifact.v1` JSONL을 봉인합니다. 첫 줄 manifest는 gold,
`v109_baseline` run, `qwen_structure_exact` run의 실제 file SHA-256과 query/pair 수를
결속합니다. 이후 각 `cardrag.blind-pairwise-rating.v1`은 lane 이름 대신 익명 left/right
답변 hash와 후보 위치, pseudonymous `rater_key`, 자연스러움과 사실 완결성 선택
(`left`, `tie`, `right`)만 기록합니다. 평가 UI는 lane 정체와 `candidate_position`을
평가자에게 노출하지 않고, 봉인 단계에서만 위치를 합칩니다.

모든 query는 manifest의 `ratings_per_query`만큼 정확히 평가되어야 하고 같은 평가자의
query 중복, pair ID 중복, 불균형한 left/right 배정은 거부됩니다. evaluator는 답변 hash를
두 run의 `answer.text`에서 다시 계산하므로 rating 파일의 위치나 run을 사후 교체할 수
없습니다. 두 답변 hash가 같으면 두 항목 모두 `tie`여야 합니다. 각 선택은 candidate 기준
`+1/0/-1`로 바꾸고, 평가자 평균 후 query 단위 paired
bootstrap을 수행합니다. release gate는 자연스러움 delta의 CI95 하한이 0 이상이고 사실
완결성 delta의 CI95 하한이 0보다 클 때만 통과합니다.

```json
{"baseline_lane":"v109_baseline","baseline_run_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","candidate_lane":"qwen_structure_exact","candidate_run_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","gold_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","lane_identity_exposed_to_raters":false,"pair_count":300,"presentation_protocol":"anonymous-a-b.v1","query_count":300,"ratings_per_query":1,"rubric_id":"cardrag.blind-rubric.naturalness-factual-completeness.v1","schema_version":"cardrag.blind-evaluation-artifact.v1"}
{"candidate_position":"left","factual_completeness_preference":"left","left_answer_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","naturalness_preference":"tie","pair_id":"pair-0001","query_id":"gold-001","rater_key":"r1","right_answer_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","schema_version":"cardrag.blind-pairwise-rating.v1"}
```

## 실행

release gold 파일을 먼저 별도 증거 저장소에서 봉인합니다.

```bash
sha256sum /candidate-evidence/gold-v110.jsonl
```

그 hash를 evaluator에 다시 요구합니다. 아래 명령은 canonical JSON report를 stdout에
출력합니다.

```bash
uv run python -m cardrag_mcp.evaluation \
  --gold /candidate-evidence/gold-v110.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --blind-evaluation /candidate-evidence/blind-evaluation.jsonl \
  --run v109_baseline=/candidate-evidence/v109-baseline.jsonl \
  --run qwen_page=/candidate-evidence/qwen-page.jsonl \
  --run qwen_structure_exact=/candidate-evidence/qwen-structure-exact.jsonl \
  --run lexical_shadow=/candidate-evidence/lexical-shadow.jsonl \
  --run reranker_shadow=/candidate-evidence/reranker-shadow.jsonl \
  --bootstrap-samples 2000 \
  --bootstrap-seed 1010 \
  > /candidate-evidence/gold-evaluation-report.json
```

합성 fixture나 소규모 schema 개발에만 `--fixture-mode`를 사용할 수 있습니다. 이 모드는
report의 `release_gate.status`를 `not_evaluated`로 남깁니다.

정식 report는 candidate 검증 증거와 함께
`release-evidence/v1.0.10/gold-evaluation-report.json`에 봉인하고, 그 파일의 SHA-256을
release workflow의 `acceptance_report_sha256` 입력으로 전달합니다. 다섯 capture receipt를
묶은 `gold-capture-set-receipt.json`의 SHA-256도
`capture_set_receipt_sha256` 필수 기술 입력으로 별도 전달합니다. 정식 evidence
directory에는 `gold.jsonl`, 다섯 `<lane>.jsonl`, `blind-evaluation.jsonl`, report와 각 run
manifest가 참조한 원본 generation manifest를
`generation-manifests/<sha256>.json`, 다섯 `<lane>.capture-receipt.json`, capture set
receipt, `v109_baseline.capture-attestation.jsonl`,
`qwen_page.capture-attestation.jsonl`, `native-v5-attestation.jsonl`로 함께 둡니다.
workflow는 offline evaluator로
report를 처음부터 재계산해 canonical bytes가 정확히 같은지 확인하고 report input hash,
gold/run/blind file hash, answer hash, generation manifest hash를 모두 다시 검증합니다.
어느 파일도 없거나 hash/schema/canonical encoding/gate가 다르면 publish 전에
중단합니다. 합성 테스트 fixture를 이 경로에 복사해 release 증거로 사용해서는 안 됩니다.

Git commit SHA를 그 commit에 포함될 evidence bytes 안에 기록하는 자기참조를 피하기 위해
release source와 evidence commit을 분리합니다. 먼저 코드·workflow·문서가 모두 확정된 40자리
`candidate_source_commit`에서 candidate를 실행하고 모든 candidate artifact의 source commit을
그 값으로 봉인합니다. 그 뒤 `release-evidence/v1.0.10/`만 추가·변경한 별도 descendant commit에
tag를 생성합니다. dispatch에는 원래 candidate commit을 명시합니다. workflow는 그 값이 tag
commit의 strict ancestor인지 확인하고 두 commit 사이의 변경 경로가 정확히
`release-evidence/v1.0.10/` 아래에만 있는지 NUL-safe로 검사합니다. code, workflow, docs 또는
다른 version evidence가 하나라도 바뀌면 validation 전에 중단합니다.

구조형 exact lane의 계약 집계 방식은 별도
[`document aggregation profile`](V1_0_10_AGGREGATION_PROFILE.md) 절차로 평가합니다.
`document-aggregation-scores.jsonl`과 `document-aggregation-profile.json`도 같은 evidence
directory에 두며, release workflow는 profile 전체 SHA-256을 별도 입력으로 받아 세 집계
정책과 CI95를 원점수에서 다시 계산합니다. 이 profile gate와 아래 end-to-end gold report
gate가 모두 통과해야 합니다.

workflow와 같은 offline 검증은 다음 형태입니다. `REPORT_SHA256`은 newline을 포함한
report file 전체 bytes의 hash입니다.

```bash
uv run python -m cardrag_mcp.evaluation \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --blind-evaluation release-evidence/v1.0.10/blind-evaluation.jsonl \
  --validate-report release-evidence/v1.0.10/gold-evaluation-report.json \
  --expected-report-sha256 REPORT_SHA256 \
  --expected-source-commit "$SOURCE_COMMIT" \
  --generation-manifest-dir release-evidence/v1.0.10/generation-manifests \
  --run v109_baseline=release-evidence/v1.0.10/v109_baseline.jsonl \
  --run qwen_page=release-evidence/v1.0.10/qwen_page.jsonl \
  --run qwen_structure_exact=release-evidence/v1.0.10/qwen_structure_exact.jsonl \
  --run lexical_shadow=release-evidence/v1.0.10/lexical_shadow.jsonl \
  --run reranker_shadow=release-evidence/v1.0.10/reranker_shadow.jsonl \
  --bootstrap-samples 2000 \
  --bootstrap-seed 1010
```

## 산출 지표와 CI

모든 지표는 query macro average입니다. gold가 없는 지표는 해당 query를 denominator에서
제외하고 `eligible_queries`를 함께 기록합니다.

- contract Recall@10/50/100
- source-span Recall@5/10
- graded contract nDCG@10과 MRR@10
- benefit-condition group 동시 회수율
- numeric fact precision/recall/exact match
- expected revision exact accuracy
- no-answer accuracy, false-positive rate, false-negative rate
- answer citation span precision/recall
- expected span ID가 다른 contract를 주장하는지 보는 span-contract integrity

각 lane의 전체·모든 slice와 paired lane delta에 deterministic PCG64 percentile bootstrap
95% CI가 기록됩니다. seed, 표본 수, input SHA와 report SHA는 동일 입력 재평가를 재현할
수 있게 고정됩니다.

정식 release 모드는 300~500개, 네 카드사, 필수 도메인/slice, high-risk 질문과 sealed
gold SHA를 모두 요구합니다. `qwen_structure_exact`의 primary retrieval CI가 baseline보다
유의하게 우수한지, slice 유의 회귀가 없는지, condition 동시 회수율이 95% 이상인지,
high-risk numeric/condition omission·revision 오류·span contract mismatch가 0인지,
블라인드 자연스러움이 무회귀이고 사실 완결성이 향상됐는지 report의 `release_gate`에
남깁니다. quality gate 실패는 report를 생성하되 `status=failed`로 남기며,
입력 수·schema·SHA·coverage 위반은 report를 만들지 않고 reason code로 종료합니다.

필수 release slice 이름은 코드와 함께 고정됩니다: `benefit`, `earning`, `discount`,
`cashback`, `performance`, `exclusion`, `limit`, `frequency`, `minimum_payment`,
`annual_fee`, `issuance_condition`, `foreign_fee`, `negation`, `exception`, `grace_period`,
`table`, `footnote`, `cross_page`, `common_notice`, `hard_negative`, `current_history`,
`product_specific`, `discovery_recommendation`, `comparison`, `no_answer`, `long`,
`major:benefit`, `major:notice`와 `issuer:kb/samsung/shinhan/woori`입니다.
