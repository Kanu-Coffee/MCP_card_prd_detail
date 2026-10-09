# 013 운영 기동 후속 보고 — 최종 상태

작성: 2026-10-09, Executor / Codex.

**이 문서는 OPERATIONS_REPORT.md의 첫 기동 시각/ID와 running 판정을 대체한다. 선행 보고서는 첫 관측 시점의 기록으로 보존한다.**

## 최종 결과

운영 반영과 **Worker 정상 초기화 및 기동을 완료했다.** 배치 완료까지 모니터링하지 않고 사용자 완료 통보 후 검증한다.

- `/opt/cardrag/current` → `/opt/cardrag/013-3eaa35d`.
- 새 MCP healthy 및 `/health/ready` ready=true 확인. 로컬 공유 볼륨/옵션 증분 백업/기존03시 타이머 설정은 선행 보고서와 같다.
- 최종 감시 대상: **`cardrag-worker-013`**.
- 컨테이너 ID: `0727196baec5f1b7ef2ae88b73c25d2da2cce7d669999366823b1487434c61b4`.
- 실제 기동: **15:43:43 KST**.
- **15:44:07.925 `Worker startup completed elapsed_seconds=24.037`**.
- Qwen DeepInfra preflight 통과: samples24, minimum_repeat_cosine1.000000, minimum_cross_provider_cosine0.995534.
- **15:44:21 삼성 discovery records565/warnings0** 및 running 확인. 이 시점부터 장시간 진행 모니터링을 중단했다.
- 자동 삭제하지 않으므로 종료 후에도 로그와 ExitCode를 확인할 수 있다. 이번 수동 실행은 systemd service 상태 대신 위 컨테이너를 감시한다.

## 초기 오류와 조치

첫 두 기동은 OCR contract discovery 이후, Qwen embedding startup completed 이전에 `worker_unexpected_failure`로 종료했다. 두 번째 로그에는 OpenRouter429 rate_limit 재시도가 있었다. 독립 embedding preflight는 통과했다. 오류 JSON이 원인 예외를 숨겨 첫 두 실패의 정확한 예외는 확정할 수 없으며, 외부 preflight 호출 실패 가능성을 기록한다.

최종 기동에는 실제 CLI/동일 signal shutdown/동일 설정을 유지하면서 예외 유형/프레임/secret 마스킹 메시지만 추가하는 호스트 diagnostic wrapper `operations/worker-entry.py`를 사용했다. 공급자 preflight, 모델/차원 검증, 본처리 gate를 우회하지 않았다. 최종 기동에서도429 재시도 후 preflight를 통과하고 정상 discovery에 진입했다. 정기03시 systemd 실행은 본래 이미지 CLI 그대로 사용한다.

실패 컨테이너의 로그는 운영 폴더의 `operations/worker-startup-failed-01.private.log`, `worker-startup-failed-02.private.log`(0600)로 보존하고, 종료된 두 컨테이너만 삭제하여 orphan 혼동을 없앴다. 원격 backup/운영 volume/기존 rollback은 삭제하지 않았다.

## 사용자 감시

```sh
docker logs -f --tail 100 cardrag-worker-013
docker inspect --format 'status={{.State.Status}} exit={{.State.ExitCode}}' cardrag-worker-013
```

exited 이후 종료 코드와 마지막 결과 JSON을 확인한다. running 상태의 ExitCode0은 완료 의미가 아니다. 별도 `cardrag-backup`이 계속 running인 것은 Worker 완료 판정과 무관하다.

최초 inventory 백업은 아직 pending이며 실제 원격 백업 완료를 주장하지 않는다. 배치 완료 통보 후 새 local generation 활성화, summary/bundle/PDF, collection degradation, OCR 재사용/신규 건수, backup pending/commit/원격 index, 디스크 여유를 검증한다. 추가 sudo 명령은 필요하지 않았다. 공개 GitHub/Docker Hub release는 이번 운영 반영에서 수행하지 않았다.
