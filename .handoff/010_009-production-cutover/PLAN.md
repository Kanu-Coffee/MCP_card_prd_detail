# 010 — 인수된 009의 운영 반영

- 작성일: 2026-10-08. 작성 역할: Reviewer, 운영 전환을 위한 Planner.
- 개발 인수: `.handoff/009_product-summary-field-classification/ACCEPTANCE.md`.
- 검증된 구현 commit: **b544a80**. 공개 릴리스는 기존 v1.0.33을 그대로 둔다.
- 현재 턴은 문서 작성이다. 아래 실제 전환은 사용자 운영 반영 지시 후 Executor가 수행한다.

## 1. 목표/범위

인수된 요약 분류와 Worker stage skip/수동 요청 완료 보정을 운영 이미지에 반영한다. 기존 WebDAV/serving generation/OCR/embedding/state/auth를 그대로 사용한다. main/PR 정리, 두 로컬 운영 이미지 빌드, 새 snapshot 생성, MCP 교체 및 Worker의 다음 예약 실행에 새 코드 적용, 짧은 확인과 REPORT로 마감한다.

강제 전량 Worker·재OCR·재임베딩·새 모델·03시2회 대기·정식 Docker Hub/GitHub release는 요구하지 않는다. 이 요약 분류 변경은 MCP 코드에 있으므로 기존 generation을 읽는 새 MCP부터 효과가 난다. Worker 부분 실행은 향후 필요한 변경에 사용하는 옵션이며 이번 요약 반영을 위해 전체 worker를 돌리지 않는다.

## 2. 현재 운영 실측값과 보존 대상

- 저장소 `/home/lee/projects/MCP_card_prd_detail`, 검토 브랜치 `codex/009-summary-stage-reuse`. Excel3개 untracked 사용자 자료 보호.
- `/opt/cardrag/current` → `/opt/cardrag/008-6b42a1a`.
- MCP 실제 container `cardrag-stable-v1026-mcp-1`, 이미지 `cardrag-mcp:007-31edb1d`, project `cardrag-stable-v1026`, readiness `http://127.0.0.1:18015/health/ready`.
- Worker image `cardrag-worker:008-6b42a1a`, project `cardrag-worker`.
- **OCR 최종 적용값 Opencode / alibaba-token-plan/qwen3.8-flash / medium**, fallback provider/model 빈 값.
- 현재 host-local `/opt/cardrag/008-6b42a1a/deploy/worker/compose.secrets.yaml`에 image/Opencode overrides가 있다. MCP의 해당 overlay에도 image pin이 있다. **새 snapshot에는 이 두 운영 overlay를 복사한 다음 image만 바꾼다. 저장소 기본 secrets overlay로 덮지 않는다.**
- `/etc/cardrag/worker.env`, `mcp.env`, secret 파일을 출력/커밋하지 않는다. env에는 과거 Codex/원격 image 값이 남아 있으나 host-local overlay가 우선한다. env를 수정할 필요가 없다.
- Worker state `cardrag-worker-v130-candidate-state`; MCP state `cardrag-mcp-v129-candidate-state`; auth `cardrag-worker-v120-recovery-auth-20260910`; Paddle models `cardrag-worker-paddleocr-models`. 같은 이름을 사용하고 복제/삭제하지 않는다.
- systemd Worker는 current 디렉터리에서 base+secrets Compose를 실행한다. `CARDRAG_WORKER_COMPOSE_OVERLAYS`는 빈 값이다.
- 타이머: 매일03:00 Asia/Seoul, Persistent=true. 점검 당시 service inactive/timer active, 다음 예약 2026-10-09 03:00. 실행 당일 다시 확인한다.
- `/opt/cardrag`는 lee 소유라 snapshot/심볼릭 링크 준비는 sudo 없이 가능. systemctl 조작에는 sudo가 필요할 수 있다.
- 기존 롤백 소스007, 현재008. 전환 후 **008 snapshot + 기존 Worker008/MCP007 이미지 한 쌍**만 이전 운영 롤백으로 유지한다. 새009는 현재 운영이다.

## 3. Git/CI 정리

1. 최신 Git/status 확인. 수정본 runtime files가 b544a80와 같은지 확인한다. 필요하면 기존 브랜치에서 PR 생성하여 통상 CI가 통과한 뒤 main 병합한다. 열린 PR이 없음을 작성 당시 확인했다.
2. 현재 CI는 feature branch push로 실행되지 않는다. PR/main push/workflow_dispatch에서 실행된다. '실행 없음'을 'CI 성공'이라고 보고하지 않는다.
3. merge conflict로 runtime 코드가 바뀌면 해당 변화만 검토/검증한다. 이미 통과한2462건과 Reviewer195건을 이유 없이 반복하지 않는다. 기존 CI의 통상1회 검증은 그대로 사용한다.
4. 이번 로컬 운영 이미지 태그는 `009-b544a80`. v1.0.33 기존 태그/Release를 옮기지 않는다. 새 정식 version 발행은 범위 밖이다.

## 4. snapshot과 이미지 준비 — 운영 중단 전에 수행

검증된 code snapshot을 고정한다. 다음 명령의 archive는 사용자 untracked 파일/venv를 포함하지 않는다. 같은 경로가 있으면 덮지 말고 기존 준비 상태를 점검한다.

```sh
cd /home/lee/projects/MCP_card_prd_detail
cardrag_deploy_dir=/opt/cardrag/009-b544a80
test ! -e "$cardrag_deploy_dir"
mkdir "$cardrag_deploy_dir"
git archive b544a80 | tar -x -C "$cardrag_deploy_dir"
cp /opt/cardrag/008-6b42a1a/deploy/worker/compose.secrets.yaml "$cardrag_deploy_dir/deploy/worker/compose.secrets.yaml"
cp /opt/cardrag/008-6b42a1a/deploy/mcp/compose.secrets.yaml "$cardrag_deploy_dir/deploy/mcp/compose.secrets.yaml"
sed -i 's|image: cardrag-worker:008-6b42a1a|image: cardrag-worker:009-b544a80|' "$cardrag_deploy_dir/deploy/worker/compose.secrets.yaml"
sed -i 's|image: cardrag-mcp:007-31edb1d|image: cardrag-mcp:009-b544a80|' "$cardrag_deploy_dir/deploy/mcp/compose.secrets.yaml"
mkdir -p "$cardrag_deploy_dir/operations"
```

새 snapshot의 `operations/worker-compose.sh`와 `mcp-compose.sh`를 아래 형태로 작성하고 chmod755한다. 과거 wrapper를 그대로 복사하면008의 절대 경로를 계속 사용하므로 새 절대 경로를 명시한다.

Worker wrapper:

```sh
#!/bin/sh
set -eu
exec docker compose -p cardrag-worker --env-file /etc/cardrag/worker.env \
  -f /opt/cardrag/009-b544a80/deploy/worker/compose.yaml \
  -f /opt/cardrag/009-b544a80/deploy/worker/compose.secrets.yaml "$@"
```

MCP wrapper:

```sh
#!/bin/sh
set -eu
exec docker compose -p cardrag-stable-v1026 --env-file /etc/cardrag/mcp.env \
  -f /opt/cardrag/009-b544a80/deploy/mcp/compose.yaml \
  -f /opt/cardrag/009-b544a80/deploy/mcp/compose.secrets.yaml "$@"
```

최종 Compose 설정을 `config --quiet`로 검사한다. `config --format json`을 사용할 경우 메모리에서 image/provider/model/volume 이름만 추출하고 전체 출력은 저장하지 않는다. 기대값은 두 새 image, Opencode 설정 유지, §2의 동일 volume 이름이다.

기존 default builder를 사용한다. 새 Buildx daemon/볼륨은 만들지 않는다.

```sh
cd /opt/cardrag/009-b544a80
docker build --target mcp --build-arg APP_VERSION=009-b544a80 \
  --build-arg VCS_REF=b544a80 -t cardrag-mcp:009-b544a80 .
docker build --target worker --build-arg APP_VERSION=009-b544a80 \
  --build-arg VCS_REF=b544a80 -t cardrag-worker:009-b544a80 .
./operations/mcp-compose.sh config --quiet
./operations/worker-compose.sh config --quiet
./operations/worker-compose.sh run --rm worker --help
```

`--help`는 Worker 실행이 아니다. 새 skip/reuse/dry-run 옵션 도움말은 `run --help`로 확인한다. 필요하면 `docker run --rm --network none cardrag-worker:009-b544a80 run --help`처럼 state/인증 없이도 확인 가능하다. 모델/OCR 검증을 위한 실제 run은 하지 않는다.

## 5. 실제 교체 — 사용자 지시 후

전환 직전 운영 Worker/service/one-off 컨테이너가 없는지 확인한다. running이면 완료 통지를 기다리고 전환을 연기한다. 강제 중지/kill하지 않는다. 롤백008 및 기존 두 image가 있는지 확인한다. 호스트 state/secret은 복사하지 않는다.

사용자가 SSH에서 복붙할 수 있는 sudo 부분:

```sh
sudo systemctl stop cardrag-worker.timer
systemctl is-active cardrag-worker.service
```

`inactive`일 때 진행한다(명령 exit3은 inactive 표현이지 장애 판정이 아니다). 이후 symlink와 MCP 교체:

```sh
ln -s /opt/cardrag/009-b544a80 /opt/cardrag/current.next
mv -Tf /opt/cardrag/current.next /opt/cardrag/current
/opt/cardrag/current/operations/mcp-compose.sh up -d --no-build --pull never mcp
curl -fsS http://127.0.0.1:18015/health/ready
```

새 MCP가 기존 generation/vector를 다시 적재하는 준비 시간은 허용한다. readiness가 false면 짧게 대기/로그 점검하고 다음 절차를 진행하지 않는다. 기존 자료를 다시 생성할 필요는 없다.

systemd unit은 current를 사용하므로 unit 내용 교체나 daemon-reload가 필요 없다. timer 재개:

```sh
sudo systemctl start cardrag-worker.timer
systemctl list-timers cardrag-worker.timer --no-pager
```

Persistent=true이므로 중단 중03:00을 넘겼다면 timer 재개 시 보충 실행이 발생할 수 있다. 정상 예약/보충 실행을 새 이미지로 수행하되 장시간 모니터링 턴을 유지하지 않는다. 별도 `systemctl start cardrag-worker.service`를 통한 전체 배치 강제 실행은 하지 않는다.

## 6. 짧은 운영 확인/인수 기준

- current→009, 실제 MCP image009/healthy, resolved Worker image009 및 Opencode 설정 유지, 동일 state/auth volume 확인.
- 실제 인증된 MCP HTTP로 `get_product_summary`를 확인한다. 토큰을 화면/로그에 출력하지 않고 기존 secret 파일/클라이언트를 사용한다.
- 최소 상품: 우리500107(1.2% 혜택/15,000원/변경 고지 제거/조건 필드 오분류 제거), 우리104022·104023, 하나15911, 신한00368. 현재 serving revision이 이전 비교와 다르면 해당 revision 원문과 대조한다. 고정 generation의 예전 텍스트와 무조건 동일해야 한다는 기준은 만들지 않는다.
- 같은 revision의 `get_contract_bundle`과 혜택/근거를 대조한다. 요약 최대5개 슬롯은 정상 제한이다.
- 임베딩·OCR를 호출하는 다른 search/reprocess 도구를 smoke 목적으로 사용하지 않는다.
- Worker 도움말과 기본 Compose 설정만 짧게 확인하면 배포 마감 가능. 다음03:00 실구동은 운영 관찰이며 인수를2일 지연시키는 개발 gate가 아니다. 구동/오류/완료는 사용자가 알려주면 후속 대응한다.
- 기본 예약 run에는 skip 옵션을 넣지 않는다. 향후 부분 수정 검증은 source run ID와 호환 산출물 확보 후 `--skip-pdf --skip-ocr --reuse-from-run <ID> --skip-webdav`의 로컬 경로부터 사용한다. stable 게시를 의도할 때만 `--publish-channel stable`을 명시한다. dry-run을 완료/게시로 보고하지 않는다.

## 7. 롤백/정리

새 MCP 준비/기능 확인이 실패하면 다음으로008 snapshot을 되돌린다. 008 overlay에는 MCP007/Worker008 및 Opencode 설정이 있으므로 두 구성 요소가 함께 이전 운영으로 복원된다.

```sh
sudo systemctl stop cardrag-worker.timer
# Worker가 실행 중이면 종료를 기다린 뒤 진행한다.
ln -s /opt/cardrag/008-6b42a1a /opt/cardrag/current.rollback
mv -Tf /opt/cardrag/current.rollback /opt/cardrag/current
/opt/cardrag/current/operations/mcp-compose.sh up -d --no-build --pull never mcp
curl -fsS http://127.0.0.1:18015/health/ready
sudo systemctl start cardrag-worker.timer
```

정상 전환 확인 후008을 **이전 운영 롤백1세트**로 유지한다. 007 snapshot은 활성 bind/systemd/wrapper 참조 여부를 확인한 뒤 오래된 소스/설정으로 정리한다. MCP007 image는008 롤백에 필요하므로 삭제하지 않는다. 이전 이미지와 snapshot은 태그 번호만 보고 일괄 삭제하지 않는다. 기존 state/auth/OCR/cache/model volume은 운영자료이므로 보존한다. 전역 `docker system prune --volumes`는 사용하지 않는다.

## 8. REPORT 마감

새 REPORT.md에 실제 main/PR/CI 결과, image ID/revision, 이전·새 snapshot, 최종 resolved provider/volume, readiness/HTTP 상품 확인, timer 상태, 롤백1세트 및 실제 정리 대상을 기록한다. 토큰/secret/full env는 포함하지 않는다. 배포 준비와 실제 교체를 명확히 구분한다.

현재 PLAN 작성으로 운영 반영이 완료된 것은 아니다. Executor가 위 실제 전환과 확인을 마치면 운영 인수 완료로 보고한다. 새 공개 릴리스는 별도 승인/과제다.
