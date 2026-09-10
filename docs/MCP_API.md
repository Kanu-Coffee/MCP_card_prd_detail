# MCP API

서버는 Streamable HTTP `/mcp`와 Bearer 인증을 사용합니다. 특정 클라이언트나 조사
애플리케이션에 의존하지 않습니다. 클라이언트에 HTTPS 서버 주소와 인증 헤더를 설정하고
`initialize`, `tools/list`, `tools/call` 순서로 연결합니다. 도구의 최신 인자 schema는
`tools/list`가 기준이며 아래 예제는 도구 호출 인자를 읽기 쉽게 표현한 것입니다.

## 기본 도구

| 도구 | 용도 |
|---|---|
| `find_products` | 상품명 검색, 페이지별 목록, 카드사·출시일 적재 범위 |
| `list_recent_products` | 확정 출시일 기준 기간 조회 |
| `get_product_summary` | 단일 또는 최대 50개 상품 요약 |
| `find_cards_by_merchant` | 혜택 조항의 가맹점·브랜드 언급 검색 |
| `search_contracts` | 계약 근거의 exact 검색 또는 exhaustive 감사 |
| `get_contract_bundle` | 계약의 조항·표·주석과 연결 근거 |
| `list_product_revisions` | 상품의 계약 개정 목록 |
| `search_evidence` | 근거 검색 |
| `get_evidence` | 식별자로 근거 조회 |
| `get_product` | 카드사와 상품코드로 상품 조회 |
| `get_source_pdf` | 출처 PDF descriptor |
| `get_source_page` | 출처 페이지 조회 |

`issuer`는 `woori`, `kb`, `shinhan`, `samsung`, `hyundai`, `hana`, `lotte`, `bc` 중 하나입니다.
`KB국민카드`, `국민카드`, `KB Card` 등 지원 별칭을 canonical 코드로 변환합니다. 모르는
카드사명은 명시 오류를 반환합니다. 오타를 0건으로 처리하지 않습니다. 복수 카드사를 받는
도구에서 `issuer`와 `issuers`를 동시에 보내면 오류입니다.

## 조사 범위를 먼저 확인하기

```text
find_products(mode="coverage", issuers=["KB국민카드", "신한카드"])
find_products(mode="catalog", issuers=["kb", "shinhan"], sort="name", limit=50)
find_products(mode="search", keyword="상품명 일부", issuer="kb")
```

`mode="search"`는 비어 있지 않은 keyword가 필요합니다. 이름은 Unicode NFKC와 대소문자
정규화를 적용합니다. `catalog`는 keyword 없이 나열하며 `sort="name"` 또는
`sort="launch_date"`를 지원합니다. `coverage`는 지원 카드사와 실제 적재 카드사,
출시일을 확인한 상품과 확인하지 못한 상품을 구분합니다. 전체 시장을 빠짐없이 수집했다는
의미가 아닙니다. 발급이 끝난 상품도 공식 최신 안내장이 있으면 포함될 수 있습니다.

## 출시 기간과 날짜

```text
list_recent_products(months=3, issuers=["kb", "shinhan"], limit=20)
list_recent_products(start_date="2026-01-01", end_date="2026-01-31",
                     issuers=["kb", "shinhan"], limit=50)
```

출시는 **`launch_date`만** 사용합니다. `effective_date`는 일반적으로 문서 개정일이며
source의 `date_basis`가 실제 의미를 결정합니다. BC의 `product_launch` 같은 예외도
별도 출시일 검증을 대신하지 않습니다. 출시일이
없거나 잘못되었거나 충돌하면 추정하지 않고 `[확인 필요]`로 표시하십시오.
`unknown_launch_date_count`는 해당 카드사 범위의 현재 상품 중 출시일 미확인 수이며,
최근 출시 수에 더할 수 없습니다.

명시 날짜는 양 끝 포함 `YYYY-MM-DD`이고 시작·종료를 함께 지정합니다. `months`와
동시에 지정할 수 없습니다. 인자가 없으면 최근 3개월, `months`는 1–120입니다. 월 단위
기간은 오늘의 일자를 기준으로 계산하고 해당 월에 그 날짜가 없으면 월말로 맞춥니다.
오늘은 한국 표준시 UTC+09:00으로 계산합니다. 미래 종료일과 역전된 범위는 거부하며,
미래 출시도 포함하지 않습니다. 날짜 정보가 없는 구 schema에는 지원 한계를 표시합니다.

## 페이지와 generation 연결

목록 호출에서 `limit`는 1–100입니다. 다음 페이지는 응답의 `next_cursor` 값을 요청 인자 `cursor`에 넣고 **같은 필터**를
전달합니다. 현재 generation이 바뀌었거나 필터를 바꾸면 커서를 다시 시작해야 합니다.
서로 다른 데이터 묶음을 조용히 합치지 않습니다.

여러 호출로 보고서를 만들 때 첫 응답의 `generation_id`를 이후 지원 도구의
`expected_generation_id`에 지정하십시오. 다른 generation이면 명시 오류로 중단합니다.
이 값은 이전 generation을 임의로 다시 여는 기능이 아니며, 장기간 재현은 원본
응답·출처·generation 식별자를 외부에서 보존해야 합니다.

## 요약과 비교

```text
get_product_summary(issuer="kb", identifier="정확한 상품코드")
get_product_summary(products=[{"issuer":"kb", "identifier":"상품코드A"},
                              {"issuer":"shinhan", "identifier":"상품코드B"}],
                    expected_generation_id="앞서 받은 generation_id")
search_contracts(query="연회비, 전월 실적과 적립 제외 조건",
                 product_lineage_ids=["앞서 받은 lineage A", "앞서 받은 lineage B"],
                 response_mode="compact", limit=10,
                 expected_generation_id="앞서 받은 generation_id")
```

일괄 요약은 최대 50개이고 요청 순서를 유지합니다. 없는 상품은 일괄 응답에서 `null`,
단건에서는 오류입니다. 모호한 이름에는 후보 식별자가 포함된 오류를 반환하므로 정확한
코드 또는 lineage ID로 요청하십시오. 요약은 날짜 상태, document·revision·lineage와
근거·출처 참조를 제공합니다.

복수 비교 검색은 `product_lineage_ids` 최대 100개를 지원하며 단일
`product_lineage_id`와 함께 보내면 오류입니다. `launch_start_date`와 `launch_end_date`를
함께 지정하면 확정 출시일로 대상을 먼저 제한하고 점수를 계산합니다.
`as_of`와 `include_history`는 계약 개정의 시점·이력 선택입니다. 두 인자는 서로
배타적이며, 출시일 기간 필터와 함께 지정할 수도 없습니다.

## 응답 크기와 근거

`search_contracts`의 `response_mode`는 `auto`, `full`, `compact`입니다. `compact`는
연결된 조항·표·적용 조건·주석을 완결된 그룹 단위로 포함하고, 빠진 그룹과 후속 조회
식별자를 기록합니다. 큰 근거의 중간 문장만 잘라 완전한 조항처럼 반환하지 않습니다.
전체 계약 구조는 `get_contract_bundle(scope="full")`로 조회합니다. 검색의
`response_mode="full"`도 node·문자·응답 예산을 넘으면 근거 bundle로 fallback하거나 일부
결과를 생략할 수 있으므로 `full_contract_fallback_count`, `response_truncated`를 확인합니다.

Compact의 64 KiB 한도는 검색 결과 JSON의 보수적 ASCII·들여쓰기 직렬화 기준입니다.
JSON-RPC 봉투와 MCP의 text/structured 중복 필드를 포함한 HTTP 전송량 한도가 아닙니다.
MCP 메타데이터 캐시는 generation·revision·source·parser에 결속되고 16 MiB·1,024개 항목
이내에서 관리됩니다. DB 연결이나 generation pin을 캐시에 보존하지 않습니다.

`mode="exact"`는 요청 필터에 포함된 활성 구조 view를 모두 exact 점수화합니다. `mode="exhaustive"`는
제한된 단계로 진행하는 지속 감사 작업이며, 같은 요청을 반복해 `running`에서
`complete`까지 진행합니다. 미완료 결과를 전체 조사 결과로 사용하지 마십시오.
Exhaustive는 issuer·상품 ID·출시 기간·as_of·이력 필터와 `response_mode`의 full/compact를
지원하지 않으며 이러한 조합은 오류입니다.
자세한 검색·구조 계약은 [DATA_FORMATS](DATA_FORMATS.md)를 참고하십시오.

## 클라이언트의 표시 원칙

출처 PDF와 페이지, 문서 개정일, 출시일의 확인 상태를 구분해 표시합니다. `ocr_failed`,
`unsupported_drm`, 출시일 미확인, 적재 범위와 누락 진단을 결과에서 숨기지 않습니다.
카드사명 오류·상품명 모호성·generation 변경은 입력을 바로잡거나 새 조회를 시작할
상황이며, 빈 결과나 성공으로 바꾸지 않습니다. 원문 질의와 결과 저장·예약·전송 정책은
사용하는 클라이언트의 책임입니다.
