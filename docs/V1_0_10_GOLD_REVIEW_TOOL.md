# v1.0.10 오프라인 Gold·블라인드 검토 도구

`cardrag_mcp.gold_review`는 완성된 v5 generation에서 release gold 초안 300건을
결정적으로 사전선정하고, 사람이 원문을 확인해 승인한 레이블만 봉인하며, 두 답변군의
익명 A/B 평가를 로컬에서 수집한다. 이 모듈은 검색 결과, 평가 점수, embedding/reranker,
API key, OAuth 정보 또는 provider를 읽거나 호출하지 않는다.

현재 generation이 아직 완성되지 않았다면 실제 300건 초안은 만들 수 없다. 코드는 fixture로
검증할 수 있지만, release 초안 생성은 Worker가 완성한 immutable
`cardrag.serving-db.v5` SQLite가 준비된 다음 수행한다.

## 신뢰 경계

- 초안은 네 카드사 `kb`, `samsung`, `shinhan`, `woori`를 층화하며 기본값은 카드사별
  75건, 합계 300건이다.
- 기본 no-answer 초안은 카드사별 6건, 합계 24건이다. 이는 정답이 없다는 자동 판정이
  아니다. 평가자가 전체 corpus에 실제 답이 없는지 확인하고 질문을 고친 뒤 승인해야 한다.
- 선택에는 v5 corpus의 issuer, 상품·개정 metadata, node 종류/major class, 원문 문자열만
  쓴다. candidate lane의 검색 순위·답변·점수는 입력 자체가 아니다.
- positive 후보는 `document_pages.text[source_start:source_end]`의 정확한 문자열,
  1-based page, 문자 offset, SHA-256, contract revision과 node ID를 함께 고정한다.
- comparison, cross-page, benefit-condition, current-history 초안은 필요한 서로 다른 exact
  source가 corpus에 없으면 만들지 않고 중단한다.
- 자동 생성 질문·slice·role·숫자·condition group은 모두 **미승인 초안**이다. 사람이 공개
  PDF/OCR과 대조하기 전에는 gold가 아니다.
- 리뷰에서 질문, slice, relevance, role, numeric fact, expected revision, condition group은
  수정할 수 있다. source contract/page/offset/hash는 초안에 포함된 후보 안에서만 사용할 수
  있다. source 후보가 잘못되었으면 승인하지 말고 다른 seed 또는 수정된 corpus로 새 초안을
  만든다.
- `seal-gold`는 300~500건 전부가 `approved`인 경우에만 진행하며 기존
  `load_gold_jsonl(..., release_gate=True)`로 300건, 필수 slice, no-answer,
  high-risk numeric/condition/revision 계약을 다시 검사한다.

## 1. Gold 초안 생성

generation DB는 쓰지 않고 SQLite `mode=ro&immutable=1`, `query_only=ON`,
`trusted_schema=OFF`로 연다. 전체 v5 schema/integrity/source coverage와 DB·page·span hash를
확인한다. 출력에는 입력 파일 경로가 아니라 DB/corpus/inventory digest만 기록된다.

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review draft \
  --database /candidate-generation/GENERATION_ID/index.sqlite3 \
  --output /candidate-evidence/gold-draft.jsonl \
  --state /candidate-evidence/private/gold-review-state.json \
  --count 300 \
  --no-answer-count 24 \
  --seed 1010
```

같은 sealed DB, count, no-answer count와 seed의 canonical draft bytes는 같다. 출력 파일이 이미
있으면 byte-identical 재실행만 허용한다. state는 모든 항목을 `pending`으로 시작하며 atomic
replace와 directory fsync로 재개 가능하게 저장된다.

## 2. 사람이 Gold 검토

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review serve-gold \
  --draft /candidate-evidence/gold-draft.jsonl \
  --state /candidate-evidence/private/gold-review-state.json \
  --port 8765
```

브라우저에서 `http://127.0.0.1:8765`만 연다. 서버는 `127.0.0.1` 이외의 주소에 bind하지
않으며 외부 asset이나 network 요청을 쓰지 않는다. POST는 loopback Host, 동일 Origin,
세션 CSRF token, JSON content type과 1 MiB 이하의 명시적 Content-Length를 모두 요구한다.
응답에는 `no-store`, CSP `default-src 'none'`, `connect-src 'self'`,
`frame-ancestors 'none'`과 `X-Frame-Options: DENY`가 적용된다. UI/API는 filesystem 경로,
lane 정체, OAuth/API secret을 제공하지 않는다.

각 항목에서 다음을 직접 확인한다.

1. 질문이 실제 사용자가 물을 만하고 한 가지로 해석되는지 확인한다.
2. 화면의 상품, page와 exact 원문을 공개 PDF/OCR에서 다시 찾는다.
3. `source_start`, `source_end`, `text_sha256`, contract revision이 그 원문과 맞는지 확인한다.
4. benefit와 조건·제외·notice가 함께 필요한 질문이면 모든 source와 condition group을
   포함한다.
5. 금액·비율·횟수·기간의 단위까지 `expected_numeric_facts`와 정확히 맞춘다.
6. 현재/과거를 묻는 질문은 `expected_revision_ids`와 revision role을 확인한다.
7. no-answer는 전체 corpus에 답이 없음을 별도로 확인한다.
8. 확인이 끝난 항목만 `approved`, 부적합 항목은 `rejected`, 미확인은 `pending`으로 둔다.

300건을 한 평가자가 모두 승인해도 schema상 가능하다. 다만 질문 초안 작성자와 다른 사람이
blind A/B를 수행하면 기대나 lane 지식이 선택에 미치는 영향을 더 줄일 수 있다. Gold 검토자는
GitHub 계정이나 GitHub environment reviewer 권한이 필요 없다.

## 3. Gold 봉인

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review seal-gold \
  --draft /candidate-evidence/gold-draft.jsonl \
  --state /candidate-evidence/private/gold-review-state.json \
  --output /candidate-evidence/gold.jsonl
```

명령은 canonical gold JSONL 전체 SHA-256만 stdout에 출력한다. `pending` 또는 `rejected`가
하나라도 있으면 출력하지 않는다. 기존 다른 출력은 덮어쓰지 않는다.

## 4. 익명 A/B packet 준비

Gold와 실제 `v109_baseline`, `qwen_structure_exact` run이 준비된 다음 실행한다. 기본
`ratings_per_query=1`은 rater key 하나를 주는 방식이다. 여러 번 평가할 때는 서로 다른
`--rater-key`를 반복하며, 각 query와 rater 조합은 정확히 한 번만 나타난다.

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review prepare-blind \
  --gold /candidate-evidence/gold.jsonl \
  --baseline-run /candidate-evidence/v109_baseline.jsonl \
  --candidate-run /candidate-evidence/qwen_structure_exact.jsonl \
  --packet /candidate-evidence/private/blind-review-packet.jsonl \
  --state /candidate-evidence/private/blind-review-state.json \
  --rater-key anonymous-rater-01 \
  --seed 1010
```

두 run은 gold SHA, query count와 순서를 정확히 공유해야 한다. packet은 두 run file hash의
순서 없는 집합, gold SHA, 질문, left/right 답변 원문과 각 답변 SHA를 고정한다. packet과
review state에는 lane 이름 또는 `candidate_position`이 없다. 각 rater별로 candidate의
left/right 배정 수가 정확히 같다. 이 때문에 query count는 짝수여야 하며 release 기본
300건은 150/150이다.

packet/state는 평가자에게 제시하는 private 작업 산출물이다. 파일명을 lane 정보가 없는
이름으로 유지하고, 평가자는 원본 run 파일이나 prepare 명령 기록을 보지 않는 것이 좋다.

## 5. 블라인드 평가와 봉인

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review serve-blind \
  --packet /candidate-evidence/private/blind-review-packet.jsonl \
  --state /candidate-evidence/private/blind-review-state.json \
  --port 8766
```

UI는 질문, 익명 left/right 답변, pseudonymous rater key와 기존 선택만 보여준다. 각 답변에
대해 다음 두 차원을 독립적으로 `left`, `tie`, `right` 중 선택한다.

- 자연스러움: 읽기 쉬움, 직접성, 불필요한 반복 여부
- 사실 완결성: 질문에 필요한 혜택, 조건, 제외, 숫자, 개정과 근거가 빠짐없이 있는지

두 답변 bytes가 같으면 두 차원 모두 `tie`만 허용한다. 완료 후 다음처럼 evaluator 호환
artifact를 봉인한다.

```bash
uv run --project apps/cardrag-mcp python -m cardrag_mcp.gold_review seal-blind \
  --gold /candidate-evidence/gold.jsonl \
  --baseline-run /candidate-evidence/v109_baseline.jsonl \
  --candidate-run /candidate-evidence/qwen_structure_exact.jsonl \
  --packet /candidate-evidence/private/blind-review-packet.jsonl \
  --state /candidate-evidence/private/blind-review-state.json \
  --output /candidate-evidence/blind-evaluation.jsonl
```

봉인 시에만 deterministic assignment를 다시 계산해 `candidate_position`을 합친다. gold/run
file SHA, query coverage, answer text SHA, rater별·전체 좌우 balance를 원점에서 재검사하고
기존 `load_blind_evaluation_jsonl`로 canonical output을 다시 읽는다. 답변이나 packet을
사후 교체하면 봉인이 중단된다.

`gold-review-state.json`, blind packet/state에는 credential을 넣지 않는다. 그래도 미완성
사람 평가와 익명화 정보를 담으므로 public release evidence와 분리해 백업하고 접근 범위를
검토자에게만 제한한다. 공개 evidence에는 승인된 `gold.jsonl`과 최종
`blind-evaluation.jsonl`만 포함한다.
