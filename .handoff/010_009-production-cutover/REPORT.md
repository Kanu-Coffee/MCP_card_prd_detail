# 010 REPORT — 009 운영 반영 완료

작성: 2026-10-08 Executor. 판정: **운영 반영 완료**. PLAN 및 CUTOVER_RETRY의 구현·전환·상품 확인·정리를 완료했다. 기존 문서와 첫 시도 실패 증거는 이력으로 보존한다.

## 1. Git / 구현 / 이미지

- 검증된 runtime 구현 b544a80. PR #42 CI run37731886246 성공 후 main8ac4712로 병합했다. 병합된009 브랜치는 정리했다. 준비 문서7c7189d, 재시도 문서/스크립트d597366도 main에 반영했다.
- main CI d597366: run37733790349 success. 준비 문서7c7189d도 run37732640747 success. main merge8ac4712의 CI는 후속 push로 cancelled됐으며 이를 성공이라고 해석하지 않는다.
- 현재 source: `/opt/cardrag/current` → `/opt/cardrag/009-b544a80`.
- MCP `cardrag-mcp:009-b544a80`, imageID `sha256:307bdccc2cbcb9c035c8b0a4bd5274c4cb7dde09527e77b234e231183645c908`.
- Worker `cardrag-worker:009-b544a80`, imageID `sha256:bb457d7d675278bb6eaefa6dd7c61375ef7005dda9fc5296e7eb57677ca349fb`.
- 상세 image/help/PR/CI 증거는 evidence의 기존 파일을 참조한다. 공개 Release v1.0.33은 그대로 두며 신규 GitHub/Docker Hub release를 발행하지 않았다.

## 2. 실제 전환 결과 / 조정

첫 sudo script는 초기 준비 대기가 짧아 rollback했다. 이전 이미지도4분가량 초기 검사 후 준비됐으므로 약4.9GB serving DB 검사 시간을 고려해 재시도 대기를15분으로 늘렸다. 재시도 script는 이동 journal과 실패 복원을 제공한다. 사용자가 `sudo python3 /opt/cardrag/009-b544a80/operations/cutover-v2.py`를 실행하여 **Cutover complete**를 확인했다. 이후 Executor가 실제 current/image/health/설정/상품 응답을 확인했다.

사용자 추가 지시에 따라 버전이 들어간 운영 이름을 바꿨다:

| 구성 | 실제 운영값 |
|---|---|
| MCP project / container | cardrag-mcp / cardrag-mcp |
| MCP volume | cardrag-mcp-state |
| Worker project / state | cardrag-worker / cardrag-worker-state |
| auth volume | cardrag-worker-auth |
| Paddle models | cardrag-worker-paddleocr-models |

같은 파일시스템 내에서 기존 volume 내용만 rename했다. 데이터 바이트를 복제하거나 Docker 메타데이터 DB를 수정하지 않았다. 새 volume은 external:true다. 실행 전 모의 성공/복원 검증에서 파일 inode와 내용 보존을 확인했다. 운영 overlay의 secret/environment/Opencode 설정을 유지했다. 현재 OCR은 **opencode / alibaba-token-plan/qwen3.8-flash / medium**, fallback provider 빈 값이다.

## 3. 실제 운영 검증

- `curl http://127.0.0.1:18015/health/ready`: ready=true. 실제 cardrag-mcp image009, healthy.
- `.venv/bin/python .handoff/010_009-production-cutover/evidence/http-smoke.py`: 인증된 HTTP 상품5건/같은 revision benefit contract bundle 통과. 우리500107·104022·104023, 하나15911, 신한00368. generation `g-eba5ca0d13924abdb1f36937-71a5fd98d58b`.
- 500107의1.2% 할인은 benefit_summary_texts에 있고 문제의 문구가 condition_summary_texts에 없다. 변경/출시 안내가 benefit_headings에 없는지 확인했고 연회비15,000원 및 같은 revision 약관의1.2% 근거도 확인했다. 상세 응답은 `evidence/operational-smoke.json`.
- systemd Worker service inactive, timer active. 다음 실행 **2026-10-09 03:00 Asia/Seoul**. current의 Worker resolved image009 및 정상 Opencode 설정/공통 volume 확인. 강제 Worker 실행은 하지 않았다.

### LibreChat 연결 후속 수정

최종 확인 중 LibreChat 중계가 이전 컨테이너 이름/네트워크를 고정 참조해502를 반환하는 것을 발견했다. 다음 host-local 설정을 기존 내용을 보존하면서 수정했다:

- `/home/lee/.openclaw/workspace/librechat-reporting/docker-compose.yml`: 중계 external network를 cardrag-mcp_default로 변경, 논리 key도 cardrag_mcp로 변경.
- 같은 프로젝트 `infra/librechat/cardrag-mcp-host-proxy.conf`: upstream을 cardrag-mcp:8000으로 변경.
- 기존 중계 컨테이너를 새 네트워크에 연결하고 nginx 구성 검사/reload 후 이전 네트워크에서 분리했다. LibreChat 본체를 재시작하지 않았다. 지속 설정도 변경돼 향후 Compose 재생성 때 같은 연결을 사용한다.
- 중계 readiness=true/healthy, LibreChat 컨테이너에서 중계 readiness HTTP200 확인. 같은 컨테이너에서 인증된 MCP initialize 및500107 상품 호출도 통과했다. `evidence/operating-state.json`, `proxy-final.conf` 참조.
- 이 외부 프로젝트의 변경은 host-local 운영 연결 조정이다. 해당 프로젝트의 기존 변경을 섞어 별도 Git commit하지 않았다.

## 4. 정리 / 롤백

실제 빈 상태와 참조 없음 확인 후 다음 이전 volume metadata를 삭제했다: cardrag-mcp-v129-candidate-state, cardrag-worker-v130-candidate-state, cardrag-worker-v120-recovery-auth-20260910. endpoint가 없는 cardrag-stable-v1026_default network도 삭제했다. 운영 데이터는 새 이름의 volume에 보존한다.

활성 container bind/current/wrapper 참조가 없음을 확인하고 더 오래된 `/opt/cardrag/007-31edb1d` source snapshot을 삭제했다(21,497,667 bytes). 빈 volume 삭제 자체에서 큰 용량 회수를 주장하지 않는다. 실제 cleanup 내역은 `evidence/cleanup.json`.

이전 운영 롤백은 **008 snapshot + Worker008/MCP007 이미지 한 쌍**만 보유한다. 008 overlay/wrapper도 공통 volume/container/project 이름을 사용하므로 이전 이름의 volume/network를 다시 만들 필요가 없다. Worker008 ID `sha256:2eed6edbeb4f1d57d69481bd0d576dfbfcc957d29cb3bd89a5591619f5e9d121`, MCP007 ID `sha256:1ddf263a7024c4ddf982ee5956938fd7fa195f9318e97901293301ebbaf9f6f7`.

필요시 타이머를 중지하고 Worker inactive/실행 컨테이너 없음 확인 후 current를008로 바꾸고 해당 mcp-compose.sh로 up한다. readiness는 최대15분 허용하고 정상 확인 후 타이머를 재개한다. 공통 cardrag-mcp 이름과 network를 쓰므로 LibreChat upstream 변경은 필요 없다. 롤백은 코드/이미지 롤백이며 별도 시점의 대형 데이터 복제본을 보유하지 않는다.

## 5. 남은 관찰 / 범위

운영 인수는 완료했다. 다음03시 정규 Worker의 실행 결과는 운영 관찰 대상이며 완료 승인을2일 지연시키는 조건이 아니다. OCR·embedding·전량 Worker를 추가 실행하지 않았으므로 새 코드의 전체 배치를 이번 전환에서 재검증했다고 주장하지 않는다. 사용자가 구동 완료/오류를 알려주면 후속 조치한다.

토큰·secret·전체 env 및 root 전용 migration journal을 커밋하지 않았다. 기존 사용자 Excel3개도 수정/삭제/커밋하지 않았다. 공개 릴리스는 별도 과제다.
