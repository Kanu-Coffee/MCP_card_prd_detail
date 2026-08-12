# LLM 및 데이터 품질 정책

## 1. 문서 목적

이 문서는 PDF 수집 이후 OCR, 구조 분석, 임베딩·검색과 MCP 근거 제공 과정에서 정보 손실과 환각을 방지하기 위한 정책을 정의한다. 모델의 편의나 처리 완료율보다 카드상품 조건을 원문 그대로 보존하고 검증 가능하게 만드는 것을 우선한다.

이 문서에서 정한 것은 구현 완료 상태가 아니다. 모델명, 수치 threshold와 평가 표본 크기처럼 실측이 필요한 기술값은 Codex가 개발 중 gold set과 benchmark로 결정하고 ADR에 근거를 남긴다.

## 2. 품질 목표와 신뢰 원칙

### 2.1 품질 우선순위

1. 실적·혜택 제외조건, 한도, 기간, 숫자와 부정 표현을 잃지 않는다.
2. 모든 파생 정보와 MCP 근거를 원본 PDF 및 OCR 위치로 역추적할 수 있게 한다.
3. 불확실하거나 상충하는 내용을 그럴듯하게 하나로 합치지 않는다.
4. 같은 입력·정책의 결과를 재현하고 모델 변경의 품질 회귀를 측정할 수 있게 한다.
5. 위 조건을 만족한 뒤 처리량, 지연과 비용을 최적화한다.

### 2.2 신뢰 가능한 사실의 순서

원본 PDF가 최상위 근거이고, 검증된 OCR은 그 검색 가능한 전사본이다. 결정론적 구조 후보, LLM 구조 보강, 임베딩과 검색 순위는 모두 파생 데이터다.

- LLM 결과를 원본 문구로 가장하지 않는다.
- LLM 결과가 OCR과 충돌하면 OCR을 우선하고 충돌을 기록한다.
- OCR이 PDF와 충돌하거나 판독 불가하면 PDF를 기준으로 재검토한다.
- 정규화 값과 요약은 정확한 원문 인용과 분리한다.
- 파생 계층을 삭제하거나 다시 만들어도 PDF와 승인된 OCR은 변하지 않아야 한다.

## 3. Codex exec와 OpenRouter 역할

### 3.1 작업별 호출 정책

| 작업 | 우선 실행 | 페일오버 | 정책 |
|---|---|---|---|
| 초기 대량 OCR | Codex CLI의 `codex exec` | 없음 | 실패 문서는 재시도 또는 검토 대기로 남기며 OpenRouter로 자동 전환하지 않음 |
| 일일 신규·변경 OCR | `codex exec` | OpenRouter | 승인된 Codex 재시도 예산 소진 후 문서 단위 전환 가능 |
| LLM 구조 분석·검증 | `codex exec` | OpenRouter | 결정론적 구조 후보는 항상 먼저 만들고 LLM은 보강에만 사용 |
| 문서 및 query 임베딩 | OpenRouter 임베딩 전용 모델 | 다른 모델로 자동 전환 없음 | 동일 index generation 안에서 모델·차원을 혼합하지 않음 |
| MCP 근거 반환 | 게시된 검색 index | 해당 없음 | MCP 자체가 근거 없는 답을 생성하는 LLM 역할을 맡지 않음 |

초기 대량 OCR을 Codex-only로 제한하는 이유는 하나의 대규모 기준 corpus 안에서 backend 차이로 인한 품질 편차를 줄이고, 실패를 숨기지 않은 채 재현 가능한 baseline을 만들기 위해서다.

### 3.2 요청 우선순위

외부 호출 자원은 다음 순서로 배분한다.

1. 초기 기준 corpus에서 실패 후 승인된 재처리 대상
2. 일일 신규·변경 문서 OCR
3. 게시 예정 generation의 구조 검증
4. 게시 예정 generation의 문서 임베딩
5. 회귀 평가, 후보 모델 비교와 비운영 실험

온라인 MCP query embedding은 사용자 요청 지연에 직접 영향을 주므로 오프라인 큐와 별도의 quota·동시성 경계를 가져야 한다. 온라인과 오프라인의 구체 quota 및 우선순위 수치는 Codex가 BULK·부하 시험으로 결정한다.

### 3.3 페일오버 원칙

- timeout 한 번만으로 즉시 provider를 바꾸지 않는다. 오류 분류별 승인된 재시도와 backoff를 먼저 적용한다.
- 일일 OCR과 구조 분석의 페일오버는 문서 단위로 수행한다. 부분 Codex 결과와 OpenRouter 결과를 한 문서에 혼합하지 않는다.
- 이미 일부 페이지가 Codex로 성공했더라도 OpenRouter 또는 다른 model로 전환해야 하면 전체 문서를 새 attempt로 다시 처리한다. 서로 다른 provider·model의 결과를 한 문서에 혼합하지 않는다.
- 페일오버 결과는 primary와 동일한 OCR·구조 schema 및 품질 검사를 통과해야 한다. 제공자 전환 자체를 성공으로 간주하지 않는다.
- OpenRouter 임베딩에서 현재 모델을 사용할 수 없다고 다른 모델로 자동 변경하지 않는다. 작업을 보류하거나 승인된 새 generation을 전체 재임베딩한다.
- 동일 모델을 여러 OpenRouter provider가 제공할 때 provider pinning과 호환 provider 간 전환 정책은 Codex가 실제 가용성 시험으로 정한다. 전환 시에도 출력 차원과 회귀 품질을 검증한다.
- circuit breaker, 재시도 횟수, timeout 및 backoff 수치는 Codex가 오류 주입·부하 시험으로 정한다.

### 3.4 호출 provenance

모든 LLM·임베딩 산출물에는 다음을 재현 가능한 범위에서 기록한다.

- 논리적 작업 종류와 문서 버전
- 실제 backend와 실제 호출 모델 식별자
- provider 또는 route 정보
- prompt·출력 schema·전처리 정책 버전
- 입력 content hash와 출력 content hash
- 실행 시각, attempt와 종료 상태
- token 또는 사용량 정보와 latency
- fallback 여부와 원인

설정 파일의 모델 label만 기록해서는 안 된다. 레거시처럼 metadata에 적힌 모델과 실제 CLI 호출 모델이 다를 가능성을 제거하기 위해 실제 실행 결과에서 식별 가능한 모델을 검증해야 한다. 식별이 불가능한 호출은 provenance 불완전 상태로 표시하고 운영 corpus 게시 여부를 검토한다.

## 4. 모델 선택과 교체 정책

### 4.1 논리적 모델 역할

모델명은 코드 여러 곳에 직접 고정하지 않고 역할별 설정으로 관리한다.

| 역할 | 모델 | 상태 |
|---|---|---|
| Codex OCR primary | `구현 중 Codex 결정` | 한글 문서·표·이미지 충실도 평가 후 선정 |
| OpenRouter OCR fallback | `구현 중 Codex 결정` | 일일 증분 fallback 전용 후보 평가 |
| Codex 구조 분석 primary | `구현 중 Codex 결정` | schema 준수와 근거 범위 연결 평가 |
| OpenRouter 구조 분석 fallback | `구현 중 Codex 결정` | primary와의 회귀·편차 평가 |
| OpenRouter embedding | `구현 중 Codex 결정` | 검색 benchmark와 운영 안정성 평가 |

레거시의 `qwen/qwen3-embedding-8b`, 4,096차원 데이터는 재사용 가능성을 평가할 기존 자산이지 신규 기본 모델 확정 근거가 아니다.

### 4.2 OCR·구조 모델 선택 기준

- 한글과 영문·숫자가 혼합된 카드 안내장 인식 정확도
- 복잡한 표, 다단 편집, 작은 각주와 반복 머리글 처리 능력
- 금액·비율·기간·부정 표현의 정확성
- 페이지 순서 및 schema 출력 준수율
- 근거 없는 보완·요약 성향
- 긴 문서에서의 일관성과 동일 입력 재현성
- 실제 모델·버전 provenance 확인 가능성
- rate limit, 가용성, latency와 운영 정책

비용은 품질과 안정성 조건을 충족한 후보 사이에서 비교한다.

### 4.3 모델 교체 절차

1. 기존 gold sample을 고정하고 후보 모델의 독립 결과를 만든다.
2. OCR, 구조, 숫자, 제외조건 및 검색 지표를 기존 모델과 비교한다.
3. 개선된 평균 점수뿐 아니라 카드사·레이아웃별 최악 구간과 중대한 오류를 확인한다.
4. 승인 기준을 통과하면 새 정책 버전과 별도 generation을 만든다.
5. canary 검증과 rollback 준비 후 게시한다.
6. 성공 generation 최근 3개와 수동 pin generation을 유지한다. 실패 candidate는 7일 보존한다.

새 모델 결과를 기존 OCR 또는 vector row에 부분 덮어쓰기하지 않는다. canary 범위와 rollback 시간 목표는 Codex가 위험도와 rehearsal 결과로 정하고, 보존 기간은 성공 최근 3개·실패 candidate 7일·수동 pin은 unpin 전까지로 적용한다.

## 5. OCR 충실도 정책

### 5.1 보존해야 할 요소

OCR은 다음 요소를 정보 단위로 보존한다.

- 페이지 경계, 읽기 순서와 제목 계층
- 상품명, 상품코드와 연회비 안내
- 혜택명, 대상, 금액·비율, 횟수, 기간과 한도
- 혜택 적용 조건과 전월실적 조건
- 실적 제외조건과 혜택 제외조건
- 필수 안내사항과 유의사항
- 표의 제목, 행·열 머리글, 셀 값과 병합·연속 관계
- 각주 기호, 각주 문구와 적용 대상
- 통화, 백분율, 부등호, 범위, 날짜와 단위
- `제외`, `미포함`, `불가`, `이상`, `초과`, `미만` 같은 의미를 뒤집을 수 있는 표현

### 5.2 금지하는 OCR 동작

- 원문을 짧게 요약하거나 자연스러운 표현으로 고쳐 쓰기
- 반복처럼 보인다는 이유로 조건·각주를 임의 삭제하기
- 표를 단순 문장으로 바꾸면서 행·열 관계를 잃기
- 판독할 수 없는 숫자나 단어를 문맥으로 추측하기
- 서로 다른 페이지의 문장을 근거 없이 결합하기
- 혜택 조건과 제외조건의 순서를 바꾸거나 긍정문으로 변환하기
- 단위를 임의 환산하거나 금액·비율 형식을 정규화하기

불명확한 문자는 불확실 상태와 페이지 위치를 표시한다. 후속 구조 분석기가 추측으로 확정하지 못하도록 machine-readable한 품질 상태를 함께 전달해야 한다. 구체 표기법은 Codex가 schema와 round-trip 시험으로 결정한다.

### 5.3 표 보존

Markdown 표로 표현할 수 있으면 머리글과 셀 대응을 유지한다. 병합 셀, 페이지를 넘는 표 또는 복잡한 시각적 그룹을 단일 Markdown 표로 안전하게 표현할 수 없으면 다음을 함께 보존한다.

- 표 제목과 원래 페이지 범위
- 머리글 계층과 행 식별 정보
- 읽은 각 셀의 원문
- 병합·계속·각주 관계
- 불확실하거나 누락된 셀의 위치

표를 읽기 쉽게 다시 작성하는 것보다 관계 손실을 명시하는 편을 우선한다. 원본 페이지 또는 렌더 이미지로 재검토할 연결을 유지한다.

### 5.4 각주와 제외조건

- 각주 기호를 제거하지 않고 대상 문구와 연결한다.
- 같은 페이지가 아닌 곳에 각주가 있어도 문서 내 연결 후보를 남긴다.
- `실적 제외`와 `혜택 제외`를 같은 범주로 합치지 않는다.
- 공통 유의사항이 여러 혜택에 적용되는 경우 적용 범위를 좁혀 추정하지 않는다.
- 제외조건이 존재하는 원문 block을 chunking 과정에서 혜택 본문과 완전히 단절시키지 않는다.

## 6. 구조 분석 품질 정책

### 6.1 결정론적 기준선 우선

구조 분석은 먼저 OCR만으로 페이지, heading, 문단, 목록, 표, 각주와 원문 범위 후보를 결정론적으로 만든다. 이 결과는 LLM 호출이 실패해도 유지되어야 한다.

LLM은 제한된 schema 안에서 다음만 보강한다.

- section 분류가 애매한 block의 후보 분류
- 혜택과 이용조건·한도·제외조건의 관계 후보
- 표와 각주의 적용 범위 후보
- 규칙 분석 결과의 모순 또는 누락 검토

### 6.2 LLM 결과 제약

- 모든 LLM 사실·관계는 입력 OCR에 존재하는 범위를 참조해야 한다.
- 인용문은 모델이 다시 쓴 문장이 아니라 참조 범위에서 가져온 원문이어야 한다.
- schema에 없는 자유 설명을 운영 데이터 사실로 저장하지 않는다.
- 원문에 없는 숫자, 조건 또는 상품 속성이 있으면 결과를 거부한다.
- LLM confidence만으로 게시하지 않고 구조 검증과 gold 평가를 통과해야 한다.
- 규칙과 LLM 결과가 다르면 원문 대사 후 해결하며, 미해결 충돌을 숨기지 않는다.
- 모델이 `정보 없음`으로 판단한 경우에도 규칙 후보와 OCR 원문을 삭제하지 않는다.

### 6.3 구조 분석 검증

다음 자동 검사를 최소 기준으로 둔다.

- 참조 문서·페이지·원문 범위의 존재와 경계
- 인용문과 원문 범위의 hash 또는 exact match
- 숫자·단위·부정 표현의 원문 포함 여부
- 부모 section, 표와 각주 관계의 참조 무결성
- schema 필수값, enum 및 중복 관계
- 동일 사실에 대한 모순된 분류
- 핵심 section과 제외조건 후보의 비정상적 급감

taxonomy와 schema, 자동 confidence threshold는 Codex가 gold set 회귀 결과로 결정한다.

## 7. 임베딩 및 검색 품질 정책

### 7.1 임베딩 모델 선택 기준

- 한국어 카드상품 용어와 숫자·조건 검색 성능
- 짧은 질의와 긴 근거 block 사이의 비대칭 검색 성능
- 혜택명보다 조건·제외·한도 질의의 recall
- 지원 입력 길이와 긴 표·각주 처리 특성
- 출력 차원, 정규화 방식과 결과 안정성
- OpenRouter에서의 제공 안정성, rate limit과 provider 일관성
- batch 처리량과 online query latency
- 버전 고정, 사용 중단 통보와 재현 가능성

모델 선정은 공개 benchmark가 아니라 이 프로젝트의 gold query로 수행한다. 후보 모델, 최종 모델, 차원 및 입력 prefix 정책은 Codex가 프로젝트 benchmark로 결정한다.

### 7.2 임베딩 입력 품질

- 검색 단위는 원문 범위와 구조 metadata를 잃지 않는다.
- 혜택 본문과 조건, 한도, 제외조건이 함께 필요한 질의를 고려한다.
- 지나치게 큰 section은 의미 경계로 나누되 부모·인접 context를 연결한다.
- 표 머리글 없는 셀 값이나 대상 없는 각주만 단독 임베딩하지 않는다.
- 모델 입력용 제목·metadata 보강은 원문과 구분하고 버전을 기록한다.
- 같은 내용의 문서와 query는 호환되는 동일 모델 및 입력 규칙을 사용한다.

### 7.3 vector 무결성

- 요청 항목 수와 응답 vector 수가 일치해야 한다.
- 모든 vector의 차원이 generation manifest와 일치해야 한다.
- NaN, 무한값, 비정상적으로 빈 vector를 거부한다.
- 원문 content hash, 모델, 차원과 입력 정책을 vector에 연결한다.
- 다른 모델·차원 또는 과거 content hash의 vector를 같은 활성 index에 혼합하지 않는다.
- 완료율은 전체 row 수가 아니라 현재 게시 대상 content hash와의 정확한 coverage로 계산한다.
- query vector를 section·문장 검색마다 중복 생성하지 않고 한 요청 안에서 재사용한다.

### 7.4 검색 평가

검색 품질은 최소 다음 관점에서 평가한다.

- 기대 근거가 상위 결과에 포함되는 비율
- 첫 번째 관련 근거의 순위
- 여러 관련 근거의 순위 품질
- 카드사, 상품, section, 버전 filter 정확성
- 같은 상품코드의 카드사 간 충돌 여부
- 혜택·조건·제외조건과 각주를 함께 회수하는 능력
- 이전 generation 대비 회귀율
- lexical-only, vector-only와 hybrid의 실제 비교

사용할 지표에는 Recall@K, MRR, nDCG@K 및 filter 정확도를 포함한다. K 값, 합격 threshold와 지표별 가중치는 Codex가 실제 사용 질의와 baseline으로 결정한다.

hybrid 검색은 lexical chunk ID와 structured ID를 그대로 섞지 않고 공통 evidence ID로 결합한다. 가중치는 실제 gold query 평가로 정하며 레거시 값을 승계하지 않는다.

### 7.5 임베딩 장애와 모델 교체

- OpenRouter가 일시적으로 불가하면 문서 임베딩 작업을 재시도 대기로 두고 현재 게시 generation을 유지한다.
- 다른 임베딩 모델을 즉시 대신 사용해 부분 coverage를 채우지 않는다.
- 모델 변경은 전체 새 generation과 query embedding 설정을 함께 교체한다.
- 온라인 query embedding 또는 vector 검색 실패 시 caller가 `allow_degraded=true`를 명시한 요청만 lexical-only 결과를 반환한다. 응답에 degraded 상태와 실패 branch를 표시하고 별도 품질·latency 기준을 적용한다. flag가 없거나 false이면 요청을 실패시킨다.
- 부분 build나 검증 실패 generation은 게시하지 않는다.

## 8. 환각 및 데이터 손실 방지

### 8.1 생성 단계별 방어

| 단계 | 주요 위험 | 필수 방어 |
|---|---|---|
| OCR | 생략, 요약, 숫자·부정 표현 변경 | 페이지 대사, 원문 전사 prompt, 숫자·표·각주 검사 |
| 구조 분석 | 관계 추측, 원문 없는 사실 생성 | 원문 범위 강제, schema validation, 규칙 결과와 대사 |
| chunking | 조건·제외와 본문 분리 | 관계 metadata, 의미 경계, 인접 context와 gold query 검증 |
| 임베딩 | stale·혼합 모델 vector | content hash coverage, 모델·차원 generation 고정 |
| 검색 | issuer 충돌, filter 후처리로 관련 결과 손실 | issuer-scoped ID, 후보 단계 filter, 공통 evidence ID |
| 원본 PDF 제공 | 잘못된 버전·변조 파일·과도한 파일 전달 | 승인 사용자, `source_pdf` scope, exact ID·SHA-256·MIME 검증, 100 MB·Range, 전체 파일 streaming |
| 페이지 제공 | 잘못된 page·파생물 혼동 | OCR text는 `search`, 원본 PDF에서 요청 시 생성한 PNG는 `source_pdf`, 7일 cache, source span 연결, 분할 PDF 생성 금지 |
| MCP 반환 | 짧은 인용으로 핵심 조건 손실 | 충분한 원문과 후속 전체 근거 조회, version·generation 표시 |
| 외부 LLM 답변 | 근거 밖 결론, 버전 혼합 | evidence-only 계약, 인용 검증, 불충분·충돌 명시 |

### 8.2 근거 없는 확정 금지

- 검색 결과가 없다는 사실을 해당 혜택이나 조건이 없다는 사실로 바꾸지 않는다.
- 서로 다른 기준일의 안내장을 하나의 현재 조건으로 합치지 않는다.
- 상품명이 비슷하다는 이유로 상품코드가 다른 결과를 합치지 않는다.
- 원문에 없는 계산 결과, 환산 값 또는 추천을 공시 사실처럼 반환하지 않는다.
- 문서가 불완전하거나 OCR 검토 대기이면 그 상태를 숨기지 않는다.
- 인용은 안정적인 evidence ID, 문서 버전, 페이지·범위와 content hash를 통해 재검증 가능해야 한다.

### 8.3 정보 손실 감시

generation별로 다음 수치를 이전 세대와 비교한다.

- 카드사별 문서·페이지 수
- OCR 문자 수와 비어 있는 페이지 수
- 표·각주·제외조건 block 수
- 구조 section 및 관계 수
- 임베딩 대상·성공·현재 hash coverage 수
- 색인 가능한 evidence 수
- 검색 gold query 통과율

급격한 감소나 비정상 증가는 자동 성공으로 간주하지 않고 원인 확인 대상으로 둔다. 경보 기준은 Codex가 corpus baseline 분석 후 결정한다.

## 9. Gold sample과 평가 데이터

### 9.1 표본 구성

gold sample은 단순 무작위 문서만으로 만들지 않고 다음을 층화해 포함한다.

- 지원하는 각 카드사와 카드 유형
- 최신본과 과거 변경본
- text-native PDF와 이미지 중심 PDF
- 짧은 문서와 긴 문서
- 단일 열, 다단, 복잡한 표와 페이지를 넘는 표
- 작은 글씨 각주와 공통 유의사항
- 연회비, 전월실적, 금액·비율·횟수·월 한도
- 실적 제외와 혜택 제외조건
- 같은 상품코드 또는 유사 상품명이 충돌할 수 있는 사례
- OCR 또는 구조 분석이 과거에 실패했던 문서

초기 문서 수, 카드사별 최소 수, 페이지 수와 운영 중 추가 표본 비율은 Codex가 레이아웃·위험조건 coverage 분석으로 결정한다.

### 9.2 gold 정답 범위

사람이 원본 PDF를 확인해 다음을 기록한다.

- 페이지별 기준 전사와 읽기 순서
- 표의 머리글·셀·병합·각주 관계
- 주요 section과 원문 범위
- 혜택, 전월실적, 한도, 적용 대상과 기간
- 실적 제외 및 혜택 제외조건
- 숫자·단위·부등호·범위의 정확한 표기
- 질문별 기대 문서·evidence와 허용 가능한 관련 근거
- 버전 충돌 또는 답할 수 없음이 정답인 사례

gold 자체도 작성자, 검토자, 근거 페이지와 변경 이력을 가진다. 중요한 조건은 2인 검토를 적용하며, 검토 범위는 Codex가 위험 기반 표본 설계로 정한다.

### 9.3 평가 세트 분리

- 모델·prompt·threshold 조정용 calibration 세트
- 최종 승인용 고정 regression 세트
- 운영 중 발견한 새로운 레이아웃과 실패 사례 세트

승인용 세트를 반복 조정에 사용해 과적합하지 않는다. gold 변경 시 기존 결과가 잘못된 이유와 승인 이력을 남긴다.

## 10. 수치 및 조건 검증

### 10.1 우선 검증 대상

- 원, 천원, 만원 등 통화와 단위
- %, 배수, 포인트·마일리지 적립률
- 일·월·연 단위 기간과 기준일
- 회, 건, 인원, 횟수 제한
- 최소·최대·이상·초과·이하·미만 범위
- 전월실적 구간과 혜택 한도
- 연회비의 국내·해외 또는 브랜드별 구분
- 제외조건에 포함된 업종, 결제 수단과 거래 유형

### 10.2 검증 단위

숫자 하나만 대조하지 않고 다음 문맥을 묶어 검증한다.

- 숫자의 대상 혜택 또는 조건
- 값과 단위
- 적용 기간과 횟수
- 최소·최대 범위
- 전월실적 구간
- 적용 대상과 제외 대상
- 각주와 예외

예를 들어 같은 `5%`라도 대상, 전월실적, 월 한도와 제외조건이 다르면 다른 사실이다. 정규화된 숫자가 일치해도 이 관계가 끊기면 품질 실패다.

### 10.3 자동 및 수동 검사

- OCR과 PDF gold 사이의 숫자·기호 token 대조
- OCR 원문과 구조화 값 사이의 exact span 대조
- 표 머리글과 셀 단위 연결 검사
- `이상/초과`, `이하/미만`, 긍정/제외 표현의 쌍 검증
- 동일 문서 안의 반복 표기 불일치 탐지
- 버전 간 변경된 숫자와 조건의 diff 검토
- 고위험 문서와 자동 검사 실패 항목의 사람 검토

문자 정확도, 숫자 token 정확도, critical condition recall과 수동 검토 비율의 threshold는 Codex가 baseline으로 결정한다. 다만 발견된 중대한 숫자 변경, 부정 표현 반전 또는 제외조건 누락은 평균 점수로 상쇄하지 않고 해당 generation의 차단 사유로 취급한다.

## 11. Acceptance gate

각 단계는 산출물 생성과 품질 승인을 분리한다. 아래 gate를 통과하기 전에는 다음 운영 단계 또는 MCP generation에 게시하지 않는다.

### 11.1 Gate A: 수집

- PDF 해시와 파일 존재 여부가 일치한다.
- 허용된 카드사 출처와 최종 URL이 확인된다.
- 모든 성공 파일이 PDF 구조와 페이지 열기 검사를 통과한다.
- 신규·변경·동일·실패 합계가 discovery 전체와 대사된다.
- 비정상적인 discovery 증감이 검토되었다.

### 11.2 Gate B: OCR

- PDF 모든 페이지가 OCR 페이지 또는 명시적 실패 상태와 대사된다.
- OCR 내용 해시·문자 수와 provenance가 실제 파일과 일치한다.
- gold sample의 전체 문자, 숫자 token, 표 관계와 critical condition 지표가 Codex가 baseline으로 정한 threshold를 충족한다.
- 탐지된 제외조건 누락, 숫자 변경 또는 부정 표현 반전이 해결되었다.
- 초기 대량 OCR은 Codex-only 정책 준수 여부가 확인된다.

### 11.3 Gate C: 구조 분석

- 모든 사실·관계와 인용이 유효한 OCR 범위를 참조한다.
- 원문에 없는 LLM 사실과 schema 위반이 없다.
- 규칙과 LLM 충돌이 해결되거나 운영 corpus에서 격리되었다.
- 표·각주·혜택·실적·제외 관계의 gold 지표가 Codex가 baseline으로 정한 threshold를 충족한다.

### 11.4 Gate D: 임베딩·색인

- 최신 문서 content hash의 임베딩·색인 coverage가 100%다. 과거 이력 실패는 quarantine·품질 보고서에 명시하며 stale row로 coverage를 채우지 않는다.
- 모델·차원·입력 정책이 generation 안에서 일관되고 stale vector가 없다.
- gold query의 Recall@K, MRR, nDCG@K와 filter 정확도가 Codex가 baseline으로 정한 threshold를 충족한다.
- 목표 corpus 규모와 초기 동시 요청 5개에서 검색 품질을 유지하고 bounded timeout·cancellation이 동작한다. 수치 latency, 메모리와 I/O 기준은 BULK pilot 후 결정한다.
- 새 generation 검증 실패 시 기존 generation 유지 및 rollback이 확인된다.

### 11.5 Gate E: MCP 근거 제공

- 대표 질의의 모든 결과가 유효한 원문 evidence로 후속 조회된다.
- issuer, 상품코드, 문서 버전, 기준일과 generation이 보존된다.
- 혜택 조건과 제외조건이 결과 제한 때문에 조용히 누락되지 않는다.
- 근거 부족과 버전 충돌 시 abstention 또는 충돌 표시가 검증된다.
- 정상, lexical-only 등 retrieval mode와 degraded 상태가 정확히 구분된다.
- 페이지 조회가 동일 version의 PDF page와 OCR source span으로 역추적된다.
- 페이지 PNG는 명시적으로 요청한 경우에만 원본 PDF에서 생성하고 7일 cache 후 제거하며 분할 PDF를 만들지 않는다.
- `source_pdf` scope로 명시적으로 요청한 원본 PDF 전체 파일이 exact document version·SHA-256과 일치하며, 모델이 만든 대체 문서나 요약본으로 바뀌지 않는다.

### 11.6 gate 판정

gate 결과는 `통과`, `조건부 통과`, `실패`로 기록할 수 있다. 조건부 통과의 허용 범위는 Codex가 위험도와 회귀 영향으로 정하고 근거를 남긴다. 다음 항목은 조건부 통과로 숨기지 않는다.

- 원본과 다른 중대한 숫자
- 제외조건 또는 부정 표현 누락
- 존재하지 않는 원문을 근거로 한 사실
- 서로 다른 모델·차원의 vector 혼합
- 문서·index 세대가 섞인 온라인 결과
- 검증하지 않은 generation의 운영 게시
- 최신 문서의 OCR·구조·임베딩·색인 누락 또는 실패

## 12. 운영 중 품질 감시와 회귀 관리

- 매 일일 증분 generation에서 문서 수, OCR 페이지·문자, 구조 block, embedding coverage와 검색 회귀를 기록한다.
- 모델, prompt, parser, taxonomy, chunking 또는 index 설정 변경은 품질 영향이 있는 버전 변경으로 취급한다.
- 실패율뿐 아니라 카드사·페이지 유형·section별 품질을 분리한다.
- 사용자나 운영자가 발견한 잘못된 근거는 원본 페이지, 원인 단계와 generation을 연결해 회귀 세트에 추가한다.
- 현행 generation과 이전 generation의 품질·성능 비교 없이 교체하지 않는다.
- quality report와 generation manifest는 해당 generation과 같은 수명으로 보존한다. 성공 최근 3개, 실패 candidate 7일, 수동 pin은 unpin 전까지다.

## 13. 외부 제공자와 데이터 취급

Codex와 OpenRouter 호출은 카드상품 원문, 페이지 이미지, OCR text 또는 검색 질의를 외부 서비스에 전달할 수 있다. 구현 전에 다음을 확인한다.

- 카드사 공시 자료의 수집·처리·재전송 허용 범위
- 제공자별 데이터 보존, 학습 사용과 지역 정책
- API key, OAuth token과 사용자 질의의 로그 노출 방지
- prompt·오류 로그에 비밀정보 또는 불필요한 개인정보가 포함되지 않는지 여부
- 원문 전체 대신 필요한 범위만 보낼 수 있는 작업인지 여부
- 제공자 장애·정책 변경 시 중단 및 재개 절차

법적 사용 범위와 외부 공개 승인 담당자는 공개 운영 전 확인하는 외부 gate다. generation 보존은 성공 최근 3개·실패 candidate 7일·수동 pin은 unpin 전까지로 확정했고, redaction 세부 규칙은 Codex가 보안 시험으로 정한다. 비밀정보는 prompt, metadata, Git, Docker 이미지 또는 corpus volume에 포함하지 않는다.

## 14. 구현 중 Codex 결정과 외부 gate

아래 기술값은 개발 착수 차단사항이 아니다. Codex가 gold set·신한 BULK pilot·부하 및 장애 시험으로 최적안을 선택하고 ADR, test report와 체크리스트에 근거를 남긴다.

| 항목 | 상태 | 필요한 근거 |
|---|---|---|
| 역할별 실제 모델명과 고정 방식 | `구현 중 Codex 결정` | gold sample 비교, 가용성 및 provenance 확인 |
| OCR·구조 분석 retry와 fallback 단위 | `결정 완료` | provider/model 전환 시 전체 문서를 새 attempt로 실행하고 혼합 금지 |
| OpenRouter provider pinning·failover | `구현 중 Codex 결정` | 동일 모델 출력 호환성과 운영 안정성 |
| gold sample 문서·페이지·질의 규모 | `구현 중 Codex 결정` | 카드사·레이아웃·고위험 조건 coverage |
| OCR 문자·숫자·표·critical condition threshold | `구현 중 Codex 결정` | baseline 측정과 중대 오류 zero-tolerance 원칙 |
| 구조 taxonomy, confidence와 검토 threshold | `구현 중 Codex 결정` | 도메인 검수 결과 |
| 임베딩 모델, 차원과 입력 prefix | `구현 중 Codex 결정` | 프로젝트 gold retrieval benchmark |
| Recall@K, MRR, nDCG@K와 filter 합격선 | `구현 중 Codex 결정` | 실제 사용 질의와 품질 우선 원칙 |
| lexical-only degraded 품질·latency 합격선 | `구현 중 Codex 결정` | opt-in 정책은 확정, gold query로 허용 가능한 결과 범위 측정 |
| 조건부 gate 승인 범위와 승인자 | `구현 중 Codex 결정` | 운영·품질 책임 분리; 중대 오류는 조건부 통과 금지 |
| generation·품질 보고서 보존 | `결정 완료` | 성공 최근 3개, 실패 candidate 7일, 수동 pin은 unpin 전까지 |
| 외부 제공자 데이터 취급·보존 정책 | `외부 gate` | 약관·보안·법무 검토; 공개 운영 전 확인 |
