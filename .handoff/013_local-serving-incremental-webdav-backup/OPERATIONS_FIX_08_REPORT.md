# 013 FIX_08 운영 반영 및 Worker 재기동 보고서

작성: 2026-10-09, Executor / Codex. 사용자 운영 반영·재기동 승인에 따라 수행.

## 결과

- 인수 commit `50a0129dcc210be06e49e7b5389e9e49383046f1`의 Worker/MCP 이미지를 Dockerfile의 정식 target으로 빌드하고 배포했다.
- 운영 경로: `/opt/cardrag/current -> /opt/cardrag/013-50a0129`.
- 운영 MCP: `cardrag-mcp:013-50a0129`; 20:30경 `http://127.0.0.1:18015/health/ready` HTTP200 / `{"ready":true}` 확인. 대용량 기존 로컬 generation 초기 검증에 약8분 소요됐다. host proxy nginx reload 완료.
- Worker: `cardrag-worker-013`, OpenCode / `alibaba-token-plan/qwen3.8-flash`, reasoning medium, fallback 없음.
- Worker 최종 기동20:26:36, startup 완료20:27:09.914, DeepInfra Qwen embedding preflight 통과. Samsung/KB/Lotte/BC 수집 진행 확인. 기존 신한 연결 실패는 `issuer_origin_network`로 기록하고 나머지 수집을 계속한다.
- 매일03시 `cardrag-worker.timer` active. 기존 systemd service는 current 경로의 새 compose/image를 사용한다. sudo 명령이나 systemd 변경은 필요하지 않았다.
- 배치 완료를 기다리지 않고 startup 및 초기 진행까지만 확인했다. 새 batch 완료·새 generation 검증·원격 backup 완료는 아직 확인하지 않았다.

## 배포 검증 및 데이터 유지

- 새 Worker 이미지 자체(호스트 소스 override 없음)에서 실제 영향 대상328건을 재검증: cache hits328, provider calls0, 원OCR/variant328 유지, native5건 provider/model 모두 정확. network none / 운영 volume read-only / 임시 출력으로 검증했다.
- secrets/environment, Worker state/auth/Paddle, MCP state, `cardrag-serving` volume 유지. Worker RW / MCP RO local publication 유지. WebDAV optional immediate inline backup 설정 유지.
- 별도 `cardrag-backup`은 정지 유지하여 Worker와 경합하지 않는다. pending/spool/receipts 및 원격 백업은 보존했다. 배치 완료 통보 후 필요 시 수정 이미지의 background retry를 재개한다.
- 중단된 이전 Worker 컨테이너를 `cardrag-worker-013-stopped-29f7877`로 보존했다. 새 Worker는 종료 시 자동 삭제하지 않아 사용자가 완료/오류 상태를 확인할 수 있다.
- 지정 rollback은 기존 `/opt/cardrag/v1.0.34` 그대로다. 운영/증거용 이전 폴더는 이번 배포에서 삭제하지 않았다.

## 기동 중 운영 잔여물 조치

초기 기동은 본 처리 시작 전 capacity preflight에서 종료했다. 실제 원인을 diagnostic wrapper로 확인했고 용량 gate를 우회하지 않았다.

1. 이전 Executor 검증의 `preflight_state.sqlite3-wal` 1개가 root:root /0600이라 UID10001이 읽지 못했다. 파일 소유권만10001:10001로 복구했다. 오류는 scanner가 permission error를 tree-changed로 표현한 것이다.
2. 이전 중단 run `29f7877bcc2e4e13b19447f023975e16`의 한 document rendered 디렉터리에 OpenCode 임시 node_modules 두 개가 남아14개 symlink로 strict capacity gate를 막았다. 두 디렉터리를 `/opt/cardrag/013-50a0129/operations/interrupted-opencode-node-modules.tar.gz`(약21MB,0600)에 먼저 보관한 뒤 해당 임시 디렉터리만 정리했다. PDF/OCR/checkpoint/manifest를 삭제하지 않았다.
3. 정상 재기동에서 capacity preflight 통과와 startup 완료를 확인했다. 최종 Worker는 기존 CLI 호출을 그대로 사용하는 sanitized diagnostic wrapper `operations/worker-entry.py`를 사용한다. timer는 정식 이미지 CLI로 실행한다.

## 사용자 모니터링

```sh
docker logs --since 2026-10-09T11:26:36Z -f cardrag-worker-013
```

초기 실패 로그가 같은 컨테이너에 남으므로 위 최종 시작 시각 이후 로그를 기준으로 확인한다.

```sh
docker inspect cardrag-worker-013 --format '{{.State.Status}} exit={{.State.ExitCode}}'
```

running은 진행 중, exited이면 exit code와 최종 결과 로그를 확인한다. 완료 또는 오류를 사용자께서 알려주면 batch 결과, 캐시 재사용/신규 OCR 사유, local generation 활성화, MCP 응답, backup pending/commit을 후속 검토한다.

근거: `evidence/operations-fix08-deployment.json`; 이미지 빌드 로그 및 실제 이미지328건 결과는 운영 폴더의 operations 아래 보관한다. 이번 작업은 공개 GitHub/Docker 릴리스 작업이 아니다.
