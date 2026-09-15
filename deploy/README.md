# 배포 템플릿

[설치·운영 안내](../docs/OPERATIONS.md)에 따라 Worker와 MCP를 독립적으로 배포합니다.
이 디렉터리는 재사용 가능한 템플릿이며 실제 호스트 설정이나 인증 정보를 포함하지 않습니다.

| 파일 | 용도 |
|---|---|
| `simple.env.example` | Compose용 호스트 설정 예시 |
| `worker/compose.yaml` | 한 번 실행하는 수집·OCR Worker |
| `mcp/compose.yaml` | 읽기 전용 상시 MCP 서버 |
| 각 `compose.secrets.yaml` | 호스트 비밀 파일의 읽기 전용 주입 |
| 각 `compose.ca.yaml` | 사용자 지정 WebDAV CA 인증서 |
| 각 `compose.candidate.yaml` | digest로 고정한 격리 후보 검증 |
| `worker/compose.cache-seed.yaml` | 종료된 호환 Worker 상태의 읽기 전용 seed |
| `worker/compose.adoption.yaml` | 검증된 외부 OCR export의 읽기 전용 입력 |
| `worker/compose.aggregation-profile.yaml` | 검증된 문서 집계 프로파일 |
| `worker/cardrag-worker.service`, `.timer` | 선택적 systemd 실행·예약 템플릿 |

기본 이미지는 소스에서 빌드합니다. 릴리스 이미지를 사용할 때는 검증된 immutable digest를
역할별 `CARDRAG_WORKER_IMAGE`, `CARDRAG_MCP_IMAGE`에 지정하십시오.
후보 overlay는 로컬 빌드 fallback을 제거하고 별도의 상태·인증 볼륨을 사용합니다.
`compose.cache-seed.yaml`은 `CARDRAG_SEED_WORKER_STATE_VOLUME`에 원본 볼륨명을
명시해야 렌더링되며, 컨테이너의 `/mnt/cardrag-seed-state`에 read-only·nocopy로 연결합니다.
원본 writer 종료와 데이터 형식 호환성을 먼저 확인하십시오.

처음 설치할 때와 기존 설치를 전환할 때의 볼륨 선택은 다릅니다. 기존 운영의 볼륨 이름을
확인하지 않은 채 새 기본값을 적용하지 마십시오. Worker 상태와 MCP 상태, Codex 인증은
서로 별도 경로입니다. 복사는 writer가 종료된 뒤에만 수행하며 상세 조건은
[복구](../docs/RECOVERY.md)와 [stable 전환](../docs/RELEASING.md)을 따릅니다.
