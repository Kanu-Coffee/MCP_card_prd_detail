# 검색과 답변 평가

평가 도구는 동일한 원문과 질의에서 검색 coverage, 문서 집계와 답변 근거를 재현 가능하게
검증합니다. 운영 Worker나 활성 DB를 평가용으로 수정하지 않습니다. 완성된 immutable generation의
별도 읽기 전용 입력과 새 출력 디렉터리를 사용하세요. 실제 릴리스 검사는
[릴리스 안내](RELEASING.md), 저장 계약은 [데이터 형식](DATA_FORMATS.md)에 설명합니다.

## 평가 범위와 입력

평가 기록에는 실행한 범위와 미실행 범위를 구분합니다. 합성 fixture 통과, 과거 실행 기록,
private artifact의 존재만으로 현재 이미지의 live 검증이나 검색 품질을 주장하지 않습니다.
일반 설치는 사설 gold·이전 운영 DB가 없어도 가능하지만, 해당 자료가 필요한 비교 실험은
검증 가능한 입력이 준비되기 전까지 미실행으로 남깁니다.

gold는 사람이 검토한 300~500개 질의와 exact source label로 구성합니다. 초안 기본값은
300개, no-answer 24개이며 카드사·질의 유형을 층화합니다. 실제 대상 카드사에 대한 source가
충분해야 합니다. 현재 release draft는 지원하는 8개 카드사를 모두 요구하며 일부 카드사가
빠지면 생성을 중단합니다. no-answer 초안은 정답 부재를 자동 판정한 것이 아닙니다. 검토자가 전체
corpus에 답이 없음을 확인하고 승인해야 합니다.

positive label은 revision, node, page, 반개구간 source offset과 SHA-256을 고정합니다.
혜택뿐 아니라 조건·제외·공통 유의사항, cross-page 근거, 현재/과거 개정과 숫자 단위를 확인합니다.
근거가 부족한 질의를 임의로 승인하거나 candidate의 검색 결과를 gold 정답으로 사용하지 않습니다.
모든 필수 항목이 승인되어야 봉인할 수 있습니다.

```bash
uv run cardrag-gold-review draft \
  --database /evaluation/source/index.sqlite3 \
  --output /evaluation/gold-draft.jsonl \
  --state /evaluation/private/gold-review-state.json \
  --count 300 --no-answer-count 24 --seed 1010
```

`cardrag-gold-review serve-gold`로 검토하고 `seal-gold`로 승인된 gold를 봉인합니다.
review 서버는 loopback 전용이며 Host/Origin/CSRF, Content-Type·크기와 CSP를 검사합니다.
초안·review state·익명 A/B packet은 검토자 접근 범위에만 보관합니다. 답변 A/B는 lane 정체를
숨기고 좌우 배정을 균형 있게 하며 자연스러움과 사실 완결성을 독립적으로 평가합니다.
gold·run·답변 byte hash가 바뀌면 이전 packet이나 평점을 재사용하지 않습니다.

## 비교 lane과 원문 결속

기존 artifact와 호환되는 lane 이름은 `v109_baseline`, `qwen_page`, `qwen_structure_exact`,
`lexical_shadow`, `reranker_shadow`입니다. 이름은 평가 schema 식별자이며 설치 버전이 아닙니다.
legacy baseline은 정확히 호환되는 frozen generation·DB·manifest identity가 필요합니다.
현재 generation을 그 이름의 과거 baseline으로 바꿔 넣거나 sealed source 검사를 생략할 수 없습니다.

Qwen page 비교군은 동일 source 페이지를 1,600문자 window와 160문자 overlap으로 나누며
뒤쪽 절반의 newline/space 경계를 우선합니다. 경계 공백을 제거해도 원본 offset은 유지합니다.
chunk ID는 document/page/range/text SHA에 결속됩니다. `evaluation_chunks`의 열은
`row_index`, `chunk_id`, `contract_revision_id`, `span_id`, `document_id`, `page`, `source_start`,
`source_end`, `text`, `input_sha256`이며 이 형식의 `span_id`는 `chunk_id`와 같습니다.
별도 벡터 sidecar와 inventory는 같은 연속 row 순서를 사용합니다.

page 입력을 준비하는 예시는 다음과 같습니다. profile ID, token limit와 tokenizer는 실제
검증한 provider profile과 일치하도록 채워야 합니다. 이 준비 명령 자체는 provider를 호출하지 않습니다.

```bash
uv run cardrag-gold-external-producer qwen-page-inputs \
  --source-generation-manifest /evaluation/source/generation.json \
  --source-database /evaluation/source/index.sqlite3 \
  --source-commit "$EVALUATION_SOURCE_COMMIT" \
  --embedding-profile-id "$EVALUATION_EMBEDDING_PROFILE_ID" \
  --provider-id deepinfra --maximum-tokens 8192 \
  --tokenizer /evaluation/source/tokenizer.json \
  --output /evaluation/qwen-page-inputs.jsonl
```

live replay는 고정한 source/profile/provider와 정확한 입력 bytes를 결속하며 호출 receipt를 남깁니다.
API key는 파일로만 전달하고 공식 OpenRouter endpoint 검사를 통과한 뒤 읽습니다.
같은 state의 검증된 완료 기록은 재사용하지만 미완료 예약을 호출 성공으로 추정하지 않습니다.

## Exact 점수와 문서 집계

`cardrag-aggregation-capture`는 gold의 시간 범위를 적용한 모든 대상 row를 exact 채점합니다.
벡터·DB·generation·source commit·gold·embedding profile과 `exact_row_corpus_sha256`이
동일해야 합니다. 입력 inode identity와 최종 전체 hash를 확인하므로 같은 bytes로 경로를
교체한 경우에도 진행 중 캡처를 계속하지 않습니다.

`cardrag.document-aggregation-score-artifact.v2`는 다음 네 파일을 결속합니다.

| 파일 | 내용 |
|---|---|
| `document-aggregation-scores.jsonl` | manifest와 query별 coverage·binary segment hash |
| `document-aggregation-corpus-inventory.jsonl` | 정확한 matrix column 순서의 row provenance |
| `document-aggregation-score-matrix.f32` | query-major/row-major little-endian float32 점수 |
| `document-aggregation-query-vectors.f32` | query-major 4,096차원 little-endian float32 질의 벡터 |

각 파일 상한은 95,000,000 bytes입니다. 점수 수는 query 수 × corpus row 수이며 최대
20,000,000개이므로 score matrix는 최대 80,000,000 bytes입니다. provider 호출 전에
필요한 자원을 계산합니다. inventory에는 연속 ordinal, 증가하는 row index, revision/node/view,
input SHA와 profile ID가 있어야 합니다. 각 revision에는 하나의 `CONTRACT`와 child가 필요합니다.
점수는 유한한 `[-1,1]`, 질의 벡터는 유한한 정규화 4,096개 float32여야 합니다.

JSON은 canonical encoding이며 duplicate key를 허용하지 않습니다. 검증기는 정규 파일을
O_NOFOLLOW로 hash-pin하고 mmap segment를 순회합니다. segment의 hole·overlap·swap·truncation·
trailing byte와 1-ULP 변경도 hash·크기·순서 검증에서 실패합니다. 합성 `fixture_mode`와
`release_grade`는 구분되며 fixture를 실제 봉인 profile의 근거로 사용할 수 없습니다.

문서 순위 정책은 같은 점수에서 다시 계산합니다.

- `max_child`: `CONTRACT`를 제외한 최대 row 점수.
- `top3_mean`: `CONTRACT`를 제외한 상위 최대 3개 row 점수의 평균.
- `contract_plus_child`: 단일 `CONTRACT`와 `max_child`를 각각 0.5 가중.

동점은 revision ID의 bytewise 오름차순으로 정렬합니다. 통계적 승자를 정하는 기준은 contract
`nDCG@10`의 paired bootstrap 95% CI이며 최소 2,000회, 기본 seed 1010입니다.
한 정책이 다른 두 정책에 대한 delta CI 하한 모두 `> 0`일 때만 유일한 승자입니다.
`max_child` 외 정책은 전체와 필수 retrieval slice에서 Recall@10, nDCG@10, MRR@10 회귀도
배제해야 합니다. 승자가 없으면 `sealed_profile=null`과 실패 상태를 남깁니다.

`cardrag_mcp.aggregation_profile`은 `--expected-source-commit`, 실제 generation manifest,
gold와 네 점수 파일에서 profile을 재계산합니다. 평가 generation M0에 profile을 결속하고,
같은 exact row corpus를 갖는 serving generation M1이 profile/hash를 포함하는 두 단계로
상호 hash 순환을 피합니다. Worker에 profile을 주입할 때는 파일과 SHA 설정을 함께 전달해야 하며,
현재 candidate M0 identity와 일치하지 않으면 게시하지 않습니다.

## 답변 캡처와 재개

검색과 답변 캡처는 별도 단계입니다. 검색 bootstrap 출력은 `/evaluation/bootstrap/`, 최종
출력은 `/evaluation/final/`처럼 다른 디렉터리를 사용합니다. bootstrap은 gold answer를
답변으로 주입하지 않으며 아직 완성된 답변 평가 증거가 아닙니다.

1. source manifest·DB·vector·gold에 결속된 검색 결과와 점수 artifact를 캡처합니다.
2. 실제 검색한 full contract/top-K source에서 answer input을 만듭니다. provider에게 gold
   정답, no-answer label이나 정답 span 목록을 주지 않습니다.
3. `cardrag-gold-answer-artifact`로 provider 결정과 답변을 기록합니다. 받은 span ID만 선택할 수
   있고 numeric fact는 인용 원문에 있어야 하며 revision도 일치해야 합니다.
4. answer artifact, call ledger, state identity, state bundle, producer receipt와 각 SHA를 결속합니다.
5. `cardrag-gold-capture`로 bootstrap retrieval run·receipt·attestation·raw score·inventory·
   score matrix·query vectors와 답변 산출물을 함께 검증하여 final 출력을 만듭니다.

각 입력의 path와 expected SHA는 한 쌍입니다. `--answer-input`, `--answer-producer-receipt`,
`--answer-artifact`, `--answer-call-ledger`, `--answer-state-identity`, `--answer-state-bundle`,
`--answer-profile-id`를 해당 명령의 `--help`에 맞춰 전달합니다. 기존 retrieval의
`--answer-retrieval-run`과 receipt/attestation/raw-score/corpus-inventory/dense-score-matrix/
query-vector-matrix 결속도 빠뜨리지 않습니다. 개별 파일 hash 일치만으로 전체 체인의 결속을
대체하지 않습니다. lexical lane을 사용했다면 그 rank artifact도 함께 검증합니다.

호출 전 idempotency reservation을 기록합니다. 검증된 완료 state는 네트워크 호출 없이 재개하고,
outcome이 불명확한 reservation은 자동 재호출하지 않습니다. native finalize는 provider를 다시
호출하지 않고 기존 contract/span/shadow 결과를 유지한 채 답변 artifact 결속만 완성합니다.
unbound 또는 fixture 결과는 release eligible 증거가 아닙니다.

## 선택적 candidate 실험

reranker shadow와 long-context map-reduce는 기본값이 꺼져 있으며
`CARDRAG_CHANNEL=candidate-v1.0.11`에서만 설정 검증을 통과합니다. 이 channel 이름은
호환 데이터 계약입니다. 두 실험 모두 주 exact 검색 결과를 추가·삭제·재정렬하지 않습니다.

`CARDRAG_RERANKER_SHADOW_ENABLED=true`는 이미 exact 채점한 dense 근거만
`qwen/qwen3-reranker-8b`의 Fireworks route로 보내며 fallback을 허용하지 않습니다.
`CARDRAG_RERANKER_SHADOW_*`로 후보 수·timeout·응답 크기를 제한합니다. 기본 응답 상한은
1 MiB이고 JSON parsing 전에 Content-Length와 실제 누적 body 크기를 검사합니다.
generation·query hash·model·provider·dense 후보 hash에 결속된 artifact는
`audit-reports/reranker-shadow/`에 보존합니다. provider 실패는 bounded shadow 진단으로
격리하며 주 검색 실패로 바꾸지 않습니다.

map-reduce는 `CARDRAG_EXPERIMENTAL_MAP_REDUCE_ENABLED=true`와 같은 접두사의
`MODEL`, `PROVIDER_ID`, `EVALUATION_SHA256`을 명시해야 합니다. 기본 candidate overlay도
이를 false로 고정하므로 실험 전용 설정에서 실제 전달 값을 별도로 검토해야 합니다.
활성화하면 13번째 MCP 도구 `experimental_long_context_audit`가 추가되며
`action="start"`, `"poll"`, `"cancel"`과 `job_id`로 작업을 관리합니다. 꺼져 있으면
해당 provider client와 도구를 만들지 않습니다.

model·provider·gold 평가 SHA, prompt, 입력 문자·완료 token·작업별 호출/입출력·응답 크기
예산은 immutable profile ID에 결속됩니다. poll 한 번은 provider 호출을 최대 한 번만
진행합니다. 전체 계약 또는 source leaf 경계의 bounded section을 map하고 모든 계약의 map이
끝난 뒤에만 계층적 reduce를 수행합니다. 허용 근거는 정확한 OCR page/offset의 원문이고,
reduce 결과는 승인된 map span의 부분집합이어야 합니다.

시도한 호출마다 봉인된 완료 token 상한 전체를 영구 차감합니다. durable reservation과
cross-process slot policy가 저장량과 provider 동시성을 제한하며 재시작 시 다른 동시성 설정을
허용하지 않습니다. 검증할 수 없는 provider 응답은 durable ambiguous call로 남기고 자동
재시도하지 않습니다. 이후 poll은 같은 pending job ID를 반환하므로 운영자가 cancel할 수 있습니다.

작업은 generation·query·profile에 결속되며 첫 ledger 전에 generation GC root를 게시합니다.
generation 교체나 재시작 뒤에도 같은 작업은 고정한 세대를 사용합니다. hard crash 뒤 남은
미완료 root를 단순히 삭제하지 말고 job의 cancel/recovery 경계를 사용합니다.

exhaustive와 map-reduce는 기본 32개 고유 작업·총 2 GiB·artifact당 256 MiB의 quota를
공유합니다. reranker는 별도로 1,024개·총 512 MiB·artifact당 8 MiB입니다. root-only claim,
generation root, provider policy와 임시 증가량도 해당 한도에 포함합니다. quota에 도달해도
기존 artifact를 자동 삭제하지 않습니다. 영속 정책·남은 reservation 처리는
[복구 안내](RECOVERY.md)를 따릅니다.

## 실행과 결과 해석

설치한 CLI에서 세부 입력과 필수 hash를 확인할 수 있습니다.

```bash
uv run cardrag-aggregation-capture --help
uv run cardrag-gold-capture --help
uv run cardrag-gold-external-producer --help
uv run cardrag-gold-answer-artifact --help
uv run cardrag-gold-review --help
uv run python -m cardrag_mcp.aggregation_profile --help
uv run python -m cardrag_mcp.evaluation --help
```

결과에는 Recall@10, nDCG@10, MRR@10 외에 source span, 조건 묶음, numeric fact, 개정 선택과
no-answer 정확도를 포함합니다. shadow 비교가 주 검색의 contract/span bytes를 바꾸지 않았는지도
확인합니다. 오류·추정·미실행을 성공 결과와 합치지 않습니다. 개발 회귀 검사는
[기여 안내](../CONTRIBUTING.md)의 로컬 절차를 따릅니다.
