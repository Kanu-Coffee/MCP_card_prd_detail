# 008 Executor — 승인된 OpenCode 운영 전환 완료

2026-10-07 23:00 KST. 사용자가 OpenCode 운영 전환을 명시적으로 승인했다. PLAN §10.2와 006의 운영 provider 게이트를 실행했다. 기존 008 구현/image는 그대로 두고 host-local 운영 overlay를 변경했다.

## 적용 설정과 예약 실행

- 이미지 `cardrag-worker:008-6b42a1a`, 구현 commit `6b42a1a55899899be8fdab5d3b3555ef7b5390da` 유지.
- primary OCR **opencode / alibaba-token-plan/qwen3.8-flash / medium**. 전용 agent `ocr`, pinned CLI `1.18.34`, external OCR allowed true.
- provider child의 config `tools["*"]=false` / `permission["*"]="deny"`, 전용 agent에서도 동일 deny를 실제 생성 config에서 확인했다. 기존 구현의 `--pure --format json` 경로를 사용했다.
- 자동 OCR fallback은 기존과 동일하게 **없음**. Paddle/codex/OpenRouter fallback을 새로 추가하지 않았다. 특히 다른 provider의 예전 anthropic fallback model 값을 사용할 여지를 제거하기 위해 overlay의 fallback provider/model을 빈 값으로 고정했다.
- OCR cache read-write / epoch 0, 동일 state/auth/model 볼륨, 원격 GC false, pending reprocess 없음 유지. 기존 OCR/corpus/embedding는 변경하지 않았다.
- 적용 파일 `/opt/cardrag/008-6b42a1a/deploy/worker/compose.secrets.yaml`. 기존 image·secret mount를 보존하고 OpenCode 환경 설정만 추가한 host-local overlay를 staging 검증 뒤 원자 교체했다. `/etc/cardrag/worker.env`의 기존 API key를 Compose가 그대로 전달하므로 비밀값 복사/출력이나 새로운 secret 설치가 필요하지 않았다.
- `/opt/cardrag/current`와 기존 wrapper 및 systemd unit이 모두 이 overlay를 사용한다. `/etc/cardrag`/systemd 파일 변경이나 reload/restart는 필요 없다. **sudo 작업 없음**.
- timer active, 다음 **2026-10-08 03:00 KST** 정상 예약 배치부터 OpenCode 설정을 사용한다. 불필요한 전체 discovery/OCR 실사를 위해 추가 장기 Worker를 기동하지 않았다. 신규 PDF가 없으면 OCR 호출 0건도 정상이다.

## 수행 검증

1. base + 준비된 compose.opencode.yaml + 기존 host secrets 조합 `config --quiet` 통과. staged 및 실제 적용 조합도 통과했다. effective 설정에서 image/provider/model/두 effort 값/key 존재/fallback 없음/cache/GC를 검증하고 비밀 없는 subset만 저장했다.
2. 기존 운영 env의 Token Plan key 존재 확인. 값을 출력하거나 Git에 기록하지 않았다.
3. **합성 1페이지 / provider 실제 호출 1회 / 8.57초**: Pillow로 만든 `CardRAG OCR smoke test`, `Discount 10%` 이미지에 실제 OpenCode provider의 `recognize`를 호출하여 문구를 정확하게 식별했다. timeout 60초, 자동 재시도/fallback 없음, 카드 corpus/PDF 미사용. CLI version/실제 격리 도구 deny도 확인했다. 최초 script에서 미설치 fitz import가 호출 전 실패했으며, 기존 Pillow로 바꾼 후 실제 호출을 딱 한 번 수행했다. dependency 설치는 없었다.
4. 실제 적용 overlay로 별도 read-only script를 실행하여 `WorkerSettings` provider/model/effort와 API key 존재, cache/GC/no-pending을 확인했다. WebDAV stable `g-5f60efbc4529434cb15b7cd4-e05b9e8d2031` 불변 확인. 이것은 짧은 설정 검사이며 Worker batch가 아니다.
5. 현재 MCP `/health/ready` HTTP 200 / ready true 유지. 서비스 재시작 없음. 실제 카드 신규 OCR을 아직 수행했다는 주장은 하지 않는다. 합성 smoke가 인증·provider 실행·이미지 OCR 경로의 실호출 근거이고 다음 예약 run이 운영 적용의 후속 증거다.
6. rollback shell `shellcheck` 통과. 변경은 운영 설정과 문서뿐이며 동일 구현의 2,339 tests / CI success를 반복 실행하지 않았다.

## 롤백 및 증거

즉시 직전 codex 설정은 `/opt/cardrag/008-6b42a1a/operations/opencode-switch-20261007/compose.codex.rollback.yaml` 한 파일에 보존했다. image/state/OCR 원본 복제 없음. 운영 전환에 문제가 있으면 배포 target을 확인한 다음 아래 script로 다음 실행의 provider를 codex-exec / qwen3.8-flash / xhigh로 되돌릴 수 있다. 실행 중 Worker가 있다면 설정 교체는 해당 프로세스의 환경을 바꾸지 않으므로 종료/중지 판단은 별도로 수행한다.

```sh
/opt/cardrag/current/operations/opencode-switch-20261007/rollback-to-codex.sh
```

이 명령은 지금 실행하지 않았다. current가 다른 배포로 바뀌었으면 script가 중단하도록 검증한다. root env·systemd 변경 없이 기존 overlay 복원으로 동작한다.

증거 `/opt/cardrag/008-6b42a1a/operations/opencode-switch-20261007/`:
`smoke.py`, `smoke-result.json`, `applied-config.json`, `post-switch-check.py`, `post-switch-check.json`, staged/rollback overlay 및 rollback script. API key는 저장하지 않았다.

008 핵심 인수에 이어 승인된 006 OpenCode 운영 설정 전환도 완료했다. main 병합/정식 공개 릴리스는 수행하지 않았다. 예약 batch 완료 통보가 오면 실제 run의 선택 provider·cache 재사용·신규 PDF 호출 및 정상 종료를 확인한다. 추가 수일 대기·신규 문서 최소건수·전량 재OCR 조건은 만들지 않는다.
