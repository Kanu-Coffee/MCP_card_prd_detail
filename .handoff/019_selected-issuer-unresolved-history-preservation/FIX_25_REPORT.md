# 019 FIX_25_REPORT — 예약 Worker와 검증된 수동 운영 경로 일치

2026-10-22 Executor. `FIX_25.md`, `LIVE_REVIEW_11.md`, `OPERATIONS_REPORT_10.md`, 현재 Git/배포 상태를 확인하고 repo service/timer 템플릿과 설치 절차를 수정했다. **root 권한이 필요한 실제 `/etc/systemd/system` 적용과 timer disable은 이번 세션에서 수행하지 못했다.** 현재 설치본은 여전히 이전 단위이며 timer가 active다. 운영 DB/serving/Worker 실행은 변경하지 않았다.

## 확인한 불일치

설치된 `/etc/systemd/system/cardrag-worker.service`는 helper가 아니라 `docker compose -f compose.yaml -f compose.secrets.yaml`을 직접 실행했다. private runtime overlay가 빠져 다음이 수동 검증 경로와 달랐다.

| 항목 | 설치 service(실패 경로) | 검증된 helper 경로 |
|---|---|---|
| image | `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:285326...` | `cardrag-worker:019-fix24-11d1321cc9e6` |
| state volume | `cardrag-worker-v130-candidate-state` | `cardrag-worker-state` |
| Codex auth volume | `cardrag-worker-v120-recovery-auth-20260910` | `cardrag-worker-auth` |
| Paddle volume | `cardrag-worker-paddleocr-models` | 동일 |
| serving volume | `cardrag-serving` | 동일 |
| secrets | OpenRouter만 | OpenRouter + WebDAV username/password |
| OCR runtime | `/etc/cardrag/worker.env`의 candidate 계열 값 | `gpt-6.1-sol` / `medium`, local Paddle fallback |
| issuer/proxy/publication | private overlay 없음 | round-robin, approved proxy, local publication, immediate backup |
| project | `cardrag-worker` | `cardrag-worker` |
| container UID | `10001:10001` | `10001:10001` |

journal의 실패는 `worker_unexpected_failure`/exit1까지만 확인됐고 내부 예외 원인은 확정하지 않았다. 이번 FIX는 그 원인을 IP·인증·OCR로 단정하지 않는다. service/runtime 불일치는 resolved compose로 확인했다.

## 변경

- `deploy/worker/cardrag-worker.service`
  - `ExecStartPre`/`ExecStart`가 `/opt/cardrag/current/operations/worker-compose.sh`를 사용하도록 변경.
  - active release helper가 private overlay와 실제 image/state/auth/model/serving/secret 구성을 고정한다.
  - 직접 compose 실행, `CARDRAG_WORKER_COMPOSE_OVERLAYS`, `EnvironmentFile` 우회를 제거.
  - `ConditionPathExists`로 env/helper 부재 시 조용히 다른 compose로 대체하지 않게 했다.
  - 수동 issuer 고정값은 넣지 않았다.
- `deploy/worker/cardrag-worker.timer`
  - `Persistent=true` → `Persistent=false`. 재부팅 후 과거 slot을 자동 catch-up하지 않는다.
- `deploy/worker/install-systemd.sh`
  - unit 설치, `daemon-reload`, `disable --now cardrag-worker.timer`까지만 수행한다.
  - Worker를 시작하거나 timer를 enable하지 않는다.
- `docs/OPERATIONS.md`, `deploy/README.md`
  - helper 기반 service, 영속 timer disable, 수동 검증 절차, 재활성화 승인 경계를 명시했다.

## 검증

```text
systemd-analyze verify deploy/worker/cardrag-worker.service deploy/worker/cardrag-worker.timer
rc=0

shellcheck deploy/worker/install-systemd.sh
통과

sh -n deploy/worker/install-systemd.sh
통과

git diff --check
통과
```

resolved compose에서 비밀값을 제외한 identity만 비교했다.

```text
verified helper:
  image=cardrag-worker:019-fix24-11d1321cc9e6
  user=10001:10001
  project=cardrag-worker
  worker-state=cardrag-worker-state (external)
  codex-home=cardrag-worker-auth (external)
  paddle-models=cardrag-worker-paddleocr-models (external)
  serving=cardrag-serving (external)
  secret_names=webdav_username,webdav_password,openrouter_api_key
  selected identity SHA-256=6509ae65efe048f6e90d75ca919b20dcb7689613f844640724878cc330ce5390

legacy installed path:
  image=ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:285326...
  worker-state=cardrag-worker-v130-candidate-state
  codex-home=cardrag-worker-v120-recovery-auth-20260910
  selected identity SHA-256=4c298ef97a5b282e847e3c5b19a61d2809a64d4dadcebef361f08c80ea76ccfd
```

현재 운영 상태는 변경 전과 동일하다.

```text
current release=/opt/cardrag/019-fix24-11d1321cc9e6
stable generation=g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9
latest successful run=666c2d2ee9e24d04acf76eed58123a15 (shinhan, succeeded)
serving counts=woori980/kb757/shinhan958/samsung766/hyundai607/hana735/lotte707/bc122
nonselected_document_changes=0
running worker containers=0
```

Python/provider 코드 변경은 없어 전체 suite를 다시 실행하지 않았다. FIX_24의 1819 통과를 이번 설정 변경의 증거로 대체하지 않는다.

## 적용 상태와 남은 조치

**미적용**: 현재 `/etc/systemd/system/cardrag-worker.service`와 `.timer`는 이전 파일이다.

```text
cardrag-worker.service: ActiveState=failed, Result=exit-code, ExecMainStatus=1
cardrag-worker.timer:   ActiveState=active, UnitFileState=enabled, Persistent=yes
next elapse:            2026-10-23 03:00:00 KST
```

비밀번호 없는 sudo가 제공되지 않아 root 단위 설치/`daemon-reload`/`disable`을 실행할 수 없었다. 다음 명령을 root 권한으로 먼저 실행해야 다음 03:00 예약이 검증된 경로로 바뀌고 잠시 멈춘다.

```bash
cd /home/lee/projects/MCP_card_prd_detail
sudo deploy/worker/install-systemd.sh
systemctl is-enabled cardrag-worker.timer   # disabled
systemctl is-active cardrag-worker.timer    # inactive
systemctl show cardrag-worker.timer -p UnitFileState -p ActiveState -p Persistent
```

적용 후 예상 값은 `UnitFileState=disabled`, `ActiveState=inactive`, `Persistent=no`다. 검증·설정 교정과 사용자 승인 전에는 `sudo systemctl enable --now cardrag-worker.timer`를 실행하지 않는다. 재활성화 시 `systemctl list-timers cardrag-worker.timer`로 다음 KST 03:00을 확인하고, `Persistent=false`이므로 과거 slot이 즉시 catch-up되지 않는지 확인한다.

이번 턴에는 전체 Worker run, provider 호출, 백업 flush, DB 수정, serving 전환을 실행하지 않았다. 실제 예약 서비스가 새 helper로 성공하는지는 아직 미검증이다.