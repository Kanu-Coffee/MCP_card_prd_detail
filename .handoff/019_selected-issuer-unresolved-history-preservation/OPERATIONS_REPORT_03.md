# 019 FIX_13 운영 반영 및 실제 백업 복구 검증 완료

2026-10-20 Codex. REVIEW_03에서 FIX_13 코드 검토를 통과한 뒤, 앞서 사용자가 승인한 “이상없으면 운영테스트까지” 범위에서 새 release를 활성화하고 실제 WebDAV 대상 선택 백업·index 회수·재시도를 수행했다. 기존 문서는 보존한다. **이번 선택 백업 실테스트는 성공했다.**

## 배포 및 복구점

- `/opt/cardrag/current` → `/opt/cardrag/019-e7fb8a0ba60c`.
- image `cardrag-worker:019-e7fb8a0ba60c`.
- image ID `sha256:c99752428ea1490d0e19f2267c6a7acd9d3d9b2aa4a07a11a0fb2bc1189c2d8f`.
- worker/core Python80개 source digest `e7fb8a0ba60c9a119db9cfc99fe634ab91a0542b221660bcf9a21b2b507cd264`. 정렬 상대경로+NUL+bytes+NUL 방식이며 unchanged py.typed는 별도 보존했다.
- HEAD `aaeb2c4` 위 검토된 worktree를 고정했다. image 내부 파일과 최종 worktree digest를 대조했으며 임의 commit/push는 하지 않았다. 제품 코드를 Reviewer가 수정한 것은 없다.
- 직전 runtime/dependency 기반 offline source-overlay build다. uv.lock/root·worker·core pyproject는 직전 release와 같으며, compose config도 image와 release build context 이외에는 동일하다. external state/auth/model/serving 볼륨 및 비공개 overlay를 유지했다. secret 값은 보고서/저장소에 넣지 않았다.
- 이전 release `/opt/cardrag/019-ba273106158e`를 보존했다. worker.lock/backup.lock 아래 source mode=ro online backup으로 새 ledger 복구본을 만들었다: `cardrag-worker-state:/deployment-backups/019-e7fb8a0ba60c/backup-ledger.sqlite3`, 24416256bytes, SHA256 `e9d64499c0619769052c44d6df84f20314a6c018ffff995891662265f9177f56`, quick_check=ok.
- 신규 Worker batch/worker-state 업무 변경이 없고 latest run도 원래 succeeded run임을 확인했다. 불필요한 7GB 복제 대신 worker-state 복구본은 기존 `deployment-backups/019-ba273106158e/worker-state.sqlite3`를 참조한다. 현재 serving pointer/manifest/READY는 새 복구점에도 보존·검증했다.

## 전제 근거의 안전한 복원

실환경에는 기존 canonical variant A의 receipt와 원격 객체가 남아 있으나 local spool은 정리돼 있었다. FIX_13은 metadata-only 주장을 신뢰하지 않으므로, 전송에 앞서 A manifest/READY/OCR body만 GET해 다음을 대조했다.

- 현재 canonical root의 receipt SHA/size와 실제 바이트 일치.
- canonical 모델/key 및 READY의 manifest/OCR hash 연결 일치.
- A와 현재 seal의 local B는 같은 source/contract이고 OCR output은 다름.

검증된 세 파일(1456B/285B/15545B)을 기존 파일을 덮어쓰지 않는 방식으로 `state/cache/ocr/{key}/`에 복원했다. 원장 SQL 조작·허위 receipt 생성·원격 overwrite는 하지 않았다. 대규모 전체 restore나 provider 호출도 없었다. 이 단계의 원격 동작은 GET뿐이다.

그 후 읽기 전용 진단은 conflicts2→0, missing2(아직 전송되지 않은 B metadata), remaining0/unbound0이었다. canonical A 바이트를 확인한 근거가 있어 정상 variant 라우팅이 가능해졌다.

## 실제 CLI 전송과 index 회수

대상 run `41ce678e665b47179f8bfb2d82273882`, selected=woori. 기존 보류2076B 두 건은 앞선 사용자 테스트에서 완료됐고, 이번1741B는 별도의 기존 variant 충돌을 해결하는 metadata다.

```bash
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps -T worker \
  backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
```

| 항목 | 첫 실행 | 두 번째 실행 |
|---|---|---|
| status | succeeded | unchanged |
| backup_scope_complete | true | true |
| uploaded_count | 2 | 0 |
| uploaded_bytes | 1741 | 0 |
| conflicts / missing / remaining / unbound | 모두0 | 모두0 |
| error | null | null |

첫 실행은 B manifest1456B/READY285B를 `v1/ocr-cache/native/{prefix}/{key}/variants/{B_ocr_sha}/...`로 전송했다. body_requests2/control_requests3/requests5다. 두 번째 실행은 전송0이고 committed index SHA도 동일해 멱등을 확인했다. `actual_uploaded_* = null` 필드는 client 속성 부재에 따른 값이며, 성공은 실제 GET 대조로 별도 확인했다.

## 원격 재검증과 격리 복원 검증

- A canonical manifest SHA `7ec3b38f7c19d0db6079e775c8bfdb1ee3e78682ccde5093eb7ef10ffc3e7a9a`, READY SHA `0297edfa67edf820aae259f11bab247de28faa07e3ee85c2aff42eca2299fc87`가 전후 동일하다. 기존 원격 A를 덮어쓰지 않았다.
- B manifest SHA `1aabcc1159576caa8a3f1839789d120dbb9fe0d0ed0edcbe44d71b5f253d1fff`, OCR SHA `1e8523461967f669747ff17ff1c6e8592a999fde5619dee010276137adc69020`는 local seal의 현재 결과와 일치한다.
- A/B metadata 네 파일 모두 실제 remote SHA/size, 현재 root receipt 및 committed index 항목이 일치한다. A의 누락 index 항목이 회수됐고 B variant 항목이 추가됐다. 각 manifest/READY의 모델 및 hash 연결과 OCR body도 GET 검증했다.
- 선택 expected1906경로 모두 receipt/index digest와 일치했다. 전체 index는16856항목이며 검증 시 SHA `cc766a0e673df0dc11fa48204f2df220a489ebdb87eaf828828d86c089abe8a9`였다. 두 번째 retry 후에도 같다.
- B의 원격 metadata와 OCR body를 새 복구점 아래 `restore-smoke/variant_B` 세 파일로 격리 재구성하고 모델/hash를 재검증했다. 전체 `backup restore` 명령을 돌리거나 운영 seed/serving을 복원 대상으로 덮어쓰지는 않았다.

## 데이터 보존과 운영 도구

전후 stable pointer 및 manifest를 준비 시 복사본과 **바이트 단위 동일**하게 확인했다. generation `g-41ce678e665b47179f8bfb2d-e884484ab66b`, 전체5537/우리980, 8사 구성 그대로다. 다른7사 문서·serving을 이번 백업 테스트에서 재처리/게시하지 않았다.

host monitor는 root-aware scope API와 호환되도록 보완했다. ledger root가 하나로 특정될 때만 해당 root의 matching receipt 근거를 표시하며, 여러 root면 모호함을 표시한다. monitor 숫자는 원격 재검증/종료 당시 결과/현재 CLI 진단을 대체하지 않는다. shellcheck·monitor compile·실제 이전 우리 run smoke를 통과했고 비선택 문서 변경0/stage0을 다시 읽었다. 상태/summary의 scoped 진단 및 snapshot 표시, live group 권한도 보존했다.

timer와 Worker service는 inactive, 실행 중 Worker 컨테이너 없음으로 마무리했다. 이번 실테스트는 백업 복구 범위이며 수집/OCR/embedding 배치를 반복하지 않았다. 전체 배치 속도 개선율과 나머지7사 수동 라이브 검증은 아직 별도 후속 항목이다. 018은 CLOSED를 유지하고 019를 8사 검증 완료로 닫지 않는다.

## 증거 및 재확인

새 release `operations/`에 `deployment.json`, `backup-verified.json`, `source-verified.json`, `evidence-restored.json`, `status-before-flush.json`, `flush-first.json`, `live-verified.json`, `flush-second.json`, `retry-verified.json`, `monitor-smoke.json`을 보존했다. 비공개 URL/auth는 이 JSON 보고서에 기록하지 않았다.

다시 확인할 때:

```bash
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup status --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
```

status는 read_only 진단으로 `backup_scope_complete=null`일 수 있다. 위 실테스트의 완료 판정은 원격 index까지 확인한 두 flush의 true와 별도 GET 대조에 근거한다. 정상 정책상 pending이 이후 생기면 범위/원인을 구분하여 해석한다.
