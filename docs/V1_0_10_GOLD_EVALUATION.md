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
{"candidate_position":"left","factual_completeness_preference":"left","left_answer_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","naturalness_preference":"tie","pair_id":"pair-0001","query_id":"gold-001","rater_key":"anonymous-rater-01","right_answer_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","schema_version":"cardrag.blind-pairwise-rating.v1"}
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
release workflow의 `acceptance_report_sha256` 입력으로 전달합니다. 정식 evidence
directory에는 `gold.jsonl`, 다섯 `<lane>.jsonl`, `blind-evaluation.jsonl`, report와 각 run
manifest가 참조한 원본 generation manifest를
`generation-manifests/<sha256>.json`으로 함께 둡니다. workflow는 offline evaluator로
report를 처음부터 재계산해 canonical bytes가 정확히 같은지 확인하고 report input hash,
gold/run/blind file hash, answer hash, generation manifest hash를 모두 다시 검증합니다.
어느 파일도 없거나 hash/schema/canonical encoding/gate가 다르면 publish 전에
중단합니다. 합성 테스트 fixture를 이 경로에 복사해 release 증거로 사용해서는 안 됩니다.

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
