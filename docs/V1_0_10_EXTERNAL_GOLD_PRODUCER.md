# v1.0.10 external gold producer

`cardrag_mcp.external_gold_producer`는 기존 `gold_capture`가 검증하는 두 external lane의
source artifact를 만드는 offline-first 도구다.

- `v109_baseline`: 보존된 v1.0.9 generation v4 DB와 그 당시의 query embedding 계약
- `qwen_page`: 후보 generation v5의 현재 문서 페이지를 정확한 1,600자 window로 다시
  구성한 평가 전용 DB, vector sidecar, inventory, observation

이 모듈은 production generation pointer나 WebDAV를 읽거나 바꾸는 기능이 없다. 출력은
호출자가 지정한 로컬 경로에만 create-only로 발행한다. 같은 경로의 byte-identical 결과는
재사용하고, 한 byte라도 다르면 중단한다.

## 공통 artifact 계약

JSON과 JSONL은 UTF-8 canonical JSON(key 정렬, 불필요한 공백 없음)이다. JSONL은 마지막
newline까지 계약에 포함된다. manifest, DB, vector, inventory, provider receipt, raw response,
gold, answer artifact는 SHA-256과 byte size로 서로 결합된다. 다음 항목 중 하나라도 다르면
출력하지 않는다.

- source commit, generation ID/schema, DB SHA-256/size/metadata
- query/document input의 개수, 순서, ID, formatted-input SHA-256
- vector 개수, 순서, 차원, finite 값, L2 normalization, little-endian float32 hash
- page source span, 원문 hash, page coverage와 declared page count
- provider/model/provider route, raw response artifact set과 receipt

provider raw body는 canonical envelope에 base64로 보존한다. receipt는 official base URL,
credential-free canonical request body SHA-256, 연속 input ID, immutable reservation ID,
raw response 파일 이름/SHA-256/size와 전체 request/response-set hash를 결합한다. loader는
expected formatted input으로 request body를 재구성하고, raw body의 duplicate key/non-finite
값/model/provider/count/index/vector를 다시 검증·정규화한 byte가 replay vector와 정확히
같은지 확인한다. 따라서 self-asserted receipt나 임의 JSON response는 유효하지 않으며 replay,
receipt, 모든 raw envelope를 함께 보존해야 한다.

## 보존된 v1.0.9 source

release-eligible historical source는 Docker volume `cardrag-worker-v109-state`의 성공 run
`2208f0c6076649c4be915be182422b6a`에 보존되어 있다. source는 nested Worker seal
`runs/2208f0c6076649c4be915be182422b6a/sealed/publish.json`의 `manifest`이거나, 그 manifest를
canonical standalone JSON으로 추출한 파일일 수 있다. 확인된 generation은
`g-2208f0c6076649c4be915be1-d11f80f9af71`이다.

release gate의 trust anchor는 다음 exact 값이다.

- publish wrapper: SHA-256
  `83ff730f7972ccc8cafb2be4bf8b82d7c65236c531244b722ccba2a5d5225ffa`, 958,668 bytes
- extracted canonical manifest: SHA-256
  `dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002`, 542,209 bytes
- serving DB: SHA-256
  `d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f`, 58,466,304 bytes

manifest가 없거나 DB의 hash/size/metadata가 다르면 v1.0.9를 재현할 수 없다. 현재 v5 DB나
임의 provider/profile로 대체하지 않는다. 보존 source가 있으므로 현재 historical-data
blocker는 없지만, 실행 환경에서 volume을 먼저 read-only로 노출해야 한다.
`v109-live-replay`의 release gate는 위 manifest/DB SHA-256과 size anchor를 SQLite open이나
metadata row materialization보다 먼저 검사한다. 따라서 hostile manifest가 거대한 metadata를
가리키더라도 preserved anchor mismatch가 DB 접근보다 먼저 실패한다. fixture-only 경로도
metadata count/type/byte-size를 bounded aggregate로 확인한 뒤 dict를 만든다.

standalone manifest와 corpus inventory는 다음처럼 만든다.

```bash
uv run python -m cardrag_mcp.external_gold_producer v109-inventory \
  --generation-manifest-source /v109-state/runs/2208f0c6076649c4be915be182422b6a/sealed/publish.json \
  --generation-manifest-output /evidence/v109/generation-manifest.json \
  --database /evidence/v109/index.sqlite3 \
  --inventory-output /evidence/v109/inventory.jsonl \
  --expected-run-id 2208f0c6076649c4be915be182422b6a \
  --expected-generation-id g-2208f0c6076649c4be915be1-d11f80f9af71 \
  --expected-publish-sha256 83ff730f7972ccc8cafb2be4bf8b82d7c65236c531244b722ccba2a5d5225ffa \
  --expected-manifest-sha256 dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002 \
  --expected-database-sha256 d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f
```

v1.0.9 query vector는 두 방식만 허용한다.

1. 동일 historical 계약으로 이미 봉인한 replay/receipt/raw response를 offline으로 사용한다.
2. `v109-live-replay`로 당시 HTTP semantics를 정확히 재실행한다.

live path는 query마다 한 번씩 `openai/text-embedding-3-small`, 1,536차원,
`cardrag.embedding-input.v1` prefix를 전송한다. historical code와 같이 provider pinning을
추가하지 않으며, 현재 Qwen route를 대신 사용하지 않는다.

```bash
uv run python -m cardrag_mcp.external_gold_producer v109-live-replay \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --generation-manifest /evidence/v109/generation-manifest.json \
  --database /evidence/v109/index.sqlite3 \
  --openrouter-api-key-file /run/secrets/openrouter-api-key \
  --provider-receipt-output /evidence/v109/query-provider-receipt.json \
  --replay-output /evidence/v109/query-replay.jsonl \
  --state-dir /evidence/v109/query-state \
  --expected-run-id 2208f0c6076649c4be915be182422b6a \
  --expected-generation-id g-2208f0c6076649c4be915be1-d11f80f9af71 \
  --expected-manifest-sha256 dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002 \
  --expected-database-sha256 d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f
```

## Qwen 1,600자 page corpus의 정확한 계약

source는 `cardrag.generation.v5` manifest와 그 manifest가 hash/size로 가리키는
`cardrag.serving-db.v5` DB다. `temporal_status='current'` revision의 모든 page가 1부터
declared `page_count`까지 빠짐없이 있어야 한다. source manifest의 primary embedding
profile은 요청한 profile과 정확히 같아야 하며 다음 계약을 만족해야 한다.

- model `qwen/qwen3-embedding-8b`, dimension 4096, dtype float32, L2 normalization
- provider `openrouter`, provider ID `deepinfra` 또는 `nebius`, fallback forbidden
- query policy `cardrag.qwen3-query.v1`, truncation `error`, 정확한 maximum token 수
- pinned tokenizer revision `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`
- tokenizer file SHA-256
  `83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d`,
  size 11,422,947 bytes

page chunking은 추정 알고리즘이 아니다. v1.0.9 commit의
`apps/cardrag-worker/src/cardrag_worker/pipeline.py::chunk_pages`를 다음 값으로 고정한
`cardrag.page-window-1600.v1` 계약이다.

- 최대 Python character 수 1,600, overlap 160
- 1,600자 이전의 마지막 newline/space가 window 절반 뒤에 있으면 그 boundary를 사용
- 양 끝 whitespace를 제거한 뒤 source start/end를 원문 character offset으로 보존
- `chunk_id = "evidence_" + canonical_sha256(document_id, page, source_start,
  source_end, text_sha256)`

평가 DB schema `cardrag.evaluation-page.v1`의 authoritative table은 다음 **10열**이다.
5열 축약 schema는 호환 계약이 아니다.

| 순서 | `evaluation_chunks` 열 | 계약 |
| ---: | --- | --- |
| 1 | `row_index` | 0부터 연속인 vector/inventory row index |
| 2 | `chunk_id` | 위 canonical identity, unique |
| 3 | `contract_revision_id` | source current revision |
| 4 | `span_id` | 항상 `chunk_id`와 동일, unique |
| 5 | `document_id` | source document |
| 6 | `page` | 1 이상인 source page |
| 7 | `source_start` | Python character 시작 offset |
| 8 | `source_end` | exclusive 끝 offset, start보다 큼 |
| 9 | `text` | source page의 정확한 slice |
| 10 | `input_sha256` | `sha256(text.encode("utf-8"))` |

metadata는 다음 17개 key만 정확히 가진다. 중복 commit이나 host-local source path는 넣지
않는다.

- `schema_id`, `generation_id`, `source_commit`, `source_generation_id`
- `source_generation_manifest_sha256`, `source_generation_manifest_size_bytes`
- `source_serving_database_sha256`, `source_serving_database_size_bytes`
- `embedding_model`, `embedding_dimension`, `embedding_profile_id`
- `chunking_policy`, `maximum_chars`, `overlap_chars`
- `source_text_contract`, `column_contract`, `row_count`

inventory의 각 row는 위 `input_sha256`과 함께 대응 vector의
`sha256(vector.astype("<f4").tobytes())`를 봉인한다. `vectors.f32`는 row-index 순서의
4096차원 little-endian float32 row-major matrix다. page DB와 corpus vector는 local
authoritative validation input이지 Git-portable evidence sidecar가 아니다. 따라서 각각 기존
4 GiB/64 GiB 안전 상한을 사용하며 95,000,000-byte Git 상한을 적용하지 않는다. 예를 들어
5,799-row corpus vector는 95,010,816 bytes여도 유효하다. release evidence는 이 파일의
SHA-256/size binding을 보존하고 validator가 로컬 원본을 재검증하지만, 파일 자체를 Git에
복사하거나 commit하지 않는다. inventory와 manifest는 Git-portable이므로 95,000,000-byte
상한을 유지한다.

document input을 provider 호출 전에 먼저 봉인한다.

```bash
uv run python -m cardrag_mcp.external_gold_producer qwen-page-inputs \
  --source-generation-manifest /candidate/manifest.json \
  --source-database /candidate/index.sqlite3 \
  --source-commit SOURCE_COMMIT \
  --embedding-profile-id PROFILE_ID \
  --provider-id deepinfra \
  --maximum-tokens 8192 \
  --tokenizer /evidence/tokenizer.json \
  --output /evidence/qwen/page-inputs.jsonl
```

`qwen-page-inputs`는 tokenizer를 열거나 output temporary file을 만들기 전에 source DB의 current
revision/page count, UTF-8 text byte 합계, 단일 page 최대 byte를 SQL aggregate로 읽는다. 전체 text
335,544,320 bytes, 단일 page 83,886,080 bytes를 넘으면 거부하고, page당 2,048 bytes와 current
revision당 512 bytes의 bookkeeping headroom까지 포함한 1.5 GiB peak를 먼저 확인한다. 따라서
whitespace-only page가 chunk 0개를 만들더라도 매우 많은 page/revision map이 무제한으로 자라지
않는다. 통과한 row만 SQLite cursor로 순차 처리하며 joined page TEXT를 `fetchall()`하지 않는다.
각 page가 chunk를 하나 이상 생성해야 하고, `(chunk_id, revision)`뿐 아니라 전체 source에서
`chunk_id` 자체가 unique해야 한다. 서로 다른 current revision이 같은 document/page/text/range로
같은 ID를 만드는 경우도 tokenizer와 output 이전에 거부한다.

첫 SQL 전에 SQLite `SQLITE_LIMIT_LENGTH`도 단일 source page 상한과 bounded ID overhead로 낮춘다.
metadata는 key/value `typeof='text'`, bounded count/합계/최대 byte를 먼저 검사한다. joined cursor의
revision/document ID는 각각 512 bytes 이하, text SHA는 정확히 64 bytes여야 하며 page/page_count는
INTEGER와 양의 범위를 aggregate에서 확인한다. 이 검사를 통과하기 전에는 metadata dict나 selected
row를 Python object로 만들지 않으므로, text는 작지만 다른 column 하나가 매우 큰 DB도 fail-closed다.

page input의 미래 replay/matrix 1.5 GiB forecast와 actual ID/batch를 사용한 provider receipt의
32 MiB canonical upper bound도 tokenizer 이전에 계산한다. 이후 manifest와 각 Pydantic row를
temporary JSONL에 한 줄씩 canonical publish하므로 formatted-input tuple, 전체 Pydantic row list,
전체 JSONL payload 사본을 동시에 만들지 않는다. `qwen-page-corpus`가 replay를 읽을 때는 source
`PageChunk` text/object의 retained resident estimate를 streaming loader forecast에 함께 전달해
matrix/response/headroom과의 합성 peak도 같은 1.5 GiB 경계를 넘지 않게 한다.

provider capture가 필요한 경우에만 live 명령을 실행한다. route는 manifest가 봉인한 provider
하나만 허용하며 fallback은 금지한다.

```bash
uv run python -m cardrag_mcp.external_gold_producer qwen-live-replay \
  --input /evidence/qwen/page-inputs.jsonl \
  --openrouter-api-key-file /run/secrets/openrouter-api-key \
  --provider-receipt-output /evidence/qwen/page-provider-receipt.json \
  --replay-output /evidence/qwen/page-replay.jsonl \
  --state-dir /evidence/qwen/provider-state \
  --tokenizer /evidence/tokenizer.json
```

Qwen live capture는 **RAM 8 GiB 이상인 단일 process 운영 host**를 기준으로 deterministic
working-set 상한을 1.5 GiB로 고정한다. 입력 manifest를 한 줄 읽은 직후, secret 파일·tokenizer·HTTP
client·provider call·state directory보다 먼저 다음 값을 계산한다.

두 live capture의 `--maximum-response-bytes`는 bool/float가 아닌 exact integer
`1,024..67,108,864`, `--timeout-seconds`는 bool이 아닌 finite number `0 < value <= 3,600`이어야
한다. Qwen의 `--batch-size`도 exact integer `1..128`이다. public Python API에서도 NaN, ±infinity,
fractional byte/batch, bool, 0, 음수, 상한 초과를 같은 방식으로 먼저 거부하며, response streaming
helper가 이 byte 상한을 다시 검증한다.

- `record_count * 4096 * 4` replay matrix bytes
- 각 16,384-byte vector의 canonical base64 길이와 실제 input ID/ordinal을 포함한 JSONL bytes
- canonical input artifact resident estimate, 최대 raw-response envelope와 batch matrix
- actual input ID/batch/raw-envelope upper bound로 계산한 canonical provider receipt bytes
- Python/runtime 여유분을 포함한 최대 working set

예상 replay가 local 64 GiB file 상한을 넘거나 working set이 1.5 GiB를 넘으면
`qwen_provider_capture_resource_limit_exceeded` 등으로 provider 호출 수 0에서 중단한다. 확인된
5,799-row corpus는 matrix 95,010,816 bytes, vector base64 본문 126,696,552 bytes이며, 16 MiB
input/기본 64 MiB response 상한을 적용한 보수적 peak forecast도 약 800 MiB라 통과한다. 이
replay는 local source artifact이므로 95,000,000-byte Git evidence 상한의 대상이 아니다. 기본
batch 16에서는 20,000-row capture와 streaming load도 같은 지원 집합 안에 있다. capture는
resident input artifact를, loader는 provider receipt와 최대 canonical replay 한 줄 및 caller가
보유한 page chunk resource만 계산하므로 453 MiB급 replay 파일 전체를 resident input으로 잘못
두 번 계산하지 않는다.

provider receipt는 32 MiB manifest 상한을 provider 호출 뒤에 처음 확인하지 않는다. actual
input ID, batch size, 고정 request/hash/file-name 길이와 최대 response envelope binding으로
canonical receipt upper bound를 tokenizer, secret, HTTP client, state directory, provider call
이전에 계산한다. 상한을 넘는 batch 구성은 `qwen_provider_receipt_size_invalid`로 output 없이
중단한다.

capture는 완료된 raw-response shard를 batch별로 다시 읽어 replay JSONL을 한 줄씩 publish하고,
loader는 replay를 streaming parse하여 단 하나의 bounded matrix만 채운다. provider 검증도 batch별로
비교하며 전체 derived matrix를 concatenate하지 않고, page vector write도 256-row block으로
검증·기록한다. 위 상한을 넘는 더 큰 corpus는 상수를 임의로 올려 실행하지 말고 별도 sharded
replay/vector workflow와 host precondition을 설계한 뒤 새 계약으로 봉인한다.

portable corpus inventory는 open/parse 전에 stat size가 95,000,000 bytes 이하인지 확인하고
canonical JSONL을 한 줄씩 읽으므로 payload/splitlines 사본을 만들지 않는다. local page vector는
inventory row count로 `row_count * 4096 * 4` exact size와 1.5 GiB working-set forecast를 먼저
검증한 뒤, `O_NOFOLLOW` descriptor에서 단일 float32 matrix로 256-row block을 직접 읽는다. 전체
artifact hash, descriptor/path identity, finite/unit norm, inventory의 per-row SHA-256을 모두 확인하며
95,010,816-byte인 5,799-row vector는 Git cap 대상이 아니므로 정상 통과한다.

`observe --lane qwen_page`도 evaluation DB의 TEXT row를 먼저 `fetchall()`하지 않는다. manifest와
inventory의 exact row count 및 vector-size/1.5 GiB forecast를 먼저 확인하고, 17-key metadata와
`count(*)`, UTF-8 text byte 합계/최대값을 SQL aggregate로 검증한다. 각 text는 1,600 Python
character의 최대 UTF-8 크기인 6,400 bytes 이하여야 한다. 이 preflight를 통과한 뒤에만
`ORDER BY row_index` cursor로 ordinal/span/chunk/hash/range/coverage를 한 row씩 검증하고, aggregate
값을 다시 대조한 후 page vector matrix를 할당한다. 따라서 sparse row, oversized TEXT, 과도한
row count는 vector allocation 이전에 중단한다. exact 58,466,304-byte release anchor를 가진
v1.0.9 DB의 기존 bounded read 경로는 이 qwen-page cursor 계약과 별개다.
이 경로도 첫 SELECT 전에 SQLite single-value limit를 64 KiB로 낮추고, metadata와 모든 selected
column의 storage type/count/합계/최대 byte를 aggregate로 확인한다. chunk/span ID는 정확히 73
bytes, input SHA는 64 bytes, revision/document ID는 512 bytes 이하이고 row/page/source offset은
bounded INTEGER여야 한다. 따라서 비정상적으로 큰 non-TEXT-contract field도 cursor 또는 vector
loader에 도달하지 않는다.

봉인 replay로 page corpus를 만든다.

```bash
uv run python -m cardrag_mcp.external_gold_producer qwen-page-corpus \
  --source-generation-manifest /candidate/manifest.json \
  --source-database /candidate/index.sqlite3 \
  --source-commit SOURCE_COMMIT \
  --embedding-profile-id PROFILE_ID \
  --document-embedding-replay /evidence/qwen/page-replay.jsonl \
  --provider-receipt /evidence/qwen/page-provider-receipt.json \
  --database-output /evidence/qwen/page.sqlite3 \
  --vectors-output /evidence/qwen/vectors.f32 \
  --inventory-output /evidence/qwen/inventory.jsonl \
  --generation-manifest-output /evidence/qwen/page-manifest.json
```

query input은 `format_qwen3_query(question)`과 pinned tokenizer/token limit을 봉인한 뒤 같은
`qwen-live-replay` command로 query replay를 만든다.

```bash
uv run python -m cardrag_mcp.external_gold_producer qwen-query-inputs \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --source-commit SOURCE_COMMIT \
  --embedding-profile-id PROFILE_ID \
  --provider-id deepinfra \
  --maximum-tokens 8192 \
  --tokenizer /evidence/tokenizer.json \
  --output /evidence/qwen/query-inputs.jsonl
```

## Observation과 기존 validator 연결

두 lane 모두 answer artifact, query replay, provider receipt, generation manifest, DB, inventory를
받아 external observation v2를 만든다. Qwen page lane에는 corpus vector sidecar도 필수다.

observation v2는 query마다 모든 corpus row를 JSON으로 반복하지 않는다. inventory는 한 번만
보존하고, 다음 sidecar를 manifest와 per-query offset/size/count/SHA-256으로 결합한다.

- dense score matrix: query × corpus row, little-endian float32, row-major
- query vector matrix: query × embedding dimension, little-endian float32, row-major
- v1.0.9 lexical rank JSONL: query마다 newline을 포함한 canonical JSON 한 줄. 0건도 한 줄을
  보존하며, 존재하는 rank는 unique이고 1부터 연속이며 최대 250개다.

offset은 0부터 빈틈없이 이어지고 float segment size는 정확히 `count * 4`다. trailing byte는
금지한다. 각 sidecar와 observation/inventory/manifest/run 파일은 95,000,000 bytes 이하여야
한다. 예를 들어 v1.0.9의 300 query × 4,175 row score matrix는 5,010,000 bytes이고,
300 × 1,536 query vector matrix는 1,843,200 bytes다.

primary span은 authoritative full ordering의 상위 100개다. contract는 그 100 span에서만
추출하지 않고 full ordering을 순회해 unique contract 최대 100개를 만든다. v1.0.9 dense
trace는 span 250개와 unique contract 100개를 보존한다. manifest의 고정 상한은
`maximum_result_contracts=100`, `maximum_result_spans=100`,
`maximum_dense_trace_contracts=100`, `maximum_dense_trace_spans=250`이다.

### 두 단계 bootstrap/final 경로

external lane도 retrieval과 answer를 순환 없이 분리한다. 먼저 모든 query에 동일한 canonical
abstention을 넣은 임시 answer artifact를 만든다. 이 값의 source of truth는
`gold_capture._bootstrap_answers`와 같은 문자열/빈 evidence tuple 계약이며, gold의 정답 label은
읽지 않는다. 다음 예시는 파일을 create-only(`xb`)로 만든다.

```bash
uv run --project apps/cardrag-mcp python - \
  /evidence/gold.jsonl GOLD_SHA256 qwen_page PAGE_GENERATION_ID PAGE_MANIFEST_SHA256 \
  /evidence/bootstrap/qwen_page.bootstrap-answers.jsonl <<'PY'
import hashlib
import sys
from pathlib import Path

from cardrag_core import canonical_json_bytes
from cardrag_mcp.evaluation import EvaluatedAnswer, load_gold_jsonl
from cardrag_mcp.gold_capture import AnswerArtifactManifest, AnswerRecord

gold_path, expected_gold, lane, generation_id, manifest_sha, output_name = sys.argv[1:]
gold = load_gold_jsonl(Path(gold_path), release_gate=True)
if gold.sha256 != expected_gold:
    raise SystemExit("gold SHA-256 mismatch")
answer = EvaluatedAnswer(
    text="제공된 검색 근거에서 답을 확인할 수 없습니다.",
    no_answer=True,
    citation_span_ids=(),
    numeric_facts=(),
    selected_revision_ids=(),
)
records = [
    AnswerArtifactManifest(
        schema_version="cardrag.gold-answer-artifact.v1",
        lane=lane,
        gold_sha256=gold.sha256,
        query_count=len(gold.queries),
        generation_id=generation_id,
        generation_manifest_sha256=manifest_sha,
        answer_profile_id="cardrag.answer.bootstrap-no-answer.v1",
        synthetic=False,
    ),
    *(
        AnswerRecord(
            schema_version="cardrag.gold-answer.v1",
            query_id=query.query_id,
            query_sha256=hashlib.sha256(query.question.encode("utf-8")).hexdigest(),
            answer=answer,
        )
        for query in gold.queries
    ),
]
payload = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
output = Path(output_name)
output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with output.open("xb") as stream:
    stream.write(payload)
print(hashlib.sha256(payload).hexdigest())
PY
```

그 artifact로 bootstrap observation과 seal을 **bootstrap 전용 경로**에 만든다. Qwen page seal은
parent v5 source를 다시 검증하므로 `--source-generation-manifest`와 `--source-database`가
release mode에서 필수다.

```bash
uv run python -m cardrag_mcp.external_gold_producer observe \
  --lane qwen_page \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --answer-artifact /evidence/bootstrap/qwen_page.bootstrap-answers.jsonl \
  --expected-answer-artifact-sha256 BOOTSTRAP_ANSWER_SHA256 \
  --query-embedding-replay /evidence/qwen/query-replay.jsonl \
  --provider-receipt /evidence/qwen/query-provider-receipt.json \
  --generation-manifest /evidence/qwen/page-manifest.json \
  --database /evidence/qwen/page.sqlite3 \
  --vectors /evidence/qwen/vectors.f32 \
  --inventory /evidence/qwen/inventory.jsonl \
  --source-commit SOURCE_COMMIT \
  --score-matrix /evidence/bootstrap/qwen_page.dense-scores.f32 \
  --query-vector-matrix /evidence/bootstrap/qwen_page.query-vectors.f32 \
  --output /evidence/bootstrap/qwen_page.capture-attestation.jsonl

uv run cardrag-gold-capture external \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit SOURCE_COMMIT \
  --observation /evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-observation-sha256 BOOTSTRAP_OBSERVATION_SHA256 \
  --inventory /evidence/qwen/inventory.jsonl \
  --expected-inventory-sha256 INVENTORY_SHA256 \
  --generation-manifest /evidence/qwen/page-manifest.json \
  --database /evidence/qwen/page.sqlite3 \
  --source-generation-manifest /candidate/manifest.json \
  --source-database /candidate/index.sqlite3 \
  --vectors /evidence/qwen/vectors.f32 \
  --score-matrix /evidence/bootstrap/qwen_page.dense-scores.f32 \
  --query-vector-matrix /evidence/bootstrap/qwen_page.query-vectors.f32 \
  --output /evidence/bootstrap/qwen_page.jsonl \
  --receipt /evidence/bootstrap/qwen_page.capture-receipt.json
```

v1.0.9 `observe`는 lexical sidecar 외에도 preserved source의 네 release anchor가 모두 필수다.
Qwen 명령을 v1.0.9 경로로 바꿀 때 다음 인자를 **전부** 추가하고 `--vectors`는 제거한다.

```bash
  --lexical-ranks /evidence/bootstrap/v109_baseline.lexical-ranks.jsonl \
  --expected-v109-run-id 2208f0c6076649c4be915be182422b6a \
  --expected-v109-generation-id g-2208f0c6076649c4be915be1-d11f80f9af71 \
  --expected-v109-manifest-sha256 dd12487e4f92a2d84362322f04d027421540c6bda27659e46cf6af553e216002 \
  --expected-v109-database-sha256 d25be45bc5d39af6561e587635b08312913107b6f6416500da39ab9eb757d38f
```

bootstrap run/receipt로 AnswerInput과 최종 answer/producer receipt/ledger/state bundle을 만든 뒤,
최종 answer artifact를 사용해 observation도 다시 만든다. bootstrap 파일을 덮어쓰지 않으며
observation, dense/query sidecar, run, receipt는 모두 **final 전용 경로**를 사용한다.

```bash
uv run python -m cardrag_mcp.external_gold_producer observe \
  --lane qwen_page \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --answer-artifact /evidence/answers/qwen_page.answers.jsonl \
  --expected-answer-artifact-sha256 ANSWER_SHA256 \
  --query-embedding-replay /evidence/qwen/query-replay.jsonl \
  --provider-receipt /evidence/qwen/query-provider-receipt.json \
  --generation-manifest /evidence/qwen/page-manifest.json \
  --database /evidence/qwen/page.sqlite3 \
  --vectors /evidence/qwen/vectors.f32 \
  --inventory /evidence/qwen/inventory.jsonl \
  --source-commit SOURCE_COMMIT \
  --score-matrix /evidence/final/qwen_page.dense-scores.f32 \
  --query-vector-matrix /evidence/final/qwen_page.query-vectors.f32 \
  --output /evidence/final/qwen_page.capture-attestation.jsonl
```

최종 seal에는 answer semantic replay에 필요한 인자를 모두 전달한다. `--answer-retrieval-*`는
AnswerInput이 실제로 고정한 **bootstrap** run/receipt/attestation/sidecar를 가리킨다.

```bash
uv run cardrag-gold-capture external \
  --gold /evidence/gold.jsonl \
  --expected-gold-sha256 GOLD_SHA256 \
  --expected-source-commit SOURCE_COMMIT \
  --observation /evidence/final/qwen_page.capture-attestation.jsonl \
  --expected-observation-sha256 FINAL_OBSERVATION_SHA256 \
  --inventory /evidence/qwen/inventory.jsonl \
  --expected-inventory-sha256 INVENTORY_SHA256 \
  --generation-manifest /evidence/qwen/page-manifest.json \
  --database /evidence/qwen/page.sqlite3 \
  --source-generation-manifest /candidate/manifest.json \
  --source-database /candidate/index.sqlite3 \
  --vectors /evidence/qwen/vectors.f32 \
  --score-matrix /evidence/final/qwen_page.dense-scores.f32 \
  --query-vector-matrix /evidence/final/qwen_page.query-vectors.f32 \
  --output /evidence/final/qwen_page.jsonl \
  --receipt /evidence/final/qwen_page.capture-receipt.json \
  --answer-input /evidence/answers/qwen_page.input.jsonl \
  --expected-answer-input-sha256 ANSWER_INPUT_SHA256 \
  --answer-producer-receipt /evidence/answers/qwen_page.producer-receipt.json \
  --expected-answer-producer-receipt-sha256 ANSWER_RECEIPT_SHA256 \
  --answer-artifact /evidence/answers/qwen_page.answers.jsonl \
  --expected-answer-artifact-sha256 ANSWER_SHA256 \
  --answer-call-ledger /evidence/answers/qwen_page.call-ledger.jsonl \
  --answer-state-identity /evidence/answers/qwen_page.state-identity.json \
  --answer-state-bundle /evidence/answers/qwen_page.state-bundle.jsonl \
  --answer-profile-id cardrag.answer.extractive-k8.v1 \
  --answer-retrieval-run /evidence/bootstrap/qwen_page.jsonl \
  --expected-answer-retrieval-run-sha256 BOOTSTRAP_RUN_SHA256 \
  --answer-retrieval-capture-receipt /evidence/bootstrap/qwen_page.capture-receipt.json \
  --expected-answer-retrieval-capture-receipt-sha256 BOOTSTRAP_RECEIPT_SHA256 \
  --answer-retrieval-attestation /evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-answer-retrieval-attestation-sha256 BOOTSTRAP_OBSERVATION_SHA256 \
  --answer-retrieval-raw-score /evidence/bootstrap/qwen_page.capture-attestation.jsonl \
  --expected-answer-retrieval-raw-score-sha256 BOOTSTRAP_OBSERVATION_SHA256 \
  --answer-retrieval-corpus-inventory /evidence/qwen/inventory.jsonl \
  --expected-answer-retrieval-corpus-inventory-sha256 INVENTORY_SHA256 \
  --answer-retrieval-dense-score-matrix /evidence/bootstrap/qwen_page.dense-scores.f32 \
  --expected-answer-retrieval-dense-score-matrix-sha256 BOOTSTRAP_DENSE_SHA256 \
  --answer-retrieval-query-vector-matrix /evidence/bootstrap/qwen_page.query-vectors.f32 \
  --expected-answer-retrieval-query-vector-matrix-sha256 BOOTSTRAP_QUERY_VECTOR_SHA256
```

v1.0.9 final observation에는 위 네 preserved-anchor와 final `--lexical-ranks`를 다시 전달하고,
final seal에는 다음 두 answer lexical 인자까지 추가한다. Qwen page에서는 lexical 인자를 주면
실패한다. sealed decision profile이면 어느 lane이든 `--answer-decision`과
`--expected-answer-decision-sha256` 쌍도 추가한다.

```bash
  --lexical-ranks /evidence/final/v109_baseline.lexical-ranks.jsonl \
  --answer-retrieval-lexical-ranks /evidence/bootstrap/v109_baseline.lexical-ranks.jsonl \
  --expected-answer-retrieval-lexical-ranks-sha256 BOOTSTRAP_LEXICAL_SHA256
```

capture receipt는 corpus inventory, dense score matrix, query vector matrix와 v1.0.9 전용 lexical
rank artifact binding을 그대로 보존한다.

## 완전 offline/fixture 실행과 credential 경계

이미 봉인한 replay, receipt, raw envelope가 있으면 `qwen-page-corpus`, `observe`, 기존 external
seal은 network와 API key 없이 실행된다. dedicated tests의 deterministic fixture vector와
`--fixture-mode` 결과는 release evidence가 아니며 300-query release gate도 통과시키지 않는다.
`--fixture-mode` 자체가 live 명령을 dry-run으로 바꾸지는 않으므로, offline 검증에서는 live
command를 호출하지 않는다.

live command는 exact `https://openrouter.ai/api/v1` origin/path와 모든
input/profile/token limit을 먼저 검증한 뒤에만 caller 지정 key file을 `O_NOFOLLOW` 방식으로
읽고 client를 만든다. alternate HTTPS host, port, userinfo, query, fragment, backslash/control
문자, redirect-enabled 또는 다른 base URL의 injected client는 key read와 network call 전에
거부한다. Codex/OAuth 설정 경로를 탐색하지 않는다. key 문자열이 provider body나 response
header에 나타나면 raw response와 replay를 발행하지 않는다. 구현/테스트 중에는 실제 paid
provider를 호출하지 않았고 Docker/WebDAV도 변경하지 않았다.

중단 재개 시 state identity와 완료된 raw-response shard를 검증해 재사용하므로 동일 shard를
다시 호출하지 않는다. request identity/reservation은 provider call 전에 durable하게 봉인하고,
response는 caller output parent와 무관한 state path에 먼저 create-only로 저장한다. OpenRouter의
idempotency 지원은 가정하지 않으므로 provider가 성공했지만 response byte를 받기 전에 process가
죽는 구간은 재호출 여부를 증명할 수 없는 외부 ambiguity로 남는다. 잘못되거나 부분적인
immutable artifact는 자동 덮어쓰지 않는다.
