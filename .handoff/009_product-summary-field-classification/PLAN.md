# 009 — 상품 요약 필드 분류와 근거 정합성 개선

## 1. 목적과 완료 범위

`get_product_summary`가 약관에 있는 실질적 혜택·조건·연회비를 올바른 필드에 담도록 개선한다. 기준 사례는 우리카드 `500107` **카드의정석2 SOLID**다. 출시일/부가서비스 변경 고지문을 혜택처럼 반환하거나, “조건·한도 없이”라는 혜택 문구를 제한 조건처럼 반환하는 오류를 수정한다. 다른 상품에서도 같은 유형이 개선되는지 확인한다.

이번 문서는 Planner의 조사 결과와 Executor의 작업 지시다. 구현·수정 후 검증은 아직 수행하지 않았다. 완료 범위는 코드 수정, 재현/회귀 테스트, 동일 serving generation의 변경 전후 비교, 짧은 MCP 후보 실행 확인, REPORT 작성이다. 예약 Worker 실구동 2회·수일 대기·전량 재OCR·새로운 공개 릴리스는 인수 조건으로 추가하지 않는다.

## 2. 시작 상태와 보호 대상

- 저장소 `/home/lee/projects/MCP_card_prd_detail`, 계획 작성 기준 main `7c44f7031974dbd87927b0b2ae194185234f389d`.
- 최신 공개 릴리스 `v1.0.33`. 이번 계획으로 기존 릴리스 태그를 이동하거나 덮어쓰지 않는다.
- 계획 작성 전 추적 파일 수정 없음. 루트의 우리카드 Excel 파일 3개는 사용자 작업물이며 이번 과제에서 수정·삭제·자동 stage하지 않는다.
- 운영 current `/opt/cardrag/008-6b42a1a`, Worker 이미지 `cardrag-worker:008-6b42a1a`; MCP `cardrag-stable-v1026-mcp-1`, 이미지 `cardrag-mcp:007-31edb1d`, 준비 확인 `http://127.0.0.1:18015/health/ready`.
- Worker state `cardrag-worker-v130-candidate-state`; MCP state `cardrag-mcp-v129-candidate-state`; 인증 `cardrag-worker-v120-recovery-auth-20260910`; 모델 `cardrag-worker-paddleocr-models`. 운영 state의 DB/노드를 UPDATE하거나 전체 볼륨을 복제하지 않는다.
- 현 운영 OCR는 OpenCode다. 이번 과제는 이미 존재하는 serving 구조를 읽어서 해결하며 OCR/임베딩/LLM 호출이나 PDF 재수집을 필요로 하지 않는다.
- 롤백은 `/opt/cardrag/007-31edb1d`의 이미지·설정 1세트. 추가 대형 운영 사본을 만들지 않는다.
- 앞선 정리에서 릴리스 전용 Buildx 빌더를 삭제했다. 필요하면 기존 default 빌더로 후보 MCP만 빌드한다. Worker 이미지까지 재빌드하지 않는다.
- Executor 시작 시 Git/적용 AGENTS.md·GEMINI.md/현재 서비스 상태를 다시 확인한다. 구현의 사실 기준은 현 코드와 Git이며, 아래 generation/container 값은 조사 시점 기록이다.

## 3. 실제 재현과 보관된 근거

`evidence/current-summary-sample.json`은 실행 중 MCP 컨테이너의 **실제 `CatalogRepository.summaries`**를 호출한 변경 전 결과다. serving DB는 SQLite `mode=ro&immutable=1`로 읽었으며, 벡터 적재·서비스 재시작·외부 추론 없이 조사했다. 저장소 조사용 직접 호출이며 HTTP MCP 도구를 호출한 결과로 보고하지 않는다.

- generation `g-eba5ca0d13924abdb1f36937-71a5fd98d58b`, schema `cardrag.serving-db.v6`.
- lineage `lineage_65fa5275f6be984ed61dc4c725b5aab3eece5659064628145e48b235476dbfd4`.
- revision `revision_b7edb706176f9ddd019c3d5a9c4338e94c36f035d0fb1ea3f41faf5f398ac6b7`.
- PDF SHA `5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69`.
- source `https://pc.wooricard.com/upload/cardClause/2026/9/29/c1441ee5-c310-4177-b21f-f102d0f414a3.pdf`. 기존 OCR/노드로 조사 가능하므로 PDF 재취득이 필요 없다.

| 위치·노드 | 현재 분류/반환 | 문제와 기대 |
| --- | --- | --- |
| ordinal 9 MAJOR_SECTION, 10 PARAGRAPH “조건, 한도 없이 든든한 1.2% 할인” | MIXED. 단락만 condition으로 반환 | 1.2% 할인은 혜택. 제한이 존재한다고 해석하지 않고 benefit에 채택 |
| ordinal 12/18 FOOTNOTE “탄탄한 기본, 국내외 1.2% 할인”, “국내/해외 가맹점 1.2% 청구할인” | BENEFIT이지만 요약 추출 대상 밖 | 실제 주요 혜택으로 평가 대상에 포함 |
| ordinal 20 LIST_ITEM “전월 실적 조건 없음(할인한도 제한 없음)” | MIXED이지만 요약 추출 대상 밖 | 제한 면제 원문을 보존하고 실적 조건이 있다는 의미로 바꾸지 않음 |
| ordinal 129 MAJOR_SECTION “상품 출시일 및 부가서비스 변경 안내” | BENEFIT, benefit_headings에 반환 | 상품의 혜택 제목에서 제외 |
| 변경 안내 아래 휴업·파산·제공 3년 등의 단락 | benefit_summary_texts에 반환 | 법정/변경 고지를 혜택으로 반환하지 않음 |
| ordinal 80 TABLE/83 TABLE_ROW, 연회비 안내 아래 | 본인15,000원, 가족 없음의 원문 존재 | 올바른 annual_fee 근거 후보 |
| 제외 항목 “각종 수수료 및 이자, 연회비” | annual_fee_text로 잘못 채택 | 제외 목록을 연회비 금액으로 채택하지 않음 |

증거 JSON에 대상의 전체 structure_nodes, node_spans, 7페이지 원문과 30건의 변경 전 요약을 저장했다. node_id·페이지·parent_id·table_role을 사용하여 작은 테스트 fixture에 필요한 부분만 옮긴다. 운영 DB 전체를 Git에 넣지 않는다.

30건 탐색 표본은 대상500107, 다른 최근 우리 상품8건, 다른 issuer마다3건이며 전수 품질평가가 아니다. 이 표본에서 변경 고지가 benefit_headings에 들어가는 다른 사례도 확인했다.

- 우리 `104022` 우리 동행카드(신용), `104023` 우리 동행카드(체크).
- 신한 `00368` 신한카드 Deep Store: `부가서비스 변경안내`.
- BC 고위드 BC 바로 Mastercard: `2. ③: 부가서비스 변경일 6개월 이전`. 정확한 product_code/revision은 증거 JSON에 있다.

## 4. 원인 — 코드 확인 완료

1. Worker `apps/cardrag-worker/src/cardrag_worker/structure.py`의 `_major_class`는 단어의 존재를 사용한다. `할인`과 `조건/한도`가 공존하면 MIXED, `부가서비스 변경 안내`는 `서비스` 때문에 BENEFIT이 된다. 구조의 대분류만으로 요약의 의미를 확정할 수 없다.
2. MCP `catalog.py`의 `_summary`는 benefit을 BENEFIT에서만, condition을 NOTICE/MIXED + 조건 단어에서 추출한다. `없음/없이`라는 부정을 평가하지 않는다.
3. `get_contract_bundle`의 benefits scope는 `exact.py`에서 BENEFIT **및 MIXED**를 포함한다. 따라서 같은 원문이 bundle에서는 발견되고 summary의 benefit에서는 빠진다. 코드를 대조한 사실이며 Planner가 실제 HTTP bundle을 호출했다고 기록하지 않는다.
4. benefit의 허용 node_type에 FOOTNOTE/LIST_ITEM이 없어 500107의 핵심 설명을 빠뜨린다. summary 후보 조회도 UNKNOWN을 일반적으로 포함하지 않는다. 타입·클래스가 완벽하다고 가정하지 않아야 한다.
5. 제목 제외어에 변경 고지가 없고, 단락의 `제공` 등으로 법정문까지 benefit에 채택한다. 부모 절의 의미를 보지 않는다.
6. 연회비는 처음 만난 `연회비` 포함 텍스트를 고르므로 앞서 나온 제외 목록이 선택된다. 금액·표·제목 관계를 우선하지 않는다.
7. summary cache key는 출시일 분석용 `PARSER_VERSION`을 사용한다. 요약 분류 갱신을 나타낼 독립 버전이 필요하다.

## 5. 관련 파일과 구현 지침

### 5.1 필수 변경의 중심

- `apps/cardrag-mcp/src/cardrag_mcp/catalog.py`: `summaries`의 노드 조회, `_summary`의 의미 분류·선택·evidence·cache key.
- 필요하면 같은 패키지에 작은 summary 분류 모듈을 분리한다. 기존 API 필드와 모델을 유지한다.
- `apps/cardrag-mcp/src/cardrag_mcp/models.py`: ProductSummary/SummaryEvidence 호환성 유지. 변경이 불필요하면 수정하지 않는다.
- `apps/cardrag-mcp/src/cardrag_mcp/metadata_cache.py`: 기존 bounded JSON LRU를 사용한다. 새로운 영속/외부 캐시를 도입하지 않는다.
- `apps/cardrag-mcp/tests/test_metadata_tools.py`, `test_catalog_v122.py`, `v5_fixtures.py`와 필요한 summary 전용 테스트. v6의 기존 fixture/test도 참고한다.
- `apps/cardrag-mcp/src/cardrag_mcp/exact.py`: bundle 비교 대상. summary 개선을 위해 bundle scope의 의미를 바꾸지 않는다.
- Worker `structure.py`는 원인 확인과 재현 fixture에 사용한다. 기존 DB에 즉시 효과가 있는 MCP 개선을 우선한다. Worker 변경/재export가 불가피하면 이유와 최소 범위를 REPORT에 적으며 전량 OCR는 하지 않는다.

### 5.2 분류 규칙

major_class만으로 확정하지 않고, 노드 자체의 문장, 조상 제목, 원문 순서, table_role 등을 함께 사용한다. 최소한 실제 혜택, 실제 적용 조건, 제한 면제, 연회비, 법정/변경 고지, 참조만 있는 문장, 표 헤더를 구분하는 결정적 규칙을 만든다. LLM에 분류시키거나 500107의 상품코드를 판정 조건으로 사용하지 않는다.

- MIXED/NOTICE/UNKNOWN에 있는 명백한 혜택도 후보로 평가한다. 모든 major_class를 BENEFIT으로 바꾸지 않는다.
- `전월 30만원 이상 이용 시 1.2% 할인`처럼 진짜 조건이 있는 문장은 benefit과 condition 양쪽에 원문을 유지하여 채택할 수 있다. 단지 `한도/조건`이라는 단어 때문에 condition으로 분류하지 않는다.
- `조건, 한도 없이 … 할인`은 benefit이다. `실적 조건 없음/할인한도 제한 없음`이라는 면제 단독 문장은 조건 상태를 설명하므로 condition에 남겨도 된다. 다만 부정어를 삭제하거나 제한이 있다는 뜻으로 바꾸지 않는다. 문제의 혜택 제목 자체가 condition에 들어가는 오류는 해소한다.
- 문장에 `없음`이 하나 있으면 모든 조건이 없다는 규칙은 금지한다. `실적 조건 없이 월 1만원까지 할인`에는 월 상한이 있다. 절/인접 표현의 부정 범위를 처리한다.
- `서비스/제공`을 일률적으로 금지하지 않는다. 공항 라운지 무료 제공·보험/부가서비스 등 실제 혜택을 보존한다.
- `부가서비스 변경/축소/폐지`, 상품 출시·법정 통지, 휴폐업·수익성 설명은 문장과 부모 절을 보고 benefit 후보에서 제외한다. `변경` 단어만으로 모두 제거하지 않고 `할인 적용 조건 변경` 같은 실제 조건을 보호한다.
- FOOTNOTE/LIST_ITEM/TABLE_ROW의 실제 설명을 처리한다. 강조 문장이 FOOTNOTE여도 빠뜨리지 않는다. 빈 ITEM, 장식선, 표 헤더, `…유의사항 참고`뿐인 문장이 5건 한도를 채우지 않게 한다.
- `기준`이라는 단어만으로 실제 혜택을 제거하지 않는다. 금액·비율 숫자만 있어도 혜택으로 판단하지 않는다.
- 연회비는 올바른 절 아래 금액/면제 표현을 우선하고 제외 목록·반환 계산·이자/수수료 설명을 배제한다. 확정할 수 없으면 null을 허용한다. 500107에서는 본인15,000원 원문과 근거를 채택하고 가족 없음과 본인 면제를 혼동하지 않는다.

### 5.3 후보 조회, 정리, 근거

summary용으로 parent_id/raw_heading/table_role과 필요한 절 정보를 조회한다. parent_node_id라는 열은 존재하지 않는다. SQL 타입/클래스 제한을 바꾸더라도 요청된 revisions 범위로 한정하고 큰 structure_views/벡터를 전수 탐색하지 않는다. 조상 순환/결손에 대비하며 같은 revision의 관계인지 확인한다.

채택 판정 후 순위·중복 제거·기존 5건 상한을 적용한다. 원문 순서를 기본으로 하며 같은 제목/자식 단락 중복이 혜택 자리를 차지하지 않도록 한다. 원문의 비율·단위·상한·예외·부정을 변경하지 않는다. 새 할인율이나 설명을 만들어내지 않는다.

기존180자/300자 제한에서 조건이나 부정어가 끝에서 잘리는지 확인한다. 자르기를 바꾸면 적용 범위를 좁히고 완전한 절을 선택하는 등의 방법으로 의미 반전을 막는다. 새 API 필드 추가를 전제하지 않는다.

benefit/condition/annual_fee 반환 문장에는 필드에 맞는 실제 node_id, pages, revision의 evidence를 붙인다. benefit_headings에도 기존 field=`benefit`으로 해당 제목의 evidence를 붙여 텍스트만 맞고 근거는 다른 절인 상태를 방지한다. 출시일 resolver/lineage evidence의 기존 규칙은 유지한다.

summary 전용 `SUMMARY_CLASSIFIER_VERSION` 등을 도입하고 summary cache key의 읽기·쓰기 양쪽에 포함한다. 출시일 `PARSER_VERSION`은 별도로 유지한다. warm cache·동일generation에서 이전 분류를 다시 사용하지 않는지 검증한다.

## 6. 검증 — 작고 구체적으로

### 6.1 필수 회귀 fixture

증거에서 필요한 노드·원문span·부모 절을 추출하여 필요한 최소 크기의 격리 fixture를 만든다. 실제 DB 일부를 직접 수정하지 않는다. 다음을 assertion으로 검증한다.

1. 500107: 1.2% 문구가 benefit_summary_texts에 있고 주요 혜택 benefit_headings가 있다. 문제의 제목 문장은 condition_summary_texts에 없다. 변경 고지와 그 아래 법정 설명은 두 benefit 필드에 없다.
2. FOOTNOTE/LIST_ITEM의 국내외1.2%, 실적/한도 면제 의미를 유지한다. 본인15,000원에 올바른 annual_fee_text 근거가 있고 제외 항목의 연회비를 채택하지 않는다.
3. MIXED의 실제 조건부 혜택, 면제와 실제 상한이 함께 있는 문장, 실적 제외/할인 제외·월 상한·횟수 제한의 보존.
4. 진짜 서비스 혜택, 표 데이터 혜택/연회비와 표 헤더의 구분, 낮은 클래스/부모 결손, 중복·장문에서 부정/상한을 잃지 않음.
5. 원문에 없는 혜택을 반환하지 않고, 채택 문장과 evidence의 field/node/page/revision이 대응함.
6. 단품/배치(최대50·동일상품 중복·미존재 포함), v5/v6, warm cache, generation mismatch의 기존 동작 유지.
7. 동일 revision의 `get_contract_bundle(scope='benefits')`와 요약을 대조하여 요약 혜택을 원문 노드로 뒷받침함. bundle에 MIXED가 포함된다는 사실 자체를 의미 분류의 정답으로 취급하지 않는다.

### 6.2 실데이터 비교

저장된30건을 동일generation/revision에서 변경 전후 비교한다. 500107, 우리104022/104023, 신한00368, BC 위 사례는 각각 확인한다. 다른 issuer도 포함하여 기존 정상 혜택·조건이 사라지지 않았는지 원문과 대응하여 기록한다. 탐색 표본을 전체 상품 개선율로 보고하지 않는다.

추가 조사는 문제 패턴이 있는 current revisions의 노드로 후보를 좁힌 뒤 소수 원문을 확인한다. 전체5천건에 LLM을 호출하지 않는다. 비교 중 운영generation이 갱신되면 신구 결과를 섞지 않고 원래 로컬generation이 남아 있는지 확인한다. 없어졌으면 증거fixture로 비교하고 실제 최신generation 확인은 별도 표로 기록한다. 과거generation 복원을 위한 대형clone은 만들지 않는다.

후보 코드의 실제DB 비교는 MCP의 불변DB를 read-only로 읽어 CatalogRepository에 필요한 최소handle을 사용하면 벡터 적재 없이 가능하다. 읽는 동안generation이 고정되는지 확인한다. HTTP MCP 확인은 기존fixture/작은 후보 환경에서 initialize→tools/call을 수행하며 인증값은 로그에 넣지 않는다.

재현 수집 스크립트 `evidence/collect-baseline.py`도 함께 제공한다. 컨테이너 내부에서 기존 설치 모듈을 읽어 요약과 원문 근거를 JSON으로 출력하며 외부 API를 호출하지 않는다. 최신 운영 결과를 다시 확인하려면 아래처럼 새 임시 파일에 저장한다. 보관된 변경 전 증거 파일을 덮어쓰지 않는다. 후보 코드 검증에서는 이 스크립트가 운영 컨테이너의 기존 코드를 호출한다는 점에 유의하여 후보 import/실행 환경을 별도로 지정한다.

```sh
docker exec -i cardrag-stable-v1026-mcp-1 python - < .handoff/009_product-summary-field-classification/evidence/collect-baseline.py > /tmp/cardrag009-latest-baseline.json
```

### 6.3 실행할 기존 명령

기존uv 환경을 사용하고 dependency를 추가/갱신하지 않는다. 예시:

```sh
uv run --frozen pytest apps/cardrag-mcp/tests/test_metadata_tools.py apps/cardrag-mcp/tests/test_catalog_v122.py
# 새 summary 테스트도 위 명령에 추가
uv run --frozen pytest apps/cardrag-mcp/tests
uv run --frozen ruff check apps/cardrag-mcp
uv run --frozen ruff format --check apps/cardrag-mcp
```

타입 검사는 현재CI/저장소 설정에 따른다. Worker를 수정했을 때만 관련structure 테스트를 추가한다. 전체CI는 최종 코드의 통상 게이트로 수행하되 불필요하게 반복하지 않는다. 준비 상태만으로 분류 품질이 검증됐다고 보고하지 않는다.

## 7. 인수 기준

- 동일generation/revision의500107에서 1.2% 혜택이benefit에 들어가며 문제 문장의condition 오분류와 변경 고지의benefit 혼입이 해소된다.
- 500107 연회비 오채택이 해소되고 기존 원문/evidence로 금액을 뒷받침한다.
- 3issuer에 걸친 위 추가 사례의 잘못된 제목도 수정되고 정상 혜택·조건·예외가 유지된다.
- 면제의 부정과 실제 조건을 테스트로 구분하며 같은 단어가 있는 정상 설명을 일률적으로 삭제하지 않는다.
- node_type/major_class만으로 발생한 누락을 개선하고 기존 필드schema/5건 상한/단품·배치/generation 정합성을 유지한다.
- summary cache의 버전 갱신, 테스트, 실제 데이터 변경 전후, 작은 후보MCP 호출 결과를 구체적으로 저장한다.
- 운영 Worker·OCR·CAS·WebDAV 데이터를 수정하거나 장시간 추가 배치를 실행하지 않는다. 새 운영 전환/공개 릴리스는 개선 리뷰 후 판단한다.

## 8. Executor REPORT 필수 내용

이 디렉터리의 다음 미작성artifact `REPORT.md`에 시작/종료Git 상태, 변경 파일/commit, 분류 규칙과 선택 이유, 500107 전후 필드와node/page 근거, 30건/추가 사례 차이, 테스트 명령/건수/실패 수정, cache/version, HTTP 호출의 실제 수행 범위, MCP/Worker 변경 여부, 운영 비파괴 확인, 남는 애매한 사례를 적는다. Planner의 탐색 결과와 Executor의 수정 후 실측을 구분한다. 기존PLAN/evidence/과거handoff를 덮어쓰지 않는다.
