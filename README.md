# CardRAG v1.0.6

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
- 카드사가 최신 파일을 PDF 대신 승인된 DRM 컨테이너로 제공하면 과거 PDF로
  대체하지 않습니다. 정확한 원본 지문이 일치하는 항목만 `unsupported_drm`으로
  명시하고 MCP 상품 조회에 그대로 노출합니다.

v1.0.6 Worker는 `cardrag.generation.v3`/`cardrag.serving-db.v3`를 게시하며,
우리카드의 정확히 승인된 Fasoo DRMONE 원본도 `unsupported_drm`으로 표현합니다.
v1.0.4 이상 MCP는 기존 v2와 새 v3 세대를 모두 읽습니다. 따라서 v1.0.2에서
업그레이드할 때는 **MCP를 먼저 v1.0.4 이상으로 교체한 뒤 Worker를 교체**해야 합니다.
첫 v3 세대 게시 뒤에는 v1.0.2 MCP로 내리지 말고 v1.0.4 호환 수정 버전으로
전진 복구합니다.

v1.0.6도 희소 페이지 OCR 출력의 의미 없는 빈 줄을 정규화하는 호환 수정을
유지합니다. 애플리케이션 버전은 1.0.6으로 올리되 native OCR의 processor contract는
`cardrag-worker/1.0.4`로 유지합니다. 따라서 v1.0.4 실행이 WebDAV에 완전히 게시한
검증 완료 OCR은 실패한 실행을 재개하지 않고 새 실행에서도 재사용됩니다.

신한카드 adapter는 회전하는 다운로드 토큰을 공식 모바일 상품공시 API에서
다운로드 직전에 다시 조회합니다. 상품코드·이름·시행일·source version을 안정적인
desktop 공시 목록과 다시 결속한 뒤에만 PDF를 받습니다. v1.0.6은 일부 긴 상품명의
검색어를 모바일 API의 EUC-KR 경계 안으로 제한합니다. 검색 인덱스가 문서를 찾지
못할 때만 범위가 제한된 현재 카테고리 목록을 조회하며, 어느 경로든 최종 결과에는
전체 상품코드·이름·시행일·source version의 exact singleton 결속을 적용합니다.
운영 활성 목록은 `woori,kb,shinhan`입니다.

## 운영 이미지

v1.0.6 운영 배포에는 다음 두 고정 태그를 사용합니다.

```text
ymtop59/mcp-card-prd-detail:1.0.6-worker
ymtop59/mcp-card-prd-detail:1.0.6-mcp
```

Worker는 실행이 끝나면 종료되는 배치이고, MCP는 계속 실행되는 HTTP/MCP
서비스입니다. 두 역할을 각각 독립된 Compose 프로젝트로 운영해야 합니다.

## 운영 시작 순서

1. 기존 `/opt/cardrag`을 보존하고 `/opt/cardrag-v1.0.6`에 immutable 배포 파일을
   준비합니다.
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
