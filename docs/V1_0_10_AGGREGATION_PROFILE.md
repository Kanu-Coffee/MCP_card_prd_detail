# CardRAG v1.0.10 document aggregation profile

v1.0.10의 문서 순위는 임의의 런타임 상수로 선택하지 않습니다. 동일한 300~500개
release gold 질의에서 모든 활성 embedding row를 exact 채점한 immutable score artifact를
입력으로 받아 아래 세 정책을 다시 계산하고, paired bootstrap 95% CI로 유일하게 지지되는
정책만 profile로 봉인합니다.

- `max_child`: `CONTRACT`를 제외한 모든 row score의 최댓값
- `top3_mean`: `CONTRACT`를 제외한 상위 `min(3, available)` row score의 평균
- `contract_plus_child`: 단일 `CONTRACT` row score와 `max_child`를 각각 0.5 가중

동점은 `contract_revision_id`의 bytewise 오름차순으로 결정합니다. 이 tie-break는 결과를
재현하기 위한 것일 뿐 통계적 동점을 winner로 바꾸지 않습니다.

## Sealed score input

`document-aggregation-scores.jsonl`은 UTF-8 canonical JSONL입니다. 첫 줄은
`cardrag.document-aggregation-score-artifact.v1` manifest이고 다음 record는 gold 파일 순서와
동일하게 질의 coverage record 하나, 그 질의의 row score record 전부가 이어집니다.

Manifest는 다음 항목에 결속됩니다.

- gold SHA-256과 query/row count
- source commit, generation ID, generation manifest SHA-256
- serving database SHA-256, vector sidecar SHA-256, 비순환 `exact_row_corpus_sha256`
- Qwen embedding profile ID, `qwen/qwen3-embedding-8b`, 4,096D
- `exact=true`, `approximate=false`, `temporal_scope_policy=gold-query.v1`
- 점수 생성 당시 runtime aggregation status/policy/sealed profile SHA-256

질의 coverage record는 `query_id`, 질문 원문의 SHA-256, 실제 little-endian float32
query vector SHA-256, `expected_rows`, `scored_rows`, `active_contracts`를 기록합니다.
`expected_rows != scored_rows`이면 즉시 실패합니다. 각
`cardrag.document-aggregation-row-score.v1` record는 다음을 포함합니다.

- 질의 내 연속 `ordinal`과 엄격히 증가하는 sidecar `row_index`
- `contract_revision_id`, `node_id`, `view_type`
- embedding input SHA-256과 embedding profile ID
- 정규화된 exact inner-product score (`-1.0..1.0`의 JSON float)

각 질의에서 `(contract_revision_id,node_id,view_type)`와 `row_index`는 중복될 수 없고, 모든
계약에는 단일 `CONTRACT` row와 하나 이상의 non-`CONTRACT` row가 있어야 합니다. bool,
NaN/Infinity, duplicate JSON key, 비canonical encoding, symlink/non-regular 입력은 거부합니다.
입력은 최대 4 GiB까지 single-pass로 읽고 읽기 전후 inode/size/mtime/ctime 결속을 확인하며,
메모리에는 계약별 상위 child 점수만 유지합니다.

## Selection and fail-closed gate

선택 objective는 contract `nDCG@10`입니다. 각 정책의 contract Recall@10, nDCG@10,
MRR@10과 모든 gold slice를 계산하고, 모든 ordered policy pair에 deterministic PCG64 paired
percentile bootstrap CI95를 기록합니다. 한 정책의 nDCG@10 delta CI95 하한이 다른 두
정책 모두에 대해 `> 0`일 때만 unique winner입니다.

release mode는 추가로 다음을 요구합니다.

- 기존 gold evaluator와 동일한 300~500개 및 필수 slice/high-risk 계약
- 최소 2,000 bootstrap samples
- `no_answer`를 제외한 모든 필수 slice에 retrieval label 존재
- winner가 `max_child`가 아니면 전체 및 모든 필수 retrieval slice에서 Recall@10,
  nDCG@10, MRR@10 delta CI95 하한이 0 이상
- generation manifest artifact의 실제 file SHA-256 일치

unique winner가 없거나 회귀를 배제할 수 없으면 artifact는 생성되지만
`release_gate.status=failed`, `sealed_profile=null`입니다. fixture mode는 통계 계산만 허용하고
항상 `status=not_evaluated`, `sealed_profile=null`입니다. 따라서 합성 fixture가 runtime
profile이나 release 증거가 될 수 없습니다.

통과한 artifact의 `sealed_profile`은 선택 정책 정의, embedding profile, 평가 generation(M0),
gold, score artifact, bootstrap 계약과 `exact_row_corpus_sha256`을 포함합니다.
`sealed_profile_sha256`은 그 canonical object의 SHA-256이고, release workflow는 전체 profile
file SHA-256을 별도 dispatch 입력으로 요구한 뒤 모든 입력에서 profile을 재계산하여
byte-for-byte 비교합니다. 최종 serving generation(M1)은 이 profile object/hash 및 동일한
`exact_row_corpus_sha256`을 manifest, DB metadata, retrieval-policy SHA-256에 함께 결속합니다.
M0 manifest SHA가 profile에 들어가고 M1이 profile hash를 담는 2단계 구조라 상호 hash 순환은
발생하지 않습니다.

## Generate and validate

먼저 실제 candidate의 모든 current/ambiguous row를 런타임과 동일한 exact scorer로 캡처합니다.
query별 immutable shard, canonical progress hash chain, generation/DB/vector/exact-row identity가
중단 재개와 변조 검출에 사용됩니다. 기본 모드는 release gold 계약을 요구하며, 테스트에서만
`--fixture-mode`를 사용할 수 있습니다.

```bash
uv run cardrag-aggregation-capture \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --generation-manifest /candidate/generation.json \
  --generation-dir /candidate/generations/GENERATION_ID \
  --object-root /candidate/objects \
  --output release-evidence/v1.0.10/document-aggregation-scores.jsonl \
  --state-dir /candidate/aggregation-capture-state \
  --source-commit "$(git rev-parse HEAD)" \
  --openrouter-api-key-file /run/secrets/openrouter_api_key
```

정식 score artifact와 gold가 준비된 뒤 profile을 생성합니다.

```bash
GOLD_SHA256=$(sha256sum release-evidence/v1.0.10/gold.jsonl | awk '{print $1}')
uv run python -m cardrag_mcp.aggregation_profile \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --scores release-evidence/v1.0.10/document-aggregation-scores.jsonl \
  --expected-gold-sha256 "$GOLD_SHA256" \
  --generation-manifest-dir release-evidence/v1.0.10/generation-manifests \
  --bootstrap-samples 2000 \
  --bootstrap-seed 1010 \
  > release-evidence/v1.0.10/document-aggregation-profile.json
```

## Build the sealed M1 candidate

M0가 candidate channel의 현재 head인 동안, 통과 artifact를 Worker에 명시적으로 주입하여 M1을
생성합니다. 두 환경 변수를 모두 생략하면 기존 M0 `candidate_default/max_child` 계약은 byte-level
contract payload까지 그대로 유지됩니다. 둘 중 하나만 설정하거나 상대 경로, symlink/non-regular
file, noncanonical JSON, 전체 file SHA-256 불일치, `release_gate.status != passed`, M0가 아닌 score
runtime identity는 Worker 시작 전에 거부됩니다.

Worker는 candidate state directory/DB 생성과 credentialed tokenizer·Qwen provider preflight보다 먼저
profile의 평가 generation ID/manifest SHA-256이 현재 candidate M0와 정확히 일치하는지 GET-only로
확인합니다. Provider contract가 준비된 뒤 run row 생성/cleanup 전에 전체 M1 contract를 재결속하고,
publication predecessor를 정하기 직전에 다시 확인하여 TOCTOU를 닫습니다. M1 contract에는 전체
profile artifact SHA-256을, serving DB에는 그
artifact SHA-256과 selected policy/profile SHA-256/exact-row corpus를, core generation manifest에는
profile object/profile SHA-256/policy/exact-row corpus와 sealed retrieval-policy SHA-256을 결속합니다.
Exporter가 M0와 동일한 `exact_row_corpus_sha256`을 재계산하지 못하면 publication 전에 실패합니다.

아래 override는 profile file 하나만 container에 read-only로 mount합니다. M0 Worker가 terminal이고
candidate 전용 volume/channel을 사용 중인지 먼저 확인해야 하며, 이 명령을 stable project/channel에
사용하면 안 됩니다.

```bash
PROFILE_FILE="$PWD/release-evidence/v1.0.10/document-aggregation-profile.json"
test -f "$PROFILE_FILE" || exit 1
test ! -L "$PROFILE_FILE" || exit 1
export CARDRAG_DOCUMENT_AGGREGATION_PROFILE_HOST_FILE="$PROFILE_FILE"
export CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256
CARDRAG_DOCUMENT_AGGREGATION_PROFILE_ARTIFACT_SHA256=$(sha256sum "$PROFILE_FILE" | awk '{print $1}')

docker compose \
  -p cardrag-v110-candidate \
  -f deploy/worker/compose.yaml \
  -f deploy/worker/compose.candidate.yaml \
  -f deploy/worker/compose.secrets.yaml \
  -f deploy/worker/compose.aggregation-profile.yaml \
  run --rm worker
```

이 runtime loader는 이미 별도 검증된 artifact를 안전하게 소비하는 경계입니다. 통계 결과를 다시
계산하는 release authority는 아래 offline validation command이며, M1 생성 후 export한 canonical
serving generation manifest를 함께 제공해야 합니다.

Release와 동일한 offline 재검증은 다음과 같습니다.

```bash
PROFILE_SHA256=$(sha256sum \
  release-evidence/v1.0.10/document-aggregation-profile.json | awk '{print $1}')
uv run python -m cardrag_mcp.aggregation_profile \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --scores release-evidence/v1.0.10/document-aggregation-scores.jsonl \
  --validate-profile release-evidence/v1.0.10/document-aggregation-profile.json \
  --expected-profile-sha256 "$PROFILE_SHA256" \
  --generation-manifest-dir release-evidence/v1.0.10/generation-manifests \
  --serving-generation-manifest \
    release-evidence/v1.0.10/serving-generation-manifest.json \
  --bootstrap-samples 2000 \
  --bootstrap-seed 1010
```

현재 repository에는 실제 300~500개 gold, full candidate row-score artifact, 통과 profile이
의도적으로 포함되어 있지 않습니다. 최종 selected serving generation manifest도 없습니다.
따라서 release workflow는 이 네 artifact가 실제 candidate 검증에서 봉인되기 전까지
fail-closed합니다. winner가 없는 candidate runtime은 명시적인 unsealed `max_child`
(CONTRACT 제외)만 사용하며, 이를 통계적으로 선택된 profile이라고 간주하지 않습니다.
