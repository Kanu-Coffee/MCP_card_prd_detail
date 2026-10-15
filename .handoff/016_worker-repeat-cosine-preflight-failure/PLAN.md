# 016 — 10/16 Worker 반복 임베딩 품질 검증 실패 진단·복구

- 작성: 2026-10-16 05:17 KST, Planner Codex.
- 조사 범위: 당일 00:00–05:17 KST의 systemd journal, startup 영속 이력, 현행 코드/운영 설정, MCP current/ready, 백업 ledger 및 직전 handoff.
- 상태: **계획 발행. 원인 진단·구현·복구 실행은 미완료.** Planner는 실제 임베딩/API 재시험, full batch, 배포, 데이터 삭제를 수행하지 않았다.

## 1. 목표와 보존 조건

1. 오늘 03시 예약 실행이 초기검증에서 중단된 원인을 수치와 재현 증거로 좁힌다.
2. 반복 cosine 실패의 진단 공백을 보완하고, 품질 gate를 유지하면서 복구 가능한 운영 절차를 마련한다.
3. 진단·짧은 검증 후 안전한 복구 배치를 1회 실행하고, 초기화 성공/전체 완료/수집 degraded/백업 결과를 구분해 보고한다.

기존 모델/4096D FP32/tokenizer/정밀도/provider pinning/기본 반복 cosine 임계값 **0.999**를 보존한다. 실패를 숨기기 위해 threshold를 낮추거나 gate를 생략하거나 이전 성공 결과를 재사용하지 않는다. 네비우스 등 다른 provider/model로 자동 전환하지 않는다. auth/state/SQLite·WAL/PDF·OCR·embedding cache/Paddle 모델/current/rollback/백업 증거를 보존한다. 전량 OCR 재처리·원격 전체 GC·공개 릴리스는 범위 밖이다.

## 2. Git·운영 기준점

- 저장소 `/home/lee/projects/MCP_card_prd_detail`, branch `main`, HEAD `652924c`.
- 구현 commit `741cfe0a21ccebdae332baeba9ce6a2608809f3c`. 운영 image `cardrag-worker:015-741cfe0`, 실제 ID `sha256:0e61d7d919eeccdc0f3334152d51933a83060160c5f66e9fca6780f4d0d1b01e`.
- `/opt/cardrag/current -> /opt/cardrag/013-50a0129`. 실제 `deploy/worker/compose.secrets.yaml:4`는 수정 이미지를 지정한다. systemd의 working directory/env/Compose 조합도 확인했다. 과거의 “이미지 미반영” 문제는 오늘 실패의 원인으로 보지 않는다.
- OCI labels unknown/dev 제약 및 설치 source hash 매핑은 015/FIX_02 및 FIX_02_REPORT에 기록돼 있다. 새 이미지 생성 시 실제 source/image ID를 기록한다.
- 기존 untracked 문서 2개: `015…/ACCEPTANCE_FIX_02.md`, `015…/RUNTIME_REVIEW_20261015.md`. 사용자 작업으로 보존하고 덮어쓰기/reset/delete하지 않는다.
- 기존 handoff 최대 번호 015이므로 신규 016을 사용한다. 015 PLAN/REPORT/FIX_01/FIX_02 보고서 및 승인/실기동 기록을 참고하되 현재 로그를 사실의 기준으로 삼는다.
- 실행 중 Worker 없음. MCP `cardrag-mcp:013-50a0129` healthy, `/health/ready` → `{"ready":true}`. timer active, 다음 **10/17 03:00 KST**.

## 3. 오늘 실행의 확정 사실

원본 조회:

```sh
journalctl -u cardrag-worker.service \
  --since '2026-10-16 00:00:00' --until '2026-10-17 00:00:00' --no-pager
```

- systemd invocation **`3c964a38aeb2442eb7f4156642b47938`**.
- 03:00:00 예약 시작 → 03:00:20 exit **1**, Result=exit-code. 다른 당일 재기동 기록 없음.
- startup correlation **`10529ce0efd20c05254b62d79d7d55f6`**.
- 03:00:10.925 최소 기동 용량 통과: available **90,029,445,120 B**, minimum **2,147,483,648 B**.
- OCR resolver 0.818초 통과(Codex 0.162.1), tokenizer 0.357초 통과.
- 03:00:15.297 Qwen endpoint preflight 시작: attempt_max=2, deadline=600초.
- 03:00:20.490 실패: **category=contract / reason_code=repeat_cosine_below_gate / provider_id=deepinfra / elapsed=5.192초 / attempt=1/2**. status_code/retryable/HTTP attempts는 null.
- Worker 전체 elapsed **18.718초**. 최종 CLI JSON에도 phase/provider/failure_reason_code가 보존됐다.
- state `startup-attempt-history.jsonl`에는 같은 correlation의 capacity/OCR resolver/tokenizer passed 및 Qwen terminal 이력이 존재한다. **무로그 종료나 이력 소실 문제가 재발한 것은 아니다.**
- pipeline entry/수집/OCR/게시/backup 진입 증거 없음. 초기검증 실패 전 실제 임베딩 sample 요청은 수행되므로 “외부 호출 자체가 없었다”고 표현하지 않는다. 당일 실행의 신규 OCR 상품은 없다.

## 4. 원인 해석과 남은 불확실성

`embedding_v5.py:preflight_openrouter_qwen_providers`는 provider별 동일 sample batch를 두 번 요청하고, 정규화된 대응 벡터의 cosine 최솟값을 0.999와 비교한다. DeepInfra부터 검사하므로 현재 코드의 `repeat_cosine_below_gate`는 **DeepInfra 반복 결과의 최소 cosine이 기준 미달**이라는 뜻이다. HTTP 연결 오류·401·dimension 불일치·FP8 거절·deadline 초과와 다른 실패다. 해당 반복 gate에서 중단됐으므로 오늘 Nebius 비교까지 통과했다고 주장할 수 없다.

현재 예외에는 provider와 reason만 있고 **실제 최소/평균 cosine, 실패 sample index/hash, 미달 sample 수, 두 요청의 안전한 식별자**가 없다. 0.99899와 크게 다른 벡터가 같은 오류로 남아 편차 크기/영향 범위를 진단할 수 없다. 응답 원문은 이미 사라졌으므로 오늘 과거 실패의 실제 값을 사후 계산했다고 꾸미지 않는다.

원인 후보는 provider 반복 응답의 변동, route/backend 교체, sample별 응답 index/벡터 대응 문제, 로컬 수치/계약 처리 오류 등이다. 특정 원인으로 확정하지 않는다. 직전 정상 startup의 repeat=1.0은 오늘 안정성의 증명이 아니며, 10/14의 generic contract를 동일 원인으로 소급 확정하지 않는다.

**재시도 해석:** 015의 startup retry allowlist는 metadata_not_json/shape_invalid/no_allowlisted_endpoint/provider_endpoint_missing 4종이다. 반복 cosine 실패는 의도적으로 제외됐으므로 max=2여도 1회 후 terminal이 정상 코드 경로다. 재시도 기능이 고장났다고 표현하거나 품질 실패를 무조건 transient로 바꾸지 않는다. deadline도 5.192초 실패의 원인이 아니다.

## 5. 용량·서빙·백업의 현재 상태

- 조사 시 root available **89,817,141,248 B**, 78%. 최근 capacity observation required **73,625,644,728 B** 대비 약 16.19GB 여유. 이는 직전 배치 기준 참고치이며 새 배치 정확한 gate 통과를 보장하지 않는다. 오늘은 embedding 이전의 동적 용량 gate에 도달하지 않았다.
- MCP current는 **`g-ec1255d924ab4c87a8a12491-8f4256acf3e6`**, run `ec1255d924ab4c87a8a12491b762fc02`의 10/15 복구 배치 결과를 유지한다. healthy는 10/16 데이터 갱신 성공의 증거가 아니다.
- backup ledger 읽기 전용 조회: last_backup_at **10/15 09:38:39 KST**, runs_since_backup=0, backup_pending 전체 0행. 오늘은 backup 진입 전 실패했으므로 이를 오늘 신규 증분백업 성공으로 표시하지 않는다. 직전 원격 5개 항목·인덱스 readback 정상 근거는 015/RUNTIME_REVIEW에 있다. 오늘 동일 원격 검증을 불필요하게 반복하지 않았다.
- 신한·롯데 수집 degraded 및 stale carry 문제는 015의 후속 이슈다. 오늘 수집 자체가 시작되지 않았으므로 오늘 원격 수집 실패 횟수를 임의로 가산하지 않는다.

## 6. Executor 실행 계획

### A. 원본 보존·실패 수치 진단 보완 (P0)

1. Git/status, 위 journal/invocation/correlation, image/source hash, 최신 timer/writer/available을 재확인한다. private operations에 원본 증거를 보존하고 공개 handoff에는 필요한 식별자/분류/수치만 남긴다.
2. 품질 오류에 제한된 structured details를 추가한다: gate threshold, measured minimum/mean cosine, 미달 count, sample_count, 최초/최저 실패 index와 입력 SHA256, allowlist provider, profile/metadata hash. 숫자는 finite/range를 검증하고 문자열/배열 개수를 제한한다.
3. 필요 시 첫/반복 응답의 sample별 vector SHA와 허용된 request ID를 private 진단에 기록한다. auth/header 전체/응답 원문/벡터 원본/환경/무제한 traceback을 journal에 쓰지 않는다. 외부에서 제공한 식별자는 길이/문자 allowlist를 통과한 경우만 취급한다.
4. reason/provider/quality details를 예외 → CLI 분류 → stage log → 영속 history/최종 실패 JSON까지 안전하게 보존한다. quality failure의 retry 결정은 명시적으로 남기고 HTTP attempts와 stage attempt를 구분한다. 현재 null fields를 미실행/미적용과 구분해 해석 가능하게 한다.

### B. 원인 재현과 품질 보호 (P0)

1. 네트워크 없는 기존 fixture로 응답 순서/index rebinding, sample 입력 동일성, 정규화/finite/norm/dimension, cosine 계산·경계(0.999 이상/미만), 한 sample만 변동하는 경우를 먼저 검증한다. 기존 gate가 올바르게 거절하는지 확인한다.
2. 수치 진단을 보완한 동일 운영 설정의 production startup-check를 **1회** 실행한다. 이는 임베딩 sample 과금이 가능한 실제 검사이며 corpus/OCR/게시/백업은 실행하지 않는다. 인증·모델·provider·정밀도/계약을 바꾸지 않는다.
3. 재현되면 측정 수치와 sample별 분포/metadata hash를 정상 근거와 비교한다. 필요한 추가 probe는 해결할 가설·최대 횟수(추가 1회 이내)·호출/시간 예산을 명시하고 실행한다. 성공할 때까지 반복하는 방식은 금지한다.
4. 비재현이면 “현재 재검증 통과, 과거 정확한 수치/원인은 미확정”으로 보고한다. 정상 재시험으로 과거 품질 실패를 무효화하지 않는다. provider 장애라고 확정하거나 기본 threshold를 완화하지 않는다.
5. local index/계산 버그가 재현되면 최소 수정한다. provider 품질 편차면 안전한 이유/샘플 증거로 운영에 제시하고 실패 상태를 유지한다. 다른 provider 전환은 cache/profile/serving query 호환 영향이 있으므로 이 계획에서 자동 수행하지 않는다.

### C. 복구 및 재검증 정책 (P1)

1. 현재 결정적인 품질 오류 비재시도 정책을 기본 유지한다. 관측된 일시 변동에 대한 운영 재검증과 하나의 실패 샘플 재호출을 구분한다.
2. 자동 재검증이 필요하다는 재현 근거가 확보되면 별도 명시 정책으로 최대 1회 추가·공유 600초 예산 내·전체 원래 sample/provider 검사를 새로 수행하는 설계만 검토한다. 개별 불량 sample을 성공할 때까지 교체하거나 정상 결과만 선택하지 않는다. 모든 실패 이력을 보존하고, 추가 검사도 미달이면 반드시 nonzero 종료한다. 기존 015 비재시도 원칙에서 변경한 이유·허용 조건·편향 위험을 REPORT에 명시한다. 근거가 없으면 자동 retry 변경을 구현하지 않는다.
3. systemd 무조건 Restart나 전량 pipeline 재실행으로 가리지 않는다. 기동 실패/품질 저하/오늘 미갱신 상태를 운영자가 한 번에 조회할 수 있는 요약을 문서화한다. 외부 메시지/알림 전송은 별도 지시 없이 하지 않는다.

### D. 짧은 검증·운영 반영·1회 복구 (P1)

1. 관련 테스트/lint/type를 통과시키고 필요한 Worker 이미지만 재현 가능하게 빌드한다. source commit/image ID/installed source SHA를 기록하고 기존 검증된 rollback을 보존한다. capacity 확보량에 빌드 증가분도 포함한다.
2. 변경 코드와 실제 운영 이미지가 일치하는 production startup-check 결과를 확인한다. B의 1회 검증을 가능하면 최종 이미지에서 수행하여 중복 과금/검증을 줄인다.
3. 품질 gate가 정상 통과하고 writer가 없으며 최신 용량 gate를 충족할 여유가 있을 때만 full recovery를 **1회** 시작한다. 진단이 실패하면 corpus를 시작하지 않고 blocker/필요 조치를 보고한다.
4. 명명 컨테이너 또는 systemd journal로 로그 보존. 초기화 통과 후 긴 실행을 계속 폴링하지 말고 monitor 명령과 전체 완료 미확정 상태를 인계한다.
5. 완료 통보 시 run/generation/exit/status, OCR cache hit·신규 provider 문서와 상품, issuer freshness/carry, embedding misses, 게시/MCP current/ready, backup pending/lost/last_backup 및 free/required를 확인한다. 기존 015 배치 성공을 새 이미지 성공으로 대신하지 않는다.

## 7. 관련 파일·필수 검증

| 파일 | 목적 |
|---|---|
| `apps/cardrag-worker/src/cardrag_worker/embedding_v5.py` | repeat gate 수치/샘플 진단 및 응답 정규화/대응 검사 |
| Worker `cli.py` | 안전한 quality details 보존, retry 결정/공유 deadline/종료 이력 |
| Worker `settings.py`, `deploy/worker/compose.yaml` | 정책을 변경할 근거가 있을 때만 설정 추가/정합성 검사 |
| `apps/cardrag-worker/tests/test_embedding_v5.py` | 실제 mock response 기반 repeat 품질/경계/index 회귀 |
| `apps/cardrag-worker/tests/test_cli_settings_provider.py` | failure details·history·retry/deadline·기밀 미노출 |
| `apps/cardrag-worker/tests/test_lock_concurrency_multiprocess.py` | lock-before-DB/busy 보존 |
| `docs/OPERATIONS.md` | 품질 실패 재현/복구/미갱신 상태 확인 절차 |

```sh
uv run --no-sync pytest -q \
  apps/cardrag-worker/tests/test_embedding_v5.py \
  apps/cardrag-worker/tests/test_cli_settings_provider.py \
  apps/cardrag-worker/tests/test_lock_concurrency_multiprocess.py
```

먼저 해당 -k 선택을 사용해도 된다. 필수 회귀: threshold 미만 nonzero·정확 경계 허용·최소값/index/hash/미달 count 전달·NaN/무한/큰 배열/secret 문자열 미노출·정상 startup 불변·기존 quality 오류 비재시도·진행 중 deadline/취소 cleanup·history symlink/FIFO 보호. 기존 lint/type 검사 수행. 새로운 정책이 있으면 bounded 횟수·원 gate 전체 재검증·실패 이력 보존을 추가 검증한다.

## 8. 인수 및 handoff

- 발생 원인 분류와 오늘 미확정 수치의 한계가 정직하게 기록되고, 재발하면 측정값/실패 sample/복구 결정을 확인할 수 있다.
- 품질 기준을 완화하지 않으며 통과하지 않은 embedding으로 게시하지 않는다.
- 개발 수정/운영 반영/startup-check/full recovery 완료 상태를 분리하고 필요한 증거를 제공한다. 새 full recovery가 실패·진행 중이면 운영 복구 완료로 표시하지 않는다.
- 10/17 예약을 기다리는 것을 인수 조건으로 추가하지 않는다. 원인 미재현만으로 진단 개선을 미루지 않는다.
- Executor는 본 PLAN과 현 Git/운영 상태를 읽고 실행 후 **이 디렉터리의 최초 `REPORT.md`**를 새로 작성한다. 변경/명령·결과/유료 probe 횟수/계획 편차/남은 위험을 기록한다. 기존 handoff를 수정하지 않고 이후 correction은 FIX_01부터 진행한다.
