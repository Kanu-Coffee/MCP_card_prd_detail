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

점수 증거는 Git/GitHub에 올릴 수 있는 네 개의 독립 파일로 봉인됩니다. 각 파일의 hard cap은
정확히 95,000,000 bytes입니다.

- `document-aggregation-scores.jsonl`: v2 manifest 한 줄과 gold 순서의 query coverage 300~500줄
- `document-aggregation-corpus-inventory.jsonl`: 고정 코퍼스 행 provenance 한 벌
- `document-aggregation-score-matrix.f32`: query-major/row-major little-endian float32 점수 행렬
- `document-aggregation-query-vectors.f32`: query-major 4,096D little-endian float32 벡터 행렬

첫 파일의 schema는 `cardrag.document-aggregation-score-artifact.v2`입니다. v1의 질의별 전체
row JSON 반복은 release와 fixture 모두에서 읽지 않습니다. `score_count`는
`query_count * corpus_row_count`와 같고 최대 20,000,000이므로 score matrix는 최대
80,000,000 bytes입니다. query vector matrix와 inventory도 별도 cap을 넘을 수 없습니다.
캡처는 이 크기를 provider 호출 전에 산술적으로 예측합니다.

Manifest는 다음 항목에 결속됩니다.

- gold SHA-256과 query/corpus-row/score count
- source commit, generation ID, generation manifest SHA-256
- serving database SHA-256, vector sidecar SHA-256, 비순환 `exact_row_corpus_sha256`
- Qwen embedding profile ID, `qwen/qwen3-embedding-8b`, 4,096D
- `exact=true`, `approximate=false`, `temporal_scope_policy=gold-query.v1`
- 점수 생성 당시 runtime aggregation status/policy/sealed profile SHA-256
- inventory, score matrix, query-vector matrix의 정확한 SHA-256/size와
  `little-endian`/`float32`/`row-major` literal
- 실제 release 캡처인지 합성 fixture인지 구분하는 `validation_profile`

profile 생성과 release 재검증은 `--expected-source-commit`을 필수 trust input으로 받아 score
manifest의 source commit과 비교합니다. release workflow는 승인된 40자리
`candidate_source_commit`을 전달하므로 다른 candidate commit에서 캡처한 점수는 hash와 내부
결속이 모두 유효해도 재사용할 수 없습니다. tag commit은 이 candidate의 evidence-only
descendant여야 하며 허용 diff는 `release-evidence/v1.0.10/` 아래로 제한됩니다.

`cardrag.document-aggregation-corpus-inventory.v1` manifest 뒤에는
`cardrag.document-aggregation-corpus-row.v1`이 정확한 matrix column 순서로 옵니다. 각 행은 연속
`ordinal`, 엄격히 증가하는 v5 `row_index`, `contract_revision_id`, `node_id`, `view_type`, embedding
input SHA-256/profile ID를 가집니다. 중복 view는 거부하며 모든 계약에는 단일 `CONTRACT`와 하나
이상의 child가 있어야 합니다.

각 `cardrag.document-aggregation-query-coverage.v2` record는 연속 query `ordinal`, 질문 SHA-256,
active contract/row count와 두 binary segment의 연속 offset, size, count, SHA-256을 기록합니다.
점수 segment는 inventory와 같은 수의 유한한 `[-1,1]` float32이고 query vector는 정확히 4,096개의
유한한 정규화 float32입니다. segment 사이 hole, overlap, swap, truncation, trailing byte, 1-ULP
변조도 전체 파일 hash 또는 segment hash에서 실패합니다.

검증기는 binary 전체를 메모리에 복사하지 않고 O_NOFOLLOW로 hash-pin한 mmap segment를 순회합니다.
JSON duplicate key, 비canonical encoding, symlink/non-regular/교체·변경 입력도 거부합니다. profile은
작은 query coverage와 inventory만 메모리에 유지하고 정책 metric은 각 score segment에서 다시
계산합니다. release mode는 `validation_profile=release_grade`만 허용합니다.

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
`--fixture-mode`를 사용할 수 있습니다. state에는 inventory와 query별
`coverage.json`/`scores.f32`/`query-vector.f32` 3종 세트를 0600 immutable 파일로 저장합니다.
유효한 완료 세트는 progress pointer가 유실돼도 provider를 다시 호출하지 않습니다. 일부만 남은
세트, orphan, ordinal skip은 자동 추측하지 않고 실패시킵니다.

실제 scorer는 `capture_unscoped_current_score_stream`으로 기본 512행(허용 상한 4,096행)씩
little-endian float32 score를 state shard에 직접 기록합니다. 따라서 기존 호환 API처럼 전체 row
tuple과 raw-score dictionary를 동시에 만들지 않으며, query embedding provider 호출은 질의당 한
번뿐입니다. 이미 완료·검증된 3종 세트는 resume에서 provider를 전혀 호출하지 않습니다.

캡처 시작 시 generation DB와 vector sidecar를 SHA-256 및
`dev/ino/size/mtime/ctime`으로 checkpoint합니다. handle load, inventory, 각 질의 전후에는 inode
identity를 확인하고 최종 네 파일을 publish·재검증한 뒤 두 generation 입력을 다시 전부 hash합니다.
동일 byte로 atomic path replacement를 하더라도 inode/ctime 차이로 실패하므로 manifest의 DB/vector
hash가 실제 score 계산에 사용된 generation과 분리될 수 없습니다. OpenRouter key를 사용하는 CLI는
secret을 읽기 전에 base URL을 공식 `https://openrouter.ai/api/v1`로 제한합니다.

```bash
SOURCE_COMMIT=$(git rev-parse HEAD)
uv run cardrag-aggregation-capture \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --generation-manifest /candidate/generation.json \
  --generation-dir /candidate/generations/GENERATION_ID \
  --object-root /candidate/objects \
  --output release-evidence/v1.0.10/document-aggregation-scores.jsonl \
  --corpus-inventory-output \
    release-evidence/v1.0.10/document-aggregation-corpus-inventory.jsonl \
  --score-matrix-output \
    release-evidence/v1.0.10/document-aggregation-score-matrix.f32 \
  --query-vector-matrix-output \
    release-evidence/v1.0.10/document-aggregation-query-vectors.f32 \
  --state-dir /candidate/aggregation-capture-state \
  --source-commit "$SOURCE_COMMIT" \
  --openrouter-api-key-file /run/secrets/openrouter_api_key
```

정식 score artifact와 gold가 준비된 뒤 profile을 생성합니다.

```bash
GOLD_SHA256=$(sha256sum release-evidence/v1.0.10/gold.jsonl | awk '{print $1}')
uv run python -m cardrag_mcp.aggregation_profile \
  --gold release-evidence/v1.0.10/gold.jsonl \
  --scores release-evidence/v1.0.10/document-aggregation-scores.jsonl \
  --corpus-inventory \
    release-evidence/v1.0.10/document-aggregation-corpus-inventory.jsonl \
  --score-matrix release-evidence/v1.0.10/document-aggregation-score-matrix.f32 \
  --query-vector-matrix \
    release-evidence/v1.0.10/document-aggregation-query-vectors.f32 \
  --expected-gold-sha256 "$GOLD_SHA256" \
  --expected-source-commit "$SOURCE_COMMIT" \
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
  --corpus-inventory \
    release-evidence/v1.0.10/document-aggregation-corpus-inventory.jsonl \
  --score-matrix release-evidence/v1.0.10/document-aggregation-score-matrix.f32 \
  --query-vector-matrix \
    release-evidence/v1.0.10/document-aggregation-query-vectors.f32 \
  --validate-profile release-evidence/v1.0.10/document-aggregation-profile.json \
  --expected-profile-sha256 "$PROFILE_SHA256" \
  --expected-source-commit "$SOURCE_COMMIT" \
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
