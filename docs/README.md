# CardRAG 문서

현재 지원하는 구조의 단일 운영 기준은 [SIMPLE_RUNTIME.md](SIMPLE_RUNTIME.md)다.

```text
cardrag-worker  -> WebDAV immutable artifacts -> cardrag-mcp
                     index.sqlite3 + PDF/OCR CAS
```

새 런타임은 Worker와 MCP 두 배포물만 사용한다. PostgreSQL, pgvector, Keycloak,
관리자 컨테이너, 과거 버전 검색, 페이지 PNG 제공은 지원 런타임에 포함되지 않는다.

## 현행 문서

- [프로젝트 README](../README.md): 저장소 구조와 개발 명령
- [단순 런타임 운영서](SIMPLE_RUNTIME.md): 설정, WebDAV 검사, OCR 이관,
  Worker 재개, MCP 기동, shadow 검증과 rollback
- [v0.2.1 현재 자료 exporter](V1_CURRENT_INVENTORY_EXPORT.md): 보존 DB/CAS를
  변경하지 않고 최신 활성 PDF/OCR inventory를 만드는 절차
- [배포 README](../deploy/README.md): 두 Compose 배포물과 secret overlay

## v0.2.1 보관 문서

아래 번호 문서와 ADR, `REAL_ENV_HANDOFF.md`는 기존 PostgreSQL/Keycloak 기반
v0.2.1을 조사·구현할 때 작성한 기록이다. 7회 shadow 전환 기간의 데이터 이관과
rollback 근거로만 읽으며 새 런타임의 구성 또는 API 계약으로 사용하지 않는다.

- `01_PROJECT_OVERVIEW.md` ~ `09_LEGACY_IMPORT_AND_PORTABLE_STATE.md`
- `adr/`
- `REAL_ENV_HANDOFF.md`

기존 `src/cardrag/` 코드와 기존 배포 파일도 같은 이유로 read-only 보존한다.
운영 전환 승인 전에는 PostgreSQL/CAS/과거 데이터 원본을 삭제하지 않는다.

## 아직 실환경에서 확인할 항목

코드와 fixture 검증만으로 완료 처리할 수 없는 다음 항목은 실제 계정과 서버에서
확인한다.

- 카드사별 현재 페이지 markup과 PDF 다운로드 계약
- Codex OCR 및 OpenRouter embedding의 quota·품질·비용
- 대상 WebDAV의 RFC 4918 `MOVE` 및 overwrite 차단 동작
- 실제 corpus에서 검색 P95, 동시 요청 5개, vector RSS 한도
- 7회 연속 03:00 Worker 실행과 무변경 시 무호출·무게시 확인
