# 008 Executor — 첫 운영 Worker 용량 오류 조치

2026-10-07 KST. 사용자 요청: 에러 종료 원인 확인 및 조치. 구현 코드 `6b42a1a55899899be8fdab5d3b3555ef7b5390da` 그대로 사용한다. 이번에는 코드 결함 수정 없이 운영 용량 확보와 동일 run 재개를 수행했다.

## 확정 원인과 기존 실행 결과

- `cardrag-prod-008-first` (`e3a34489ff0a`)는 17:37:44 시작, 19:10:13 종료, exit 1 / OOMKilled false.
- run `5f60efbc4529434cb15b7cd43128b705`, SQLite run.error: **`Worker reserved free-space gate rejected v5 artifacts`**. 19:09:49의 `v5_capacity_preflight_failed`와 일치한다. 실제 저장 공간의 안전 여유를 포함한 예상 v5 export 최대치가 부족했다. state quota/파일 상한이나 OCR 오류가 원인이 아니다.
- 수집 degraded: 정상 7 issuer, 신한 ReadError 격리(4 attempts), 기존 신한 931 문서 carry. 다른 issuer는 수집을 완료했다.
- OCR 완료 5,513 / 실패 0, **캐시 재사용 5,511 / 실제 provider documents 2**. 진행 로그 5,513건을 전량 추론으로 해석하면 안 된다. corpus diff: historical 유지, missing_unjustified 0, retired 0. 이 결과는 실패 run 증거이며 새 generation 게시 완료를 의미하지 않는다.
- embedding 대상 derived views 615,840, unique misses 177, DB payload 3,532,075,812 bytes / rows 12,588,590. embedding/provider 변이가 아니라 그 직전 용량 gate에서 종료했다. MCP는 기존 007 이미지 healthy, 이전 generation 서비스 유지.

## 실제 조치

1. 실패 로그와 collection/performance 보고서를 `/opt/cardrag/008-6b42a1a/operations/recovery-20261007/`에 보존했다. 원래 종료 컨테이너·실패 run·OCR/PDF/CAS·SQLite를 삭제하지 않았다.
2. `docker buildx prune --builder cardrag-release-v1026 --all --force --filter until=24h`: 오래된 미사용 release build cache 7.426GB 정리.
3. 모든 컨테이너의 image 참조를 확인하고 사용되지 않는 과거 CardRAG 후보/CI image 태그 9개만 제거했다. 제거 목록 `unused-images.json`. 운영 Worker 008, 직전 rollback Worker 007, 현재 MCP 007은 명시적으로 보존했다. 다른 서비스 이미지·볼륨·WebDAV는 변경하지 않았다.
4. `docker builder prune --all --force --filter until=24h`: 오래된 default build cache 15.52GB 정리. cache의 shared layer 수치는 실제 물리 확보량과 같지 않으므로 filesystem 측정으로 확인했다.
5. 여유 공간은 조치 전 약 60GB에서 조치 후 **93,529,427,968 bytes (약 87.1 GiB)**로 증가했다. 위 출력 단위 GB/GiB를 구분한다. image 삭제/prune 중간값 67,551,498,240 bytes 대비 추가 약 26GB 확보.
6. 실패 run의 저장된 structure/views를 읽어 FTS text와 secondary index bytes를 재산정했다. issuer/lineage/revision/profile index에는 16MiB 추가 상향 여유, WAL에는 실제 WAL 대신 전체 7,077,064,704-byte Worker DB 크기를 예약하는 보수적 추정으로 **기존 `preflight_v5_capacity` 자체를 실행하여 통과**했다. 해당 검증은 read-only/네트워크 없음, OCR/embedding 호출 없음.
   - derived views 615,840 동일, FTS 319,469,760 bytes, secondary upper 853,397,900 bytes.
   - predicted DB 15,332,442,112 bytes, vector 10,089,922,560 bytes.
   - logical growth 32,519,417,856 bytes / peak growth 78,516,744,192 bytes.
   - state usage 53,075,443,689 bytes / free 93,527,597,056 bytes. peak + 기존 reserve 2GiB = 80,664,227,840 bytes보다 여유가 크며 128GiB state quota도 만족한다.
   - 이것은 재개 전 보수적 추정이며 runtime은 실제 ledger/WAL로 정확한 검사를 다시 수행한다. 기준/배수/reserve/상한을 낮추거나 gate를 우회하지 않았다. script 및 JSON은 운영 증거 디렉터리에 보존했다.
7. 기존 이미지/설정/볼륨으로 19:23:04에 동일 run을 재개했다:

```sh
/opt/cardrag/008-6b42a1a/operations/worker-compose.sh run -d --no-deps \
  --name cardrag-prod-008-resume01 worker run --resume 5f60efbc4529434cb15b7cd43128b705
```

새 컨테이너 ID `9e92eea6cbca7787424bd223aace03f3094547284855ae969a572113b87bc973`. resume는 기존 finite run의 캐시/체크포인트와 stage 상태를 재사용한다. 저장소 계약상 origin 목록은 다시 관측할 수 있으며 새 실제 개정 PDF가 있다면 정상 처리한다. 강제 OCR/Paddle/epoch 증가/reprocess apply/전량 임베딩은 요청하지 않았다.

## 검증 범위와 인계

코드는 바뀌지 않아 기존 CI success 및 전체 테스트 2,339 passed를 재사용한다. 추가 검증은 원인 SQLite 조회, 수집/OCR 보고서, 실제 저장 공간, 참조 없는 이미지 목록, 보수적 v5 capacity gate 통과, 동일 이미지·마운트·기동 상태 확인이다. 장기 배치의 완료나 최종 export/MCP generation 전환을 지금 확인한 것으로 쓰지 않는다.

다음 timer는 2026-10-08 03:00 KST, active 유지. 동일 worker.lock으로 중복을 방지한다. 현재 서비스 유지, 직전 rollback 이미지 1세트 보존, GC false 및 OCR codex/qwen 유지, OpenCode 운영 활성화 미실행.

사용자의 새 감시 대상:

```sh
docker logs -f --tail 50 cardrag-prod-008-resume01
docker inspect cardrag-prod-008-resume01 --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'
```

`exited exit=0 oom=false`가 프로세스 정상 종료 기준. `running exit=0`은 미완료. 컨테이너 AutoRemove=false로 종료 결과를 보존한다. 사용자 종료/오류 알림 뒤 collection report·carry OCR SHA·provider 호출수·generation/export/MCP를 검증한다. 장시간 감시는 하지 않는다.
