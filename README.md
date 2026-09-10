# CardRAG

카드사 상품안내장 PDF를 수집하고, 조항·표·주석의 연결 관계를 보존해 검색하는 MCP 서버입니다.
현재 소스 버전은 **1.0.22**입니다. 카드 상품 탐색, 출시 상품 조사, 혜택 비교에 필요한
원문 근거와 출처를 AI 클라이언트에 제공합니다.

```text
카드사 공식 PDF → Worker: 수집 · OCR · 임베딩
                          ↓
                 WebDAV: 불변 데이터 묶음
                          ↓
                 MCP: 검증 · 읽기 전용 조회 → MCP 클라이언트
```

Worker는 한 번 실행하고 종료합니다. MCP는 검증된 데이터 묶음(generation)을 읽는 별도
상시 서비스입니다. 둘은 상태 디렉터리를 공유하지 않으며 각각 배포할 수 있습니다.
LibreChat 등 Streamable HTTP MCP 클라이언트로 연결할 수 있고 특정 조사·보고 앱을
설치할 필요는 없습니다.

## 제공 기능

- 8개 카드사의 공식 상품안내장 수집과 변경 추적
- 한국어·영어 카드사명 정규화, 잘못된 이름에 대한 명시 오류
- 상품명 검색, 상품 목록·적재 범위 확인, 가맹점별 혜택 탐색
- 확정 출시일 기준의 기간 조회와 여러 상품의 일괄 요약
- 계약 단위 검색과 조항·표·주석·페이지·PDF 출처 조회
- 여러 호출의 generation 일치 확인, 페이지 탐색, 크기를 제한한 검색 응답

| 코드 | 카드사 |
|---|---|
| `woori` | 우리카드 |
| `kb` | KB국민카드 |
| `shinhan` | 신한카드 |
| `samsung` | 삼성카드 |
| `hyundai` | 현대카드 |
| `hana` | 하나카드 |
| `lotte` | 롯데카드 |
| `bc` | BC카드 |

지원 카드사 목록과 실제 적재 범위는 다릅니다. `find_products(mode="coverage")`로 현재
데이터를 확인하십시오. 출시 여부는 `launch_date`로 판단하고 다른 날짜 필드인
`effective_date`로 추정하지 않습니다. 문서 날짜의 의미는 source의 `date_basis`로 구분합니다. 출시일이 없거나 충돌하면 `[확인 필요]`로 표시합니다.

## 시작하기

개발에는 Python 3.12–3.14와 uv가 필요합니다. 아래 명령은 외부 서비스나 실제 OCR을
호출하지 않는 테스트를 실행합니다.

```bash
git clone https://github.com/Kanu-Coffee/MCP_card_prd_detail.git
cd MCP_card_prd_detail
uv sync --frozen --all-packages --all-extras
uv run --all-packages pytest
```

실제 데이터를 만들려면 HTTPS WebDAV 저장소, OCR용 Codex 인증, OpenRouter 임베딩
API 키가 필요합니다. 기본 OCR 모델은 `gpt-5.6-sol`, 임베딩은 Qwen3 8B 4,096차원입니다.
모델·제공자의 이용 가능 여부와 접근 권한은 사용하는 계정에서 확인해야 합니다.
수집된 데이터나 인증 정보는 저장소에 포함되지 않습니다.

컨테이너 설치에는 Linux amd64와 Docker Compose 2.24.4 이상을 사용합니다. Worker의
Codex sandbox를 위해 user namespace를 허용하는 커널이 필요합니다.
[설치·운영 안내](docs/OPERATIONS.md)에 따라 역할별 설정과 비밀 파일을 준비하고,
독립적인 Worker/MCP Compose 파일로 실행하십시오.

## MCP 사용

클라이언트에 서버의 `https://<your-host>/mcp` 주소와 `Authorization: Bearer <token>`
헤더를 등록합니다. 공개 주소는 TLS 프록시 뒤에 두고 MCP 호스트 포트는 기본 loopback
설정을 유지합니다. 도구 스키마는 MCP의 `tools/list`에서 확인할 수 있습니다.

```text
find_products(mode="coverage", issuers=["KB국민카드", "신한카드"])
list_recent_products(months=3, issuers=["kb", "shinhan"], limit=20)
find_products(keyword="카드 상품명", issuer="kb")
search_contracts(query="연회비와 적립 제외 조건", response_mode="compact")
```

기본 도구는 12개입니다. 날짜 범위, 일괄 조회, 오류 처리와 generation 연결 방법은
[MCP API 안내](docs/MCP_API.md)를 참고하십시오.

## 문서와 개발

- [설치·설정·모니터링](docs/OPERATIONS.md)
- [MCP 도구와 응답 계약](docs/MCP_API.md)
- [데이터 구조와 검색 원칙](docs/DATA_FORMATS.md)
- [백업·복구와 상태 관리](docs/RECOVERY.md)
- [평가와 실험 기능](docs/EVALUATION.md)
- [릴리스와 stable 전환](docs/RELEASING.md)
- [기여 안내](CONTRIBUTING.md) · [보안](SECURITY.md)

프로젝트 소스는 [Apache License 2.0](LICENSE)입니다. 외부 구성요소는
[제3자 고지](THIRD_PARTY_NOTICES.md)를 따릅니다. 카드사 PDF와 외부 제공자의 데이터·서비스
이용 권한은 소스 코드 라이선스와 별개입니다.
