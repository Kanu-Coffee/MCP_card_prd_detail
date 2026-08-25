# CardRAG v1.0.0

CardRAG는 카드사 상품설명서 PDF를 검색 가능한 형태로 만드는 두 프로세스
서비스입니다. 운영 런타임은 다음 흐름만 사용합니다.

```text
one-shot Worker -> immutable WebDAV artifacts -> always-on MCP
```

- Worker는 카드사 PDF를 수집하고 OCR과 임베딩을 수행한 뒤, SQLite 검색 세대와
  PDF/OCR 객체를 WebDAV에 게시합니다.
- MCP는 WebDAV를 주기적으로 확인하고 세대 전체를 로컬에 내려받아 검증한 뒤
  원자적으로 활성화합니다.
- Worker만 WebDAV에 씁니다. MCP 요청 처리는 검증된 로컬 파일만 사용합니다.

## 운영 이미지

v1.0.0 운영 배포에는 다음 두 고정 태그를 사용합니다.

```text
ymtop59/mcp-card-prd-detail:1.0.0-worker
ymtop59/mcp-card-prd-detail:1.0.0-mcp
```

Worker는 실행이 끝나면 종료되는 배치이고, MCP는 계속 실행되는 HTTP/MCP
서비스입니다. 두 역할을 각각 독립된 Compose 프로젝트로 운영해야 합니다.

## 운영 시작 순서

1. `/opt/cardrag`에 v1.0.0 배포 파일을 준비합니다.
2. `/etc/cardrag/worker.env`, `/etc/cardrag/mcp.env`, 파일 기반 비밀값을
   준비합니다.
3. Worker 컨테이너에서 Codex 로그인을 완료합니다.
4. `webdav-check`가 모든 필수 WebDAV 동작을 통과하는지 확인합니다.
5. Worker를 한 번 실행해 첫 검색 세대를 게시합니다.
6. MCP를 시작하고 `/health/ready`가 HTTP 200인지 확인합니다.
7. Worker systemd timer를 활성화해 매일 03:00 Asia/Seoul에 실행합니다.

초보자용 명령과 성공 판정 기준은
[실운영 가이드](docs/SIMPLE_RUNTIME.md)에 순서대로 정리되어 있습니다.

## 배포 파일

- [배포 파일 안내](deploy/README.md)
- [Worker Compose](deploy/worker/compose.yaml)
- [MCP Compose](deploy/mcp/compose.yaml)
- [환경변수 예제](deploy/simple.env.example)
- [운영 문서 색인](docs/README.md)

MCP는 기본적으로 호스트의 `127.0.0.1:8000`에만 게시됩니다. 외부 공개 시에는
같은 호스트의 TLS 리버스 프록시를 사용하고, `/health/live`와
`/health/ready`를 제외한 모든 요청에 Bearer 토큰을 전달해야 합니다.
