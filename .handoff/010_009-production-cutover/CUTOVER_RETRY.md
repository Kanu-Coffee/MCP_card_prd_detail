# 010 운영 전환 재시도와 운영 이름 정리

작성: 2026-10-08 Executor. 기존 PLAN/PREPARATION은 당시의 계획·준비 이력으로 보존한다. 사용자 추가 지시에 따라 아래 변경을 적용한다.

## 첫 시도 결과

사용자가 기존 `operations/cutover.sh`를 sudo로 실행했다. 새 MCP는 시작했으나 90회×2초의 readiness 대기 안에 준비되지 않아 스크립트가 current를008로 복원하고 타이머를 재개했다. 실제 확인 결과 기존 MCP007 이미지가 건강 상태이고 readiness=true, Worker service inactive, timer active였다. 새 코드 배포 완료로 판정하지 않는다.

초기 적재 중 serving SQLite 약4.9GB가 열려 있었고, 이전 이미지도 복원 후 약4분 만에 건강 상태가 되었다. 따라서 첫 시도는 초기 데이터 검사 시간을 충분히 허용하지 않은 것으로 추정한다. 신규 코드 오류로 확정할 근거는 없다. 재시도는 readiness JSON의 ready=true를 최대15분 기다리며, 실패하면 이전 데이터 위치·구성·이미지로 복원한다. 복원 readiness도 최대15분 확인하며 복원까지 실패하면 타이머를 중지한 상태로 남긴다.

## 사용자 승인된 이름 변경

버전별 혼동을 줄이도록 현재 운영뿐 아니라008 롤백 구성을 아래 공통 이름으로 바꾼다. 이미지 태그 및 snapshot 경로는 실제 구현 식별을 위해 유지한다.

| 대상 | 변경 후 |
|---|---|
| MCP Compose project / container | cardrag-mcp / cardrag-mcp |
| MCP state | cardrag-mcp-state |
| Worker state | cardrag-worker-state |
| Worker auth | cardrag-worker-auth |
| Paddle models | 기존 cardrag-worker-paddleocr-models 유지 |

compose.secrets.yaml의 image/Opencode/environment/secret 설정은 보존하고 volume name/external:true 및 MCP container_name만 추가한다. systemd는 기존 current/base+secrets 구성을 그대로 사용한다. 외부 볼륨으로 명시하므로 과거 Compose project 소유권 경고도 해소한다.

## 데이터 이전과 실패 복원

- 실행파일: `/opt/cardrag/009-b544a80/operations/cutover-v2.py`. 저장소 사본: `evidence/cutover-v2.py`.
- root 실행을 요구한다. 타이머 중지 후 Worker inactive/실행 컨테이너 없음을 확인한다. 실행 중 Worker를 강제로 중지하지 않는다.
- MCP를 정상 정지하고 컨테이너만 제거한다. 볼륨 및 데이터는 삭제하지 않는다.
- plain local Docker volume만 허용하고 기존 볼륨을 참조하는 컨테이너가 없음을 확인한다. 새 이름의 볼륨은 새로 생성하며 빈 상태·동일 파일시스템을 확인한다.
- Docker 관리용 볼륨 디렉터리는 그대로 두고 그 내부의 각 최상위 파일/디렉터리를 `os.rename`으로 새 볼륨에 이동한다. 바이트 복제나 Docker 메타데이터 DB 수정은 없다. 파일 inode/하위 디렉터리/권한을 보존한다.
- 이동 전마다 root 전용 journal을 기록한다. 실패/일반 중단 시 역순 이동 후 원래 overlay/wrapper/current/MCP를 복원한다. 강제 전원 차단 등으로 자동 복원되지 않았다면 `sudo python3 .../cutover-v2.py --restore`로 journal 기반 복원이 가능하다. journal에는 운영 overlay 원문이 들어 있으므로 출력/커밋하지 않는다.
- 새 MCP ready 확인 후008 롤백 구성도 공통 볼륨 이름으로 바꾸고 타이머를 재개한다. Worker는 강제 실행하지 않는다.
- 기존 빈 볼륨 및 사용하지 않는 이전 project network는 최종 상품 smoke가 통과한 뒤 참조 여부를 확인하고 정리한다. 이전 데이터와 더 오래된 rollback을 추가로 보유하지 않는다.

## 검증 / 다음 단계

- Python 문법 및 Ruff 검사 통과.
- 임시 디렉터리를 이용한 성공 이전 및 readiness 실패 복원 모의 검증 통과. 실제 파일 inode/내용 유지, current 및 overlay 복원, 환경 설정 보존 확인. 실제 sudo 실행을 대신하는 증거로 해석하지 않는다.
- 현재 세션의 타이머 조작은 대화형 인증이 필요하므로 사용자 SSH sudo 실행이 필요하다.
- 실행 결과를 받은 뒤 실제 MCP HTTP 상품5건과 동일 revision contract bundle을 확인하고, state/auth 참조와 타이머를 확인한다. 성공 후 REPORT를 작성한다. OCR/embedding/전량 Worker 실행과 새로운 공개 Release는 범위에 추가하지 않는다.
