# 운영 Worker의 OCR·예약 시간 변경

운영에서 자주 바꾸는 두 설정의 절차입니다. 이미지 재빌드나 새 릴리스 배포 없이
다음 Worker 실행부터 적용합니다. 이 문서는 현재 관리 호스트
`/opt/cardrag/current`와 `operations/worker-compose.sh`가 있는 설치를 기준으로 합니다.

## 현재 값과 설정 우선순위

2026-10-27 09:09 KST 호스트 반영 확인:

| 항목 | 값 | 실제 설정 위치 |
|---|---|---|
| 예약 | 매일 10:00 Asia/Seoul | `/etc/systemd/system/cardrag-worker.timer` 및 systemd drop-in |
| OCR 기본 | `codex-exec` / `gpt-6.1-terra` / `high` | `operations/compose.runtime.private.yaml` |
| OCR 폴백 | `local-paddleocr` / `PaddleOCR-VL-1.6` | 같은 파일 |
| 실행 범위 | `round_robin` | 같은 파일 및 운영 환경 |

문서의 값보다 아래 명령으로 확인한 실제 설정이 우선입니다. helper는
`/etc/cardrag/worker.env`, 활성 release의 `deploy/worker/compose.yaml`,
`operations/compose.runtime.private.yaml`을 조합합니다. **private overlay의 명시 값이
우선하므로 worker.env만 수정하면 OCR 설정이 바뀌지 않을 수 있습니다.**
private overlay 전체나 전체 `compose config` 출력을 공유하지 마십시오. 비밀 값이
포함될 수 있습니다. 아래 필터는 필요한 옵션만 출력합니다.

```bash
OPS=/opt/cardrag/current/operations
"$OPS/worker-compose.sh" config --format json | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]["worker"]
keys = ["CARDRAG_ISSUER_EXECUTION_MODE", "CARDRAG_OCR_PROVIDER",
        "CARDRAG_OCR_MODEL", "CARDRAG_OCR_REASONING_EFFORT",
        "CARDRAG_OCR_FALLBACK_PROVIDER", "CARDRAG_OCR_FALLBACK_MODEL",
        "CARDRAG_CODEX_MODEL_PROVIDER"]
print(json.dumps({"image": s["image"], "options": {
    k: s["environment"].get(k) for k in keys}}, indent=2))'
systemctl cat cardrag-worker.timer
systemctl show cardrag-worker.timer -p ActiveState -p UnitFileState \
  -p TimersCalendar -p NextElapseUSecRealtime -p Persistent
systemctl show cardrag-worker.service -p ActiveState -p SubState -p Result
```

## 1. 변경 전 확인과 백업

Worker가 실행 중이면 종료를 기다립니다. 실행 중 설정을 바꾸거나 서비스를
재시작하지 않습니다. 변경 중 예약 기동을 방지하기 위해 timer를 중지합니다.
timer 중지는 이미 실행 중인 Worker를 중지하지 않습니다.

```bash
systemctl status cardrag-worker.service --no-pager
# 수동·검증 컨테이너도 실행 중이 아닌지 확인합니다.
docker ps --filter label=com.docker.compose.project=cardrag-worker \
  --filter label=com.docker.compose.service=worker
sudo systemctl stop cardrag-worker.timer

OPS=/opt/cardrag/current/operations
STAMP=$(date +%Y%m%dT%H%M%S)
# 배포본·데이터 전체 대신 작은 설정 파일만 백업합니다.
cp -p "$OPS/compose.runtime.private.yaml" "$OPS/compose.runtime.private.before-$STAMP.yaml"
systemctl cat cardrag-worker.timer
```

백업도 비밀 파일입니다. `0640`, `cardrag` 그룹을 유지하고 Git에 추가하지 않습니다.
timer도 변경한다면 기존 본체와 drop-in의 위치·내용을 별도로 기록합니다.

## 2. OCR 기본·폴백 변경

```bash
nano /opt/cardrag/current/operations/compose.runtime.private.yaml
```

기존 `services.worker.environment` 아래의 해당 키만 수정합니다. 파일 전체를
아래 예제로 교체하거나 `environment`를 중복 추가하지 않습니다.

```yaml
services:
  worker:
    environment:
      CARDRAG_OCR_PROVIDER: codex-exec
      CARDRAG_OCR_MODEL: gpt-6.1-terra
      CARDRAG_OCR_REASONING_EFFORT: high
      CARDRAG_EXTERNAL_OCR_ALLOWED: 'true'
      CARDRAG_OCR_FALLBACK_PROVIDER: local-paddleocr
      CARDRAG_OCR_FALLBACK_MODEL: PaddleOCR-VL-1.6
      CARDRAG_CODEX_MODEL_PROVIDER: ''
      CARDRAG_CODEX_MODEL_PROVIDER_BASE_URL: ''
      CARDRAG_CODEX_MODEL_PROVIDER_ENV_KEY: ''
      CARDRAG_CODEX_MODEL_CATALOG_JSON: ''
```

위 네 개 custom-provider 항목의 빈 값은 기존 Codex 인증 경로를 사용하기 위한
현재 운영 설정입니다. 다른 API 공급자로 전환할 때는 해당 provider의 인증·URL·
실행 환경까지 별도 확인해야 합니다. 모델명만 바꾸는 것과 공급자 전환은 다릅니다.
폴백 provider가 빈 값이면 폴백이 구성되지 않습니다. 폴백 구성만으로 모든 종류의
오류가 폴백되는 것은 아니며, 실제 오류 분류와 복구 가능 여부에 따라 결정됩니다.

설정 문법과 운영 초기화 경로를 검증합니다.

```bash
OPS=/opt/cardrag/current/operations
"$OPS/worker-compose.sh" config --quiet
"$OPS/worker-compose.sh" run --rm -T --no-deps worker startup-check
```

`startup-check`는 인증·실행 파일·OCR resolver·임베딩 초기화를 확인하지만,
**선택한 모델의 실제 OCR 요청 성공이나 폴백 전환 성공을 보장하지 않습니다.**
모델을 처음 도입할 때는 작은 테스트 이미지로 운영 `CodexOCRProvider.recognize`
호출을 별도 검증하고 모델·effort·판독 결과를 기록합니다. 카드사 전체 배치를
모델 접근 확인용으로 실행하지 않습니다. 2026-10-27 Terra/high는 실제 테스트 이미지의
`CARDRAG OCR TEST 20261027` 판독에 성공했습니다. 당시 근거는 운영
`operations/live/ocr-terra-20261027.probe.json`과
`ocr-terra-20261027.startup-check.json`에 보관했습니다.

## 3. 예약 시간 변경 및 sudo 반영

원하는 한국 시간으로 timer 사본의 `Description`과 `OnCalendar`를 수정합니다.
예: 오전 10시라면 `OnCalendar=*-*-* 10:00:00 Asia/Seoul`입니다.
`Persistent=false`, `Unit=cardrag-worker.service` 등 다른 설정은 유지합니다.

```bash
OPS=/opt/cardrag/current/operations
cp /opt/cardrag/current/deploy/worker/cardrag-worker.timer "$OPS/cardrag-worker-next.timer"
chmod 0640 "$OPS/cardrag-worker-next.timer"
nano "$OPS/cardrag-worker-next.timer"
# 아래 시각도 선택한 값으로 바꿉니다. 다음 실행 날짜를 확인합니다.
systemd-analyze calendar '*-*-* 10:00:00 Asia/Seoul'
```

`systemctl cat`에 drop-in이 있다면 먼저 그 내용을 확인합니다. 아래의 본체 교체
방식은 drop-in을 제거하지 않습니다. drop-in이 `OnCalendar`를 설정한다면 그 파일도
수정해야 합니다. drop-in으로 변경할 경우 기존 시간을 지우는 빈 `OnCalendar=` 다음에
새 `OnCalendar=...`를 넣어야 두 시간이 동시에 등록되지 않습니다.

다음 명령은 실제 서비스 계정의 Compose 접근 검증이 성공한 경우에만 설치합니다.
운영 배포본과 systemd 본체를 함께 갱신하여 다음 설치에서 이전 시간이 복원되는
일을 방지합니다. 마지막 활성화 전 병합된 예약이 원하는 **한 시간만** 있는지
확인합니다. 실패했다면 timer를 중지한 채 원인을 해결합니다.

```bash
sudo bash -s <<'SH'
set -euo pipefail
REL=$(readlink -f /opt/cardrag/current)
"$REL/deploy/worker/verify-service-access.sh" "$REL"
systemctl stop cardrag-worker.timer
install -m 0644 "$REL/operations/cardrag-worker-next.timer" "$REL/deploy/worker/cardrag-worker.timer"
install -m 0644 "$REL/operations/cardrag-worker-next.timer" /etc/systemd/system/cardrag-worker.timer
systemctl daemon-reload
systemctl cat cardrag-worker.timer
systemctl show cardrag-worker.timer -p TimersCalendar -p Persistent
SH

# 위 출력이 원하는 단일 시간과 Persistent=no인지 확인한 후 실행합니다.
sudo systemctl enable --now cardrag-worker.timer
systemctl list-timers cardrag-worker.timer --all --no-pager
```

당일 예약 시각 전에 활성화하면 당일부터 적용됩니다. 이미 시각이 지났다면
`Persistent=false`이므로 해당 시각을 즉시 재실행하지 않으며, 실제 다음 실행은
`list-timers`로 판단합니다. 수동 기동은 별도 운영 판단으로 결정합니다.
`enable --now`는 timer를 활성화하는 명령이며 Worker를 직접 시작하는 명령이 아닙니다.

2026-10-27에 사용한 `operations/apply-20261027-1000.sh`는 당시 release와
OCR 값을 고정 검증하는 일회성 스크립트입니다. 과거 `apply-fix*-*.sh`도 시간·날짜·
release가 고정되어 있으므로 다음 옵션 변경에 재사용하지 않습니다.

## 4. 변경 후 확인·기록·복구

- 위의 필터된 Compose 출력과 `list-timers`로 실효 설정을 다시 확인합니다.
- 소스 저장소의 `deploy/worker/cardrag-worker.timer`와 이 문서의 현재 값을 함께
  갱신합니다. 소스 파일 수정만으로 운영 호스트에 반영되는 것은 아닙니다.
- 운영 `deployment.json`의 현재 `verification.timer_schedule`,
  `cutover.timer_schedule` 및 새 변경 일시·OCR 옵션 기록을 갱신합니다.
  과거 변경 기록, 릴리스 노트, 닫힌 handoff 문서는 수정하지 않습니다.
- 다음 배치의 journal·terminal 결과·게시 generation으로 실제 실행을 확인합니다.
  timer 활성화 또는 과거 `ExecMainStatus=0`만으로 새 배치 성공을 판정하지 않습니다.

```bash
journalctl -u cardrag-worker.service --since today --no-pager
systemctl show cardrag-worker.service -p ActiveState -p SubState -p Result -p ExecMainStatus
```

문제가 있으면 timer를 중지하고, 이번에 만든 OCR 설정 백업과 timer 본체·drop-in의
변경 전 값을 복원한 뒤 동일한 검증·설치·활성화 절차를 수행합니다. 비밀 파일 권한을
완화해서 오류를 우회하지 않습니다.

OCR·예약 옵션만 바꾸면 순회 DB와 순서는 유지됩니다. state/auth/Paddle 모델/serving
볼륨과 image를 그대로 두고, 순회 이력이나 캐시를 초기화하지 않습니다. 기존 OCR의
재사용 여부는 OCR contract 호환성 판정에 따르므로 모델 변경이 전량 재처리를 뜻하지는
않습니다. 같은 날짜의 수동 추가 실행은 현재 하루 최대 3회 정책의 영향을 받습니다.
재부팅 후에도 순회 상태는 유지되지만, `Persistent=false`이므로 놓친 예약 시각은
자동 보충하지 않습니다.
