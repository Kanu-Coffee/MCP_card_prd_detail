# CardRAG v1.0.5 운영 문서

CardRAG v1.0.5의 지원 운영 구조는 다음과 같습니다.

```text
cardrag-worker -> WebDAV -> cardrag-mcp
```

Worker는 필요할 때 한 번 실행되는 유일한 게시자이고, MCP는 검증된 로컬 검색
세대를 계속 서비스합니다. 두 프로세스 사이에 온라인 요청 경로는 없으며,
WebDAV의 불변 객체와 세대 포인터만 공유합니다.

## 처음 읽는 순서

1. [프로젝트 README](../README.md)에서 전체 구조와 운영 이미지를 확인합니다.
2. [배포 파일 안내](../deploy/README.md)에서 Compose와 secret overlay의 역할을
   확인합니다.
3. [실운영 가이드](SIMPLE_RUNTIME.md)를 위에서 아래로 실행합니다.
4. 검증된 과거 OCR을 이관할 때만
   [Legacy data-kit OCR adoption v2](LEGACY_DATA_KIT_ADOPTION_V2.md)를 따릅니다.

## 운영 완료 기준

- Worker 컨테이너의 Codex 로그인이 완료되어 있습니다.
- `webdav-check`가 종료 코드 0과 `"reachable": true`를 반환합니다.
- 첫 Worker 실행 결과가 `succeeded` 또는 이미 같은 세대가 있을 때
  `no_change`입니다.
- MCP의 `/health/live`와 `/health/ready`가 모두 HTTP 200입니다.
- `cardrag-worker.timer`의 다음 실행 시각이 03:00 Asia/Seoul로 표시됩니다.
- MCP 외부 주소는 HTTPS이고 컨테이너 포트는 인터넷에 직접 노출되지 않습니다.

상태 확인, 실패 실행 재개, MCP 재동기화, 이미지 업그레이드 절차도
[실운영 가이드](SIMPLE_RUNTIME.md)에 포함되어 있습니다.

v1.0.2에서 v1.0.4 이상으로 올릴 때는 v2/v3를 함께 읽는 MCP를 먼저 배포하고,
그 MCP가 기존 v2 세대를 계속 서비스하는 것을 확인한 다음 v3 Worker를
배포하십시오.
