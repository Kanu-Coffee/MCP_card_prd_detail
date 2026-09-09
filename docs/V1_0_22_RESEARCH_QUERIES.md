# v1.0.22 조사·비교 조회

## 목적과 범위

반복적인 상품별 요약 호출, 출시일이 없는 상품의 재조사, 단일 상품 검색에서 대형 계약
전체가 반환되는 문제를 줄인다. 기본 MCP 도구 이름은 기존 12개를 유지한다. LibreChat
소스·설정·프롬프트를 바꾸지 않고 기존 인자를 계속 지원한다. Worker는 버전 표시만
변경하며 실행 중인 수집·OCR, 스케줄, WebDAV generation 형식은 변경하지 않는다.

## 조회 흐름

1. `find_products(mode="coverage", issuers=["KB국민카드", "신한카드"])`로 현재
   generation에 실제 적재된 범위와 출시일 확인 현황을 받는다. 지원 카드사와 적재
   카드사는 별개이며 전체 시장의 완전성을 뜻하지 않는다.
2. `list_recent_products(start_date="2026-07-27", end_date="2026-08-02",
   issuers=["kb", "shinhan"], limit=50)`로 확정된 출시만 조회한다. 날짜는 양 끝 포함이며
   `months`와 함께 보낼 수 없다. `issuer`와 `issuers`도 상호 배타적이다.
3. 다음 페이지에는 동일한 필터와 반환된 `cursor`를 보낸다. 후속 요약·검색에는
   `expected_generation_id`를 지정한다. generation이나 커서 조건이 달라지면 명시 오류로
   재시작을 요구하며 결과를 조용히 섞지 않는다.
4. `get_product_summary(products=[{"issuer":"kb", "identifier":"상품코드"},
   {"issuer":"shinhan", "identifier":"상품코드"}], expected_generation_id="...")`로
   최대 50개를 한 generation 안에서 조회한다. 결과는 요청 순서이며 미존재 상품은
   `null`이다. 단건의 미존재 상품은 기존처럼 오류다. 모호한 이름은 후보 식별자를
   반환하는 명시 오류이므로 코드나 lineage ID로 다시 요청한다.
5. 상세 근거가 필요하면 `search_contracts(query="연회비와 적립 조건",
   product_lineage_ids=["...", "..."], response_mode="compact", limit=10,
   expected_generation_id="...")`를 사용한다. 복수 lineage는 최대 100개이며 단일
   `product_lineage_id`와 동시에 지정할 수 없다. 출시 기간과 혜택의 교집합은
   `launch_start_date`와 `launch_end_date`를 함께 지정해 점수 계산 전에 적용한다.

날짜 범위 대신 기존 `months` 호출도 유효하며 인자 없이 호출하면 최근 3개월이다.
`find_products`의 기본 `mode="search"`에는 비어 있지 않은 keyword가 필요하다.
`mode="catalog"`는 keyword 없이 상품을 페이지 단위로 열거한다. 이름과 출시일 정렬을
지원한다. v4 generation은 출시일 지원 여부를 명시하며 확인되지 않은 날짜를 만들어내지
않는다. `effective_date`는 문서 개정일이고 `launch_date`의 대체값이 아니다.

## 응답과 검색 계약

`search_contracts`의 `mode`는 `exact` 또는 `exhaustive`, `response_mode`는 `auto`,
`full`, `compact`이다. 기존 `auto` 경로와 exact 점수·순위 계약을 보존한다. compact는
완결된 조항·표·연결 근거 그룹만 포함하고 제외된 그룹 및 후속 조회 식별자를 기록한다.
64 KiB는 검색 결과 JSON의 보수적인 ASCII/들여쓰기 직렬화 기준이며 JSON-RPC 봉투나
MCP의 structured/text 중복 필드까지 포함한 HTTP 전송 크기를 의미하지 않는다.
단일 근거 그룹도 제한보다 크면 일부 문장을 잘라 완전한 근거처럼 반환하지 않는다.
전체 계약은 `get_contract_bundle` 또는 명시적인 `response_mode="full"`로 조회한다.

메타데이터 캐시는 generation과 조회 조건으로 결속하고 최대 16 MiB·1,024개 항목으로
제한한다. DB 연결이나 generation pin을 캐시에 보존하지 않는다. 요약에 lineage,
revision, document, 출처 및 근거 참조를 제공해 같은 상품을 반복 탐색할 필요를 줄인다.

인증된 `/metrics`는 도구별 성공·실패·취소, 실행 시간, MCP 결과 직렬화 바이트와 캐시
hit/miss/eviction/사용량을 제공한다. 질의문, 상품명, 사용자 정보는 metric label에 넣지
않는다. Compact의 JSON 제한과 MCP 봉투를 포함한 관측 바이트는 서로 다른 지표다.

## ResearchOps 동반 적용

ResearchOps v0.2.1은 opt-in Task에 확정한 조사 날짜·카드사·series를 immutable 입력으로
전달하고, 검증된 전송 이력을 별도 파일로 전달한다. 앱이 근거 없이 기존 prepared를
발송 완료로 간주하거나 모델이 주장한 성공을 receipt로 인정하지 않는다. 상세 계약과
검증은 해당 저장소의 v0.2.1 문서에 둔다. 실제 task 재실행과 메일 발송은 검증에 포함하지
않는다.

## 적용과 복구

MCP만 검증된 소스 커밋의 로컬 이미지를 빌드해 교체한다. 실행 중인 OCR Worker의
container ID, image, 시작 시각, restart count와 설정 해시를 전후 비교한다. Worker
설치 pointer·volume·인증·timer는 유지한다. 기존 MCP 컨테이너와 이미지를 롤백용으로
보존하며 같은 generation과 기존 인증·volume·접근 경로를 유지한다.

ResearchOps는 신규 예약을 잠시 막고 실행·발송이 자연 종료해 idle인 것을 확인한 뒤
백업과 별도 설치본 검증을 거쳐 교체한다. 실행 중인 모델 child를 종료해서 idle을 만들지
않는다. Task 변경은 예상 활성 hash를 확인하고 새 immutable version으로 적용한다.
기존 task·archive·라우팅·예약 OFF 설정은 유지한다. 공개 Git에는 운영 DB, 원본 질의,
배포 비밀정보나 조사 원문을 포함하지 않는다.
