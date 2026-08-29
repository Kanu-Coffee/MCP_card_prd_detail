# CardRAG v1.0.10 acceptance gates

candidate는 아래 gate가 모두 증거로 남기 전에는 release-ready가 아닙니다. 테스트
통과와 stable 전환 승인은 서로 다른 단계입니다.

## 구조와 revision

- 카드사별 KB/삼성/신한/우리 fixture에서 heading, table, footnote, continuation,
  boilerplate와 `UNCLASSIFIED` fallback을 검증합니다.
- canonical leaf를 재조합한 비공백 원문 count/hash가 OCR page 원문과 100% 같습니다.
- 모든 span의 page/range/hash가 일치하고 줄 중간 임의 truncation이 없습니다.
- cross-contract parent/link, notice와 footnote 혼합은 0건입니다.
- lineage current 중복은 fail-closed이며 불명확한 revision은 `ambiguous`입니다.
- current/as-of/history에서 superseded 혼입과 임의 최신 선택이 없습니다.

## 임베딩과 sidecar

- live preflight에서 allowlisted provider가 정확히 4,096 finite float를 반환합니다.
- 동일 입력 반복 cosine과 최소 20개 한국어 혜택/제외/표 문장의 provider 비교를
  기록합니다. FP8 route는 품질 profile에서 제외합니다.
- query payload snapshot은 Qwen instruction을 포함하고 document payload는 instruction을
  포함하지 않습니다.
- token overflow는 구조 boundary split 또는 error이며 자동 truncation이 아닙니다.
- profile/input 재실행은 v5 cache hit, provider/profile 변경은 cache miss입니다.
- v1.0.9 1,536D와 legacy Qwen cache read는 0건입니다.
- sidecar size는 `row_count × 4096 × 4`이고 SHA, finite, L2 norm을 검증합니다.

### 2026-08-29 sanitized live preflight 증거

[봉인된 JSON evidence](../release-evidence/v1.0.10/qwen-provider-preflight.json)는
`cardrag.qwen-provider-preflight-evidence.v1`이며, `evidence_sha256` 필드를 제외한
canonical payload의 SHA-256은
`b52c81be473d16b781aad5c4310389d51f9bde7930b7a197224a8979cc0ea666`입니다.
관측 시각은 `2026-08-29T06:22:03.255807+00:00`입니다.

- 고정 sample set의 한국어 혜택·제외·표 문장 24개를 동일 입력으로 두 번씩 호출한
  live preflight에서 pinned `deepinfra`와 `nebius` route가 모두 통과했습니다.
- 두 route 모두 요청별 정확히 4,096개의 finite 값을 반환했고, Worker의 L2 정규화와
  반복 cosine 하한 `0.999` 검증을 통과했습니다. provider 간 cosine은 비교 report에
  산출하지만 provider 품질 우열이나 gold 합격으로 해석하지 않습니다.
- `deepinfra`는 maximum tokens 32,768, 최소/평균 반복 cosine
  `0.9998990168876162`/`0.9999236649344625`였고, `nebius`는 maximum tokens 32,000,
  최소/평균 `0.9998723435368064`/`0.9999051657671553`이었습니다. provider 간
  최소/평균 cosine은 `0.9998338336912108`/`0.999902989123012`였습니다.
- registry request model `qwen/qwen3-embedding-8b`에 대해 live 응답은
  `Qwen/Qwen3-Embedding-8B`처럼 canonical capitalization을 반환할 수 있습니다. 검증은
  경로·철자·구성요소를 그대로 고정한 채 case-only 차이만 허용합니다.
- endpoint metadata의 `max_prompt_tokens=null`은 유효한 양의 정수
  `context_length`로만 대체합니다. 선택된 값은 profile/cache namespace에 봉인되고
  candidate 설정과 다르면 corpus 처리 전에 중단합니다.
- 두 route의 metadata가 `dimensions`를 광고하지 않아 routing request는
  `require_parameters=false`를 사용합니다. 대신 `order`와 `only`를 동일 provider 하나로
  고정하고 `allow_fallbacks=false`로 두며, 실제 응답의 provider·4,096D·finite·index를
  다시 검증합니다.

이 증거에는 credential, endpoint 원문, 응답 body와 sample 원문을 포함하지 않습니다.
이는 live provider 계약 gate만 통과했다는 뜻이며, 실제 4개 카드사 full candidate run,
MCP smoke, 300~500개 gold 평가가 통과했다는 증거는 아닙니다.

### Qwen reranker provider preflight 증거

[Sealed reranker preflight JSON](../release-evidence/v1.0.10/qwen-reranker-provider-preflight.json)은
`cardrag.qwen-reranker-provider-preflight-evidence.v1`이며 `evidence_sha256`을 제외한
canonical payload SHA-256은
`80a0602e7acaaa6e4d8d718570d3c588b72f45243f8331389befd19f44304b13`입니다.

- endpoint metadata는 `qwen/qwen3-reranker-8b`, 유일 provider tag `fireworks`,
  `context_length=40960`, `max_prompt_tokens=null`, quantization unknown으로 관측했습니다.
- [OpenRouter `POST /api/v1/rerank`](https://openrouter.ai/docs/api/api-reference/rerank/create-rerank)는
  [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)의
  `order`와 `only`를 `fireworks` 하나로 고정하고
  `allow_fallbacks=false`, `require_parameters=false`, 3 documents와 `top_n=3`으로
  실행했습니다.
- HTTP 200 응답은 provider `Fireworks`, canonical model alias
  `accounts/fireworks/models/qwen3-reranker-8b`, 3/3 unique indices와 내림차순 finite
  score를 반환했습니다. runtime은 요청 model 또는 이 봉인된 canonical alias만
  허용하며 provider/count/index/finite score를 fail-closed 검증합니다.

이 evidence에는 query/document 원문, endpoint URL, credential, 원 응답 body가 없고
`candidate_ranking_executed=false`, `gold_evaluation_executed=false`가 명시되어 있습니다.
따라서 provider API 계약만 확인한 것이며 candidate ranking이나 300~500개 gold gate
합격을 뜻하지 않습니다.

### v1.0.9 immutable cache reuse prerequisite 증거

[Sealed cache audit JSON](../release-evidence/v1.0.10/v109-cache-reuse-audit.json)은
`cardrag.v109-cache-reuse-audit.v1`이며, `evidence_sha256` 필드를 제외한
canonical payload SHA-256은
`f62e60824c7c9a5ef96d41f701616c4f9d76167d642c9c551aaafce82ec5c56f`입니다.
관측 시각은 `2026-08-29T06:31:52.288216+00:00`입니다.

- `candidate-v1.0.9` pointer/manifest/READY의 canonical SHA와 상호 binding을
  검증했습니다. 대상은 v4 KB 747 documents/4,175 chunks이며, 715개
  cache control manifest/READY/reuse-key/output binding이 모두 유효했습니다.
- 714개 unique OCR CAS object는 전건 full GET SHA/size가 일치했습니다.
  652개 unique PDF CAS object는 전건 Range GET의 total size가 manifest와
  일치했고, 가장 작은 결정적 sample 1개는 full GET SHA/size까지 일치했습니다.
- 서버가 세 inventory scope의 `PROPFIND`를 403으로 거부해 디렉터리 열거는
  주장하지 않습니다. 대신 검증된 v4 manifest가 참조한 1,366개 CAS
  object를 직접 GET했습니다.
- `candidate-v1.0.10` pointer는 관측 시점에 404였습니다. 이 증거는 재사용 가능한
  immutable remote seed의 prerequisite만 입증하며 v1.0.10 full generation, 4개 카드사
  coverage, MCP smoke 또는 gold 합격을 의미하지 않습니다.

Evidence에 WebDAV URL, credential, remote object path, response body는 포함하지
않았습니다. 실제 candidate env의 WebDAV namespace와 OCR contract가 이 cache와
정확히 같은지는 launch render와 Worker cache-hit ledger에서 다시 검증합니다.

동일 OCR reuse key를 두 격리 Worker가 동시에 채울 때 provider 출력은 비결정적일 수
있습니다. 후발 Worker의 immutable manifest commit이 충돌하면 원격 first-writer의
READY, canonical manifest, source/contract, CAS SHA/size, 페이지 hash를 전부 다시
검증한 경우에만 그 원격 결과를 채택합니다. 불완전하거나 다른 계약의 객체는 기존처럼
fail-closed하며, 후발 CAS object를 삭제하거나 기존 immutable control object를
덮어쓰지 않습니다. 다만 이 winner-adoption은 새 Worker끼리의 안전성만 보장하며,
동시 실행 중인 수정 전 v1.0.9 loser를 보호하지 못합니다. 실제 candidate audit에서
공용 reuse key를 candidate가 먼저 commit한 뒤 v1.0.9 Worker가
`ocr_cache_publication_manifest_integrity`로 종료된 인과가 확인됐습니다. 따라서
v1.0.10 candidate는 remote OCR cache를 `read-only`로 강제하고, 검증된 READY/manifest/CAS
GET만 재사용하며 resolver의 native-cache CAS/manifest/READY transaction과 READY repair를
모두 0건으로 유지합니다. Generation publisher가 MCP source 제공을 위해 같은 OCR bytes를
전역 content-addressed object path에 idempotent하게 올리는 것은 별도 artifact 단계이며,
native manifest/READY가 없으면 공유 OCR cache entry를 만들지 않습니다.
운영 Worker를 재시작해 동시성으로 검증하지 않습니다.

[Sealed causality JSON](../release-evidence/v1.0.10/v109-ocr-cache-race-causality.json)은
`cardrag.v109-ocr-cache-race-causality.v1`이며, `evidence_sha256`을 제외한 canonical
payload SHA-256은
`0f1084efc30858528c586a1b01f0c91abfa198c756c13340be7d66ac1e5fbbc5`입니다. 동일
document/PDF/OCR contract/reuse key와 동일한 5개 chunk input 중 비결정적으로 달라진
candidate·v1.0.9 OCR/manifest, candidate first-writer commit, v1.0.9 integrity failure 및
exit의 순서를 직접 결속합니다. 감사 자체의 Docker/volume/WebDAV mutation과 운영
restart/signal은 모두 0건입니다. 이 증거 때문에 이전의 “구형 Worker도 winner를
채택한다”는 가정은 폐기하며, remote read-only live proof가 나오기 전 candidate를
재개하지 않습니다.

### v1.0.9 prefix-only OCR cache compatibility live 증거

[Sealed compatibility JSON](../release-evidence/v1.0.10/v109-prefix-only-cache-compatibility.json)은
`cardrag.v109-prefix-only-cache-compatibility.v1`이며, `evidence_sha256`을 제외한
canonical payload SHA-256은
`a7eec6534cd30a5dd284bf03b7e41216b29113f19b71839e16a8e4af9b38d5cb`입니다.
관측 시각은 `2026-08-29T15:07:10.351512+00:00`입니다.

- candidate volume을 read-only로 열고 정확히 계산한 두 reuse key의 READY, manifest,
  CAS 경로만 GET했습니다. 두 cache entry 모두 HTTP 200, canonical control binding,
  local/remote manifest 및 OCR bytes 일치, page SHA/size 검증을 통과했습니다. remote
  mutation은 0건이며 URL, credential, OCR 원문과 raw response는 evidence에 없습니다.
- `doc_5fb...`의 22쪽과 `doc_69bd...`의 18쪽은 visible source character가 0인
  logo/pictogram-only page이고, canonical body는 prefix-only입니다. 각 body/page SHA와
  전체 PDF/OCR/control SHA, reuse key, 상대 object 경로는 evidence에 봉인했습니다.
- 실제 v1.0.9 Worker image
  `sha256:a3be8e1b74cb310c3f0d00496db440a00f31d65a0851337205e627153ea103c8`
  (`revision=fee8f65a9fda7ae0c286ac92cf4c3f55c1a6f113`)로 control과 bytes를 다시
  검증했습니다. fee8f65 provider/checkpoint validator는 prefix-only target을 거부하지만,
  v1.0.9 cache consumer는 그 validator를 호출하지 않고 공통 `verify_ocr_bytes`를 호출해
  두 산출물을 모두 수용했습니다.
- v1.0.9와 v1.0.10의 공통 OCR byte verifier source SHA는
  `00bc8379e1aae489e0240ffbc275caed15b4da86cecab7d34ddc3e42bb67c681`이고,
  실제 manifest는 기존 `processor_version=cardrag-worker/1.0.4` 및 동일 contract SHA를
  유지합니다. 따라서 prefix-only 허용은 provider/checkpoint 입력 경계의 수정일 뿐
  sealed cache 소비 계약 변경이 아니며 OCR cache namespace bump는 필요하지 않습니다.
  namespace를 바꾸면 안전 이득 없이 계획이 요구한 검증된 v1.0.9 OCR 재사용만 잃습니다.

이 live proof는 두 회귀 문서의 cache compatibility만 증명합니다. 전체 candidate
generation, 구조 coverage, MCP smoke와 gold gate의 합격 증거로 확대 해석하지 않습니다.

### v1.0.9 구조 회귀 기준과 sealed DB 재감사

서로 다른 provenance를 한 관측으로 합치지 않습니다.

- [historical Worker-run observation](../release-evidence/v1.0.10/v109-kb-real-regression-baseline.json)은
  sanitized run ID와 원래 알고리즘/KB·Samsung count를 보존합니다. 원 run artifact의
  SHA-256이 보존되지 않았으므로 `binding=observation_only`이고 release blocker입니다.
- [sealed v4 DB re-audit](../release-evidence/v1.0.10/v109-kb-v4-structure-reaudit.json)은
  generation `g-2208f0c6076649c4be915be1-d11f80f9af71`, serving schema v4,
  DB SHA/size, corpus/contract SHA에 직접 결속됩니다. 독립 CLI가 DB를
  `O_NOFOLLOW`, immutable/query-only로 열고 감사 전후 identity와 SHA를 재검증했습니다.

sealed DB 관측은 continuation 1,379/4,175, mid-line 1,293/1,379
(`93.7636%`), titled-body 1,467/3,710, body titleless continuation
388/1,379입니다. canonical Markdown table은 header+separator+최소 한 body row의 전체
span으로 정의하며 2,779개 중 45개가 단일 chunk에 완전히 포함되지 않았습니다.
historical 관측의 titleless 389와 sealed 재감사의 388은 원 source provenance와
알고리즘이 다르므로 꾸미거나 합치지 않고 `match=false`로 봉인합니다. table count 45는
일치하며 historical table-block denominator 3,065는 historical artifact에 별도로
보존합니다. 이 차이 자체는 release blocker가 아니지만, historical source artifact
SHA 부재와 v1.0.10 candidate zero-defect 구조 감사 미완료는 blocker입니다.

```bash
python -m cardrag_worker.legacy_v4_audit \
  --validate-release-artifact "$PWD/release-evidence/v1.0.10/v109-kb-v4-structure-reaudit.json" \
  --historical-artifact "$PWD/release-evidence/v1.0.10/v109-kb-real-regression-baseline.json"
```

현재 위 release-mode 명령은 historical source SHA가 없으므로 의도대로 non-zero로
종료합니다. 일반 artifact validation은 schema, canonical bytes/self-hash, exact observed
counts와 DB binding을 검증하며 self-asserted boolean을 신뢰하지 않습니다.

## exact 검색

- NumPy brute-force와 contract/node 순위 및 tie-break가 같습니다.
- 모든 temporal-scope row가 score되며 expected/scored contracts와 rows가 같습니다.
- 모든 여섯 view lane에 대한 score ledger가 있고 candidate pruning이 없습니다.
- product-specific 검색은 한 최신 contract 전체를 원문 순서로 반환합니다.
- 비교 검색은 contract별 bundle을 먼저 완성하며 다른 계약 문맥을 섞지 않습니다.
- parent, table header/rows, footnote, 조건/한도/제외와 same-contract notice를 확장합니다.
- lexical-only evidence가 dense 순위/근거를 바꾸거나 제거하지 않습니다.
- reranker는 shadow이며 운영 결과를 바꾸지 않습니다.
- reranker provider failure도 primary bundle/rank를 유지하고 bounded failure artifact와
  diagnostics만 남깁니다.
- `approximate`, `lexical_influenced_ranking`, `reranker_influenced_ranking`은 false입니다.

## gold evaluation

release 평가 입력은 300~500개의 비민감 한국어 카드 질문과 source-span 정답을
immutable JSONL로 봉인합니다. 혜택/할인/적립, 전월실적·제외, 한도·횟수·금액,
연회비·발급·해외, 부정·예외·기간, 표·각주·cross-page, hard negative,
current/history, 상품 특정/탐색/비교, no-answer slice를 포함합니다.

비교군은 v1.0.9 Small+RRF, Qwen+기존 page window, Qwen+structure exact,
exact+lexical shadow, exact+reranker shadow입니다. contract Recall@10/50/100,
span Recall@5/10, nDCG/MRR@10, 조건 동시 회수, 숫자·기간, revision, no-answer와
citation precision/recall을 측정하고 slice별 bootstrap 95% CI를 기록합니다.

release gate는 primary retrieval의 유의한 개선, 핵심 slice 유의 회귀 없음,
최신성/문서경계/span 치명 오류 0, 혜택 제한·유의사항 동시 회수 95% 이상,
고위험 숫자·제외조건 치명 누락 0을 요구합니다. 또한 익명 A/B artifact가 실제
v1.0.9/exact run 및 각 `answer.text` hash에 결속되어야 하며, 자연스러움 delta의 CI95
하한은 0 이상, 사실 완결성 delta의 CI95 하한은 0보다 커야 합니다. release workflow는
gold/run/blind/report canonical bytes와 참조된 generation manifest SHA-256을 offline으로
재계산하고 하나라도 다르면 fail-closed합니다. publish source는 실제 annotated tag
object여야 하며 lightweight tag는 local `git cat-file -t`와 remote peeled ref 검증에서
거부됩니다.

## 자동·통합·운영 gate

다음 검사를 모두 통과시킵니다.

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
uv run pytest
docker compose ... config --quiet
systemd-analyze verify deploy/worker/cardrag-worker.service deploy/worker/cardrag-worker.timer
mapfile -d '' shell_files < <(git ls-files -z -- '*.sh' '*.bash')
((${#shell_files[@]} == 0)) || shellcheck "${shell_files[@]}"
actionlint -no-color
gitleaks detect --source . --no-banner --redact --exit-code 1
trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 .
docker build --target worker -t cardrag-ci:worker .
docker build --target mcp -t cardrag-ci:mcp .
trivy image --scanners vuln,secret --severity HIGH,CRITICAL --exit-code 1 cardrag-ci:worker
trivy image --scanners vuln,secret --severity HIGH,CRITICAL --exit-code 1 cardrag-ci:mcp
```

CI는 위 네 audit 도구를 고정 버전과 공개 SHA-256으로 설치합니다. 로컬 filesystem
scan에서 uv workspace lockfile을 해석하지 못했다는 경고가 있으면 dependency 검사가
완료된 것으로 간주하지 않고, 두 최종 image의 OS·Python dependency scan 결과로
보완합니다.

CI의 반복 가능한 merge gate는 `.git`을 제외하고 `--ignore-unfixed`를 사용해 현재
수정 가능한 HIGH/CRITICAL finding을 차단합니다. 위에 적은 image 명령은 unfixed finding도
포함하는 더 엄격한 release-acceptance gate입니다. CI scan 통과를 strict release scan
통과로 대신 기록하지 않으며, strict scan이 nonzero이면 upstream fix 가능 여부와 별개로
release blocker로 보고합니다.

실데이터 candidate full run은 네 카드사를 포함하고 PDF/OCR cache hit와 provider call
count, parser/node/view/revision count, sidecar size를 기록합니다. MCP health, generation
ID, 8 tools, exact search coverage, bundle와 revision, legacy adapter, PDF range smoke를
수행합니다. candidate Compose와 sealed worker contract는 remote OCR cache
`read-only`를 증명하고, cache miss가 포함된 live 구간에서 resolver native-cache
publication call이 0건이고 해당 reuse-key manifest/READY가 생성·변경되지 않았음을
exact-path before/after GET evidence로 봉인합니다. Generation CAS는 별도 artifact
ledger로 기록합니다.

## 승인 경계

아래는 acceptance 결과 보고 뒤 별도 승인 없이는 수행하지 않습니다.

- stable pointer PUT과 stable Worker/MCP 재시작 또는 교체
- shared native/adopted OCR cache publication
- `/opt/cardrag/current` 변경
- 운영 v1.0.9 volume/image/snapshot cleanup
- LibreChat endpoint 또는 tool 소비 경로 변경
- tag, release, image push와 외부 PR/merge
