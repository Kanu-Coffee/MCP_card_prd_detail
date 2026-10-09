# 013 운영 반영 및 Worker 기동 보고

작성: 2026-10-09, Executor / Codex.
기준 commit: `3eaa35d1af64a36ab2974b756700e3d30d403c6d`.

## 결과

**운영 반영 및 Worker 기동 완료. 배치 완료/실제 백업 완료 판정은 사용자 완료 통보 후 진행한다.**

- `/opt/cardrag/current` → `/opt/cardrag/013-3eaa35d`.
- Worker 이미지: `cardrag-worker:013-3eaa35d` (`sha256:3674095503987f4cc765b30165e42883a52432c4bb8a37982b37ef573cfe65e4`).
- MCP 이미지: `cardrag-mcp:013-3eaa35d` (`sha256:d660aabc9bd2dec317d579bd9bf3e3c07524ad63d601648b29a639208fe45998`).
- `cardrag-mcp` 교체 후 `http://127.0.0.1:18015/health/ready` 응답 `{"ready":true}`. 기존 데이터 시작 검증에 약7분 소요. host proxy nginx reload 완료.
- Worker: `cardrag-worker-013`, ID `8a1c8658b0567df009f61d9d861516c7faefc76268dff4775790e72af0d569d9`, **15:39:52 KST 기동**, running 확인. 자동 삭제하지 않아 종료 후 로그/ExitCode 확인 가능.
- 기존 03시 `cardrag-worker.timer` active 유지. 이번 수동 Worker는 systemd service가 아닌 위 Docker 컨테이너로 감시한다.

## 배포 설정과 초기 자료

Git HEAD를 새 운영 폴더에 추출하고, 기존 실제 secrets overlay와 외부 state/auth/Paddle volume을 유지했다. OpenCode `alibaba-token-plan/qwen3.8-flash`, medium, fallback 없음 설정을 보존했다. 환경변수/키를 보고서나 Git으로 복사하지 않았다.

Worker RW / MCP RO의 `cardrag-serving` 공유 볼륨을 만들고 UID/GID10001 권한을 설정했다. 양쪽 publication transport는 local이다. 외부 볼륨을 explicit external로 설정했다.

오늘03시 정상 완료된 run `025ce35739944d8facfdda4c99961f0c`의 기존 sealed generation `g-025ce35739944d8facfdda4c-19895e1a91da`를 초기 local head로 게시했다. 기존 DB/벡터/OCR/PDF를 재사용했으며 provider-free 초기화에서 discovery/OCR/embedding 호출을 하지 않았다. CAS9,904개 고유 객체를 게시하고 local head 검증을 통과했다. 기존 Worker/MCP state와 WebDAV 자료는 삭제하지 않았다.

## 옵션 백업

운영 backup mode는 `immediate`: 전체 DB/벡터를 원격 서빙용으로 업로드하지 않고, 신규/미등록 OCR/PDF 백업 목록을 증분 처리한다. 초기 목록 등록으로 pending9,914건/4,563,632,652bytes, distinct pending OCR5,051건, lost0건이었다. **이는 초기 inventory 대기량이며 실제 신규 업로드량이나 백업 완료 증거가 아니다.**

`cardrag-backup` 컨테이너는 기존 운영 Compose 설정/secret/state와 새 Worker 이미지를 사용하며 `unless-stopped`로 자동 재시작한다. 초기15분 후부터15분마다 budget300초의 `BackupLedger.flush`를 실행한다. 내부 backup.lock으로 inline flush와 단일 writer를 유지하며 오류는 로그로 기록 후 다음 주기에 재시도한다. 이 프로세스는 OCR/embedding을 실행하지 않는다. 실제 host 경로를 사용하므로 새 sudo/systemd 설치 없이 구동했다.

초기 inventory는 기존 sealed OCR/PDF CAS를 보존한다. 기존 adopted/Paddle native 복구 메타데이터와 신규 index의 전체 복원성까지 이번 기동 단계에서 추가로 실증하지 않았다. 기존 원격 backup을 유지하며 배치 완료 후 원격 증분/index/복구 메타데이터를 확인한다. 초기 원격 객체 재사용/전송량도 그때 실제 로그로 판단한다.

## 감시와 후속 검증

```sh
docker logs -f --tail 100 cardrag-worker-013
docker inspect --format 'status={{.State.Status}} exit={{.State.ExitCode}}' cardrag-worker-013
# 별도 백업 로그
docker logs --tail 100 cardrag-backup
```

Worker가 exited가 되면 종료 코드와 로그 마지막 JSON 결과를 전달받아, local_generation_published/신규 generation 활성화/상품 응답/PDF/백업 상태/잔여 용량을 확인한다. running 상태의 ExitCode0은 완료 판정으로 사용하지 않는다. 이번 턴에서는 배치 완료까지 모니터링하지 않는다.

운영 실행 경로와 이미지 근거는 `/opt/cardrag/013-3eaa35d/operations/deployment.json`, bootstrap 결과는 `operations/bootstrap.log`, helper는 `worker-compose.sh`, `mcp-compose.sh`, `start-worker.sh`, `backup-compose.py`, `backup-retry.py`다.

롤백 근거는 `/opt/cardrag/v1.0.34` 한 배포를 지정했다. 새 helper `operations/rollback.sh`는 수동 Worker 종료 후에만 사용하며 current 복원/MCP 교체/proxy reload를 수행한다. 기존 원격 generation과 운영 state를 정리하거나 삭제하지 않았다. GitHub commit/merge/release는 이번 운영 반영 요청에 포함해 수행하지 않았다.
