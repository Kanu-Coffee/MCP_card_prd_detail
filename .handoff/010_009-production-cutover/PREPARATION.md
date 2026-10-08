# 010 운영 반영 준비 — sudo 실행 대기

- 작성일: 2026-10-08. 역할: Executor.
- 상태: **Git/CI/이미지/snapshot 준비 완료. 실제 전환은 SSH sudo 실행 대기. 운영 반영 완료 보고가 아니다.**

## 완료된 준비

- PR #42 생성 → GitHub CI run37731886246 success → main merge `8ac4712`.
- main의 apps/packages/Dockerfile/deploy가 인수된 `b544a80`와 동일함을 확인했다.
- 병합된 remote/local `codex/009-summary-stage-reuse` 브랜치를 정리했다. 공개 v1.0.33 태그/Release는 유지했다.
- `/opt/cardrag/009-b544a80`에 고정 code snapshot, 기존 host-local Opencode/secrets overlays, 새 절대 경로 compose wrapper 준비.
- 두 로컬 이미지 빌드 완료, revision b544a80 확인:
  - `cardrag-mcp:009-b544a80`, ID `sha256:307bdccc2cbcb9c035c8b0a4bd5274c4cb7dde09527e77b234e231183645c908`.
  - `cardrag-worker:009-b544a80`, ID `sha256:bb457d7d675278bb6eaefa6dd7c61375ef7005dda9fc5296e7eb57677ca349fb`.
- 기존 default builder 사용. 별도 Buildx daemon/volume 없음.
- Compose config 검증 및 기존·신규 전체 environment/volume 정의 동일 assertion 성공. image만 변경했다.
- Worker run --help의 skip/reuse/dry-run/publish-channel 옵션 확인. 새 이미지의 Opencode version1.18.34 확인. 실제 Worker 배치/유료 inference0.

## 필요한 SSH 명령

`sudo -n`과 비대화형 systemctl 조작이 실제로 인증 요구로 거절됐다. `/opt/cardrag` 쓰기 권한과 systemd 타이머 관리 권한은 별개다. 사용자에게 아래1개 명령을 요청했다:

```sh
sudo bash /opt/cardrag/009-b544a80/operations/cutover.sh
```

검토 가능한 script 사본: `evidence/cutover.sh`. `bash -n` 검사 완료. 실행 시 timer 활성/이전 current/네 이미지 존재/Compose 유효성을 확인하고, timer를 잠시 멈춘 뒤 Worker service inactive 및 Worker container 없음 조건에서만 교체한다. 기존 running Worker를 강제 종료하지 않는다.

current를009로 atomic 교체 → MCP 교체 → readiness 대기(최대90회, 간격2초, 요청 timeout5초) → timer 재개 → `operations/cutover-status.json` 기록. 중간 실패 시008 snapshot/MCP 복원과 timer 재개를 시도한다. CLI/systemd에서 실패하면 성공으로 보고하지 말고 current/readiness/timer를 다시 확인한다.

sudo 실행 완료 후 후속 Executor는 다음을 수행한다:

1. current→009, 실제 MCP image/healthy, timer 활성 및 다음 예약, status marker 확인.
2. `.venv/bin/python .handoff/010_009-production-cutover/evidence/http-smoke.py`로 실제 인증된5상품 summary+같은revision benefits bundle 검사. token은 화면/로그에 출력하지 않는다. 결과는 `evidence/operational-smoke.json`에 저장한다.
3. 007 source snapshot의 활성 참조/mount가 없음을 확인하고 정리.008 snapshot + Worker008/MCP007 image 한 쌍을 롤백1세트로 유지한다. 운영 volume/secret/OCR/cache/model은 삭제하지 않는다.
4. REPORT.md를 새로 작성하여 실제 전환/확인/정리 결과로 마감한다. 이 PREPARATION 문서를 덮어쓰지 않는다.

이번 준비 시점의 current는008, 기존 MCP는007, Worker service inactive/timer active다. 새 이미지가 준비됐다는 사실만으로 실제 서비스를 교체했다고 해석하지 않는다. 다음 예약 Worker를 강제로 시작하거나 장시간 모니터링할 필요는 없다.

## 증거

- evidence/pr.json, ci.json: 실제 PR/main merge 및 PR CI 결과.
- evidence/compose-preflight.json: image 이외 environment/volume 보존.
- evidence/images.json, worker-help.txt: 빌드 image identity/CLI 확인.
- evidence/cutover.sh: sudo 전환/실패 복원 script.
- evidence/http-smoke.py: 전환 후 실제 MCP 확인 script.

사용자 Excel3개, 기존 handoff 역사/운영 volume은 변경하지 않았다.
