# cardrag-mcp

WebDAV generation을 검증하고 읽기 전용으로 서비스하는 FastAPI·Streamable HTTP MCP
서버입니다. 기본 MCP 도구 12개와 원문 PDF·페이지·근거 리소스를 제공합니다.

Updater는 DB, vector sidecar, source CAS의 identity와 크기를 검증한 뒤 로컬 generation을
원자적으로 교체합니다. 각 요청은 하나의 generation을 pin하고, 업데이트 실패 시 마지막
정상 generation을 계속 제공합니다. 여러 호출에는 `expected_generation_id`를 사용합니다.

| 경로 | 인증 | 의미 |
|---|---|---|
| `/mcp` | Bearer token | MCP 도구·리소스 |
| `/health/live` | 없음 | HTTP 프로세스 생존 |
| `/health/ready` | 없음 | 검증된 generation 제공 가능 여부 |
| `/metrics` | Bearer token | 제한된 label의 운영 지표 |

MCP는 기본 `127.0.0.1:8000`에 바인딩합니다. 외부 노출에는 TLS 프록시와 고유 Bearer token을
사용하십시오. 인증 파일, DB, PDF, 조사 질의와 감사 원문은 저장소에 넣지 않습니다.

v5는 Qwen 4,096차원 FP32 sidecar를 읽기 전용 mmap으로 사용합니다. 파일 크기, 메모리,
download, state, audit 저장 한도는 별도로 검사합니다. 기존 schema reader와 캐시 identity는
호환성을 위해 유지합니다. 실험 도구는 기본 비활성화이며 primary 검색을 바꾸지 않습니다.

- [MCP API와 조사 예제](../../docs/MCP_API.md)
- [설치와 용량 설정](../../docs/OPERATIONS.md)
- [데이터·검색 계약](../../docs/DATA_FORMATS.md)
- [평가와 오프라인 도구](../../docs/EVALUATION.md)
