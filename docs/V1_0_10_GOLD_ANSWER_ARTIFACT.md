# CardRAG v1.0.10 gold answer artifact producer

`cardrag_mcp.gold_answer_artifact`는 봉인된 검색 결과로부터
`cardrag.gold-answer-artifact.v1` JSONL을 만드는 offline producer입니다. 출력은 기존
`cardrag_mcp.gold_capture`의 `AnswerArtifactManifest`와 `AnswerRecord`를 그대로 사용합니다.
따라서 native v5 capture가 별도 변환 없이 읽을 수 있고 v1.0.9/Qwen-page 외부 lane도
동일한 답변 schema를 사용할 수 있습니다.

이 도구에는 live provider adapter가 없습니다. 내장 extractive 방식이나 이미 봉인한
human/provider decision JSONL로 실행할 수 있습니다. 라이브 모델을 붙이려면 library의
`AnswerDecisionProvider` protocol을 별도 adapter로 구현해야 하며, adapter는 request의
`idempotency_key`를 provider 요청에 그대로 사용해야 합니다.

## 라벨 누출 방지와 답변 계약

producer는 gold에서 `query_id`와 질문 원문만 answer selection 경계로 투영합니다. gold의
정답 contract, span, numeric fact, revision, `no_answer`, slice, `high_risk` 값은
deterministic selector나 provider request에 들어가지 않습니다. Gold SHA와 query 순서는
artifact identity를 봉인할 때만 사용합니다.

검색 순위도 사람이 작성한 AnswerInput을 신뢰하지 않습니다. release 입력은 기존
`cardrag.gold-run-artifact.v1`, `cardrag.gold-lane-capture-receipt.v2`, attestation/observation,
raw-score artifact 네 파일에 결속됩니다. `build_answer_input_artifact`가 run의 전체 contract
순위와 상위 K span을 그대로 복제하고 source text만 pinned DB에서 다시 추출합니다. gold label을
본 작성자가 정답 span을 rank 1로 옮기거나 no-answer evidence를 비우면 producer가 projection
불일치로 거부합니다.

provider나 decision artifact가 자유 형식 답변 문장을 전달하는 필드는 없습니다. decision은
retrieval capture에 포함된 span ID, numeric fact, revision ID만 선택합니다. producer가
선택된 source text를 순위 순서대로 결합해 최종 `EvaluatedAnswer.text`를 만듭니다.

- citation은 provider에 실제 제시된 상위 retrieval evidence만 참조할 수 있습니다.
- citation 순서는 retrieval rank 순서여야 합니다.
- revision 목록은 citation이 속한 contract 목록과 정확히 같아야 합니다.
- numeric fact는 citation source에 동일 문자열로 존재해야 하며 출현 순서로 정렬됩니다.
- `no_answer=true`이면 citation, numeric fact, revision을 모두 비워야 합니다.
- 답변 가능한 decision은 citation을 하나 이상 가져야 합니다.
- source/question/decision/output 어디서든 OAuth/API key, bearer/JWT, private-key 등 credential
  token form을 발견하면 provider 호출 또는 publish 전에 중단합니다.

이 구조 때문에 provider paraphrase나 fabricated citation은 artifact 계약으로 표현할 수
없습니다. 내장 `deterministic_extractive`도 질의와 실제 retrieval만 보고 최대 8개 근거를
선택하는 실제 answer profile입니다. 합성 fixture가 아니며 정식 300-query gate에서
`release_eligible=true`가 될 수 있습니다. 검색 결과가 없으면 고정된 abstention 문장을
출력합니다.

## Retrieval capture에서 AnswerInput 만들기

입력은 UTF-8, key-sort, 공백 없는 canonical JSONL이며 마지막 newline이 필수입니다. 첫 줄은
`cardrag.gold-answer-input-artifact.v1`, 이후 줄은 gold 순서와 정확히 같은
`cardrag.gold-answer-input-query.v1`입니다. release manifest에는 다음 값이 모두 필요합니다.

- `retrieval_contract=cardrag.gold-run-ranking-projection.v1`
- `retrieval_capture_phase=bootstrap_retrieval`
- `retrieval_run`: sealed run의 SHA-256과 byte size
- `retrieval_capture_receipt`: lane capture receipt의 SHA-256과 byte size
- `retrieval_attestation_artifact`: native attestation 또는 external observation binding
- `retrieval_raw_score_artifact`: native raw score 또는 external observation binding
- 모든 lane의 `retrieval_corpus_inventory`, `retrieval_dense_score_matrix`,
  `retrieval_query_vector_matrix`: compact v2의 이름 있는 corpus/score sidecar binding
- v1.0.9 전용 `retrieval_lexical_rank_artifact`; native와 qwen page에서는 반드시 null

외부 observation JSONL과 모든 named compact sidecar는 각각 95,000,000 bytes 이하입니다.
corpus inventory가 score matrix의 행 identity/order를 고정하고, dense/query vector는
little-endian float32 row-major matrix입니다. query별 offset/size/count/SHA는
attestation/observation에 연속 구간으로 봉인됩니다. v1.0.9 lexical rank는 query별 canonical
JSONL 구간입니다. 따라서 query마다 전체 corpus row를 반복하던 대형 JSON 증거는 허용하지
않습니다.

각 query record의 `retrieval_ranking_sha256`는 query ID, 전체 contract 순위, 전체 span 순위로
계산한 canonical projection hash입니다. record의 `contracts`는 run의 전체 contract tuple과
exact match여야 합니다. `evidence`는 run span의 앞
`maximum_answer_evidence_spans`개와 `(span_id, contract_revision_id, rank, score)`가 exact
match여야 하며 선택적 생략이나 재정렬을 허용하지 않습니다. `fixture-unbound.v1`은
`release_gate=False`에서만 허용되고 receipt는 release-ineligible로 남습니다.

권장 생성 경로는 public `build_answer_input_artifact(...)`입니다. 이 함수는 gold의 ID/질문만
읽고, run/capture chain과 generation identity를 확인한 뒤 DB에서 source text/hash를 만듭니다.
외부 lane은 검색 순위를 만들기 위해 no-answer bootstrap answer로 observation을 한 번 만들 수
있습니다. bootstrap run의 answer는 이 producer가 읽거나 사용하지 않습니다. bootstrap
`LaneCaptureReceipt.v2`는 반드시 `capture_phase=bootstrap_retrieval`,
`validation_profile=release_grade`, `release_eligible=false`, `answer_evidence=null`이어야 합니다.
`fixture_only` bootstrap이나 bootstrap receipt 자체가 final release를 주장하는 순환 계약은
거부합니다.

최종 native/external capture는 원점수와 corpus에서 새로 계산한 `QueryRunResult` tuple을
`verify_answer_input_ranking(...)`에 전달해야 합니다. 이 검증은 bootstrap run을 신뢰하지 않고
최종 full contract/top-K span 순위 및 projection hash를 다시 대조합니다. 이 단계가 통과한
AnswerInput/producer receipt만 최종 capture receipt에 봉인합니다. 그때만 final lane receipt가
`capture_phase=final_release`, `release_eligible=true`, non-null `answer_evidence`로 승격됩니다.

모든 rank는 1부터 연속이어야 하고 ID는 query 안에서 중복될 수 없습니다. score는 finite
number만 허용합니다. source text hash도 입력에서 즉시 재계산합니다. 이후 lane별로 실제
봉인 DB와 다시 대조합니다.

- `qwen_structure_exact`: `structure_nodes.display_text`와 모든 `node_spans`의 page range/hash를
  재구성해 source text와 byte-equivalent인지 검증합니다. serving DB 전체 v5 schema도
  read-only로 검증합니다.
- `v109_baseline`: historical source commit에 고정하고 v4 `evidence_id`, `document_id`, `text`와
  exact match를 요구합니다. v4 schema/FTS/source range도 검증합니다.
- `qwen_page`: authoritative external producer의 실제 10열
  `evaluation_chunks(row_index, chunk_id, contract_revision_id, span_id, document_id, page,
  source_start, source_end, text, input_sha256)` schema를 exact 검사합니다. 17-key metadata set
  (source commit, parent generation manifest/DB SHA와 size, source-text/column contract 포함),
  1,600/160 chunk policy, row count/profile, deterministic chunk ID,
  source range/길이/text SHA도 모두 재계산합니다.

모든 입력·state·출력 부모 경로는 root부터 component별 dirfd `O_NOFOLLOW`로 순회합니다. state
leaf는 current uid와 mode 0700을 요구하고, 출력 leaf는 current uid이며 group/world writable이
아니어야 합니다. generation DB는 고정한 descriptor를 통해 SQLite `mode=ro&immutable=1`로 열며,
읽기 전후 inode/size/mtime/ctime과 전체 SHA-256을 확인합니다. generation manifest, input,
gold, expected source commit, answer profile 가운데 하나라도 다르면 provider를 호출하지
않습니다.

## Sealed decision JSONL

사람 또는 별도 provider가 citation 선택을 미리 봉인할 때 사용합니다. 첫 줄 manifest는
input/gold/source/generation/profile을 모두 결속하고 `decision_authority`를
`sealed_human` 또는 `sealed_provider`로 명시합니다. `release_eligible`은 자동 추론하지 않고
artifact에 명시해야 합니다. `synthetic`은 항상 `false`입니다.

```json
{"answer_profile_id":"cardrag.answer.extractive-k8.v1","capture_input_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","decision_authority":"sealed_human","generation_id":"GENERATION_ID","generation_manifest_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","gold_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","query_count":300,"release_eligible":true,"schema_version":"cardrag.gold-answer-decision-artifact.v1","source_commit":"dddddddddddddddddddddddddddddddddddddddd","synthetic":false}
{"decision":{"citation_span_ids":["span-1"],"idempotency_key":"answer-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","no_answer":false,"numeric_facts":["10,000원"],"query_id":"gold-001","schema_version":"cardrag.gold-answer-decision.v1","selected_revision_ids":["revision-1"]},"query_id":"gold-001","request_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","schema_version":"cardrag.gold-answer-decision-record.v1"}
```

record는 gold와 같은 순서로 전 query를 정확히 한 번 포함해야 합니다. `request_sha256`와
`idempotency_key`는 producer가 query/retrieval/generation/profile로 계산한 값과 같아야
합니다. 사람이 JSON을 직접 조합하기보다 동일 library의 `build_answer_request` 결과를
사용해 decision packet을 만드는 것이 안전합니다.

## 실행

`cardrag-gold-answer-artifact` console entry point 또는 module로 실행합니다. 아래 deterministic
명령은 live provider를 호출하지 않습니다. 모든 lane은 compact corpus/score sidecar를 각각
경로와 SHA로 전달해야 합니다(`--retrieval-corpus-inventory`,
`--retrieval-dense-score-matrix`, `--retrieval-query-vector-matrix`, v1.0.9의
`--retrieval-lexical-ranks`).

```bash
SOURCE_COMMIT=$(git rev-parse HEAD)
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_answer_artifact \
  --gold /candidate-evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --input /candidate-evidence/qwen-structure-answer-input.jsonl \
  --expected-input-sha256 INPUT_SHA256 \
  --generation-manifest /candidate-generation/manifest.json \
  --database /candidate-generation/GENERATION_ID/index.sqlite3 \
  --retrieval-run /candidate-evidence/bootstrap/qwen-structure.jsonl \
  --expected-retrieval-run-sha256 RUN_SHA256 \
  --retrieval-capture-receipt /candidate-evidence/bootstrap/qwen-structure.capture-receipt.json \
  --expected-retrieval-capture-receipt-sha256 CAPTURE_RECEIPT_SHA256 \
  --retrieval-attestation /candidate-evidence/bootstrap/native-v5-attestation.jsonl \
  --expected-retrieval-attestation-sha256 ATTESTATION_SHA256 \
  --retrieval-raw-score /candidate-evidence/bootstrap/qwen-structure-scores.jsonl \
  --expected-retrieval-raw-score-sha256 RAW_SCORE_SHA256 \
  --retrieval-corpus-inventory /candidate-evidence/bootstrap/qwen-structure-corpus.jsonl \
  --expected-retrieval-corpus-inventory-sha256 CORPUS_SHA256 \
  --retrieval-dense-score-matrix /candidate-evidence/bootstrap/qwen-structure-scores.f32 \
  --expected-retrieval-dense-score-matrix-sha256 SCORE_MATRIX_SHA256 \
  --retrieval-query-vector-matrix /candidate-evidence/bootstrap/qwen-structure-queries.f32 \
  --expected-retrieval-query-vector-matrix-sha256 QUERY_MATRIX_SHA256 \
  --state-dir /candidate-evidence/answer-state/qwen-structure \
  --output /candidate-evidence/qwen-structure-answers.jsonl \
  --ledger /candidate-evidence/qwen-structure-answer-calls.jsonl \
  --receipt /candidate-evidence/qwen-structure-answer-receipt.json \
  --expected-source-commit "$SOURCE_COMMIT" \
  --expected-answer-profile-id cardrag.answer.extractive-k8.v1 \
  --maximum-provider-calls 0 \
  --deterministic-extractive
```

봉인 decision을 사용할 때는 마지막 두 줄 대신 다음을 사용합니다.

```bash
  --maximum-provider-calls 0 \
  --decisions /candidate-evidence/qwen-structure-answer-decisions.jsonl \
  --expected-decisions-sha256 DECISIONS_SHA256
```

`--fixture-mode`는 300-query release gate를 끄며 receipt의 `release_eligible`도 `false`로
봉인합니다. 실제 release에서는 사용하지 않습니다.

## 중단 재개와 call ledger

state directory의 `identity.json`은 decision mode, input/gold/source/generation/database,
profile, retrieval chain, full decision artifact, provider ID, 최대 logical call 수를 create-only로
봉인합니다. query별 shard도 create-only입니다. 완료된 shard는 재실행 때 provider를 다시
호출하지 않습니다. deterministic shard는 현재 request에서 decision을 다시 계산해 exact
대조하고, sealed shard는 현재 decision record와 exact 대조합니다.

provider mode는 호출 전에 query별 logical call reservation을 먼저 봉인합니다. 중단 후 같은
`idempotency_key`로 재시도할 수 있지만 adapter가 provider 측 idempotency를 반드시 보장해야
합니다. `maximum_provider_calls`는 logical reservation의 상한이며 전체 query를 처리할 수 없는
값은 첫 호출 전에 거부합니다. 재개한 provider shard는 logical call index와
reservation/query/request/provider가 모두 exact match여야 합니다. deterministic 및
sealed-decision mode는 reservation을 가질 수 없고 call entry 수가 0입니다.

모든 query가 끝나면 producer는 `state-bundle.jsonl`을 create-only로 발행합니다. 첫 줄은
state identity와 corpus inventory binding, decision mode, query/reservation 수를 담고 이후
정확히 query 수만큼 reservation(해당 시)과 shard를 원래 순서로 포함합니다. receipt가 이
bundle의 SHA-256과 byte size를 직접 봉인하므로 검증기는 가변 state directory나 임의의 추가
파일을 순회하지 않습니다.
각 query에서 AnswerRequest를 AnswerInput과 질문으로 다시 만들고 request/idempotency hash,
deterministic 재계산 또는 sealed decision exact match, provider reservation/provider ID,
decision SHA, `_answer_from_decision` 결과를 최종 AnswerRecord와 정확히 대조합니다. call-ledger
entry도 query/request/idempotency/decision/provider mapping이 bundle과 동일해야 합니다.

최종 publish 순서는 answer JSONL, call-ledger JSONL, receipt JSON입니다. 모두 임시 파일 fsync 후
hard-link create-only로 발행합니다. 동일 경로 재실행은 byte-identical일 때만 성공합니다.
receipt는 기존 answer manifest에 없는 source commit을 포함해 다음을 한 번에 봉인합니다.

- gold/input/generation manifest/serving DB/answer/call-ledger/state identity/state bundle
  SHA-256과 byte size
- retrieval run/capture receipt/attestation/raw-score와 lane별 compact named sidecar 전체 binding
- sealed mode의 full decision artifact binding, 모든 mode의 full provider ID와 call limit
- source commit, generation ID, lane, answer profile, query count
- decision mode, release eligibility, synthetic=false, logical provider call count

answer artifact와 이 receipt/call ledger를 함께 보존해야 source-commit 결속과 zero-call 증거를
재현할 수 있습니다.

최종 봉인 단계에서는 `load_answer_producer_receipt(...)` 또는 더 강한
`verify_answer_producer_receipt(...)`를 사용합니다. 후자는 canonical receipt뿐 아니라 gold,
AnswerInput, generation DB, bootstrap run/capture/attestation/raw-score/named sidecar, answer, ledger,
state identity/state bundle, 선택적 full decision artifact를 다시 읽어 모든 expected
SHA/source/generation/lane/profile을 exact 검사합니다. release mode에서 bootstrap evidence 중
하나라도 없거나 bootstrap receipt가 `release_grade`가 아니면 fail closed입니다.

GitHub release checkout에는 수백 MB~GB generation DB/vector를 넣지 않습니다. issuance/finalize에서
위 full verifier가 통과한 뒤에는 `verify_answer_producer_receipt_portable(...)`을 사용합니다. 이
검증기는 gold, AnswerInput, answer, call ledger, state identity/bundle, receipt, compact corpus
inventory, 선택적 decision만 읽습니다. DB/run/score matrix의 binding은
AnswerInput↔identity↔state bundle↔receipt 사이에서 exact 대조하고, 모든
request/decision/answer/ledger를 다시 재생합니다. 따라서 대형 source artifact 없이도 release
validate-set이 semantic provenance를 확인하지만, portable 검증이 issuance의 full source/ranking
검증을 대체하지는 않습니다.

portable checkout에 포함되는 gold, AnswerInput, answer, call ledger, state identity, state bundle,
receipt, corpus inventory, 선택적 decision은 각 파일이 95,000,000 bytes 이하여야 합니다.
receipt(32 MiB)와 state identity(2 MiB)는 더 엄격한 상한을 적용하며, producer는 publish 전에,
verifier는 파일을 읽기 전에 크기를 거부하므로 GitHub의 파일별 100 MB 제한을 넘는 증거를
만들거나 메모리에 적재하지 않습니다.
