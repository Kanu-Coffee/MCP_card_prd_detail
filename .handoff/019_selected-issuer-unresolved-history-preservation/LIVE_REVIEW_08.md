# 019 LIVE_REVIEW_08 — 롯데 resume의 content variant 재구성 오류

2026-10-21 Codex. 사용자가 제시한 resume container의 runtime 실패를 조사했다. 현재 활성 FIX_21 및 run/실제 OCR 파일을 읽기 전용으로 대조하고, 실제 cache reader의 write hook을 비활성화한 읽기 재현과 별도 최소 fixture로 검증했다. 운영 DB/receipt/seal/체크포인트/원격을 수정하거나 provider·카드사 호출·배포·재기동하지 않았다. 관련 보완은 같은019의 FIX_22로 발행한다.

## 운영 사실

- run `49845932e05849d3a0110a2bc82d6c1c`, resume container `cardrag-live-lotte-resume-20261021T021643Z`, exited1.
- 11:17:51 KST PDF530/530 완료, cache_hits529/downloads0. 11:23:38 collection barrier complete. 11:27:46 prior local OCR346건 재사용 후보 발견. 11:32:36 generic worker_unexpected_failure로 종료.
- DB run error는 **OCR failure isolation bookkeeping failed**다. OCR succeeded330, structure330, views330 유지. 이전 한도 실패 document의 stage는 retry/attempt0이며 새 systemic failure report는 없다. 남아 있는 ocr-systemic-failure.json은 최초 중단의10:58:05 기록으로 이번 실패 원인으로 재사용하면 안 된다.
- 이번 performance의 ocr_completed8/expected707/missing699는 **현재 재개 시도의 처리 카운터**다. 영속 stage/파일 기준 누적 완료330/미완료377과 구분한다.
- preflight에 저장한330개 OCR body fingerprint를 종료 후 모두 대조했고 변경/누락0. chunk checkpoint97개도 파일 SHA 일치/누락0이다. live stable은 현대 `g-10f236331a2744f78980f2b8-9d16cfdbae66`, 기존5545건이며 새 게시가 없다.
- 이번에는 Worker 종료 후 WAL 파일이 없고 활성 Worker도 없음을 확인했다. 일반 stock SQLite mode=ro가 unable-to-open을 반환하여, sidecar 생성 없이 offline main DB만 읽는 mode=ro&immutable=1로 조사했다. 운영 journal/lock을 수정하지 않았다.

## 원인 연결

1. pipeline.py `_retained_identity_conflict`(:5437)은 현재 run에 ocr.md가 **존재하기만 해도 True**다. 원래 docstring의 "partner seal SHA와 다를 때 충돌"보다 강하다. resume330건의 정상 own OCR도 prior seal binding에서 제외된다. 첫 실행의 prior647 후보가 재개 시346으로 줄어든 현상과 일치한다.
2. 성공 stage도 `_finite_stage`(:2484)가 operation()을 다시 호출한다. 따라서 기존 완료 문서가 OCRResolver를 다시 통과한다. prior binding이 없으면 local publication의 ContentOCRVariantStore._lookup_local로 들어갈 수 있다.
3. content_cache.py :342–374는 기존 content 문서를 generation-only imported provenance/생성시각 등으로 **다시 구성하면서 기존 ocr_variant_id를 그대로 붙인다.** content variant ID는 모든 정규화 manifest field의 hash이므로 원래 manifest metadata를 복원하지 못한 재구성은 Core validator에서 `content OCR variant ID does not match manifest fields`로 실패한다. 원본 body 손상이 아니라 metadata 재구성 계약 오류다.
4. 성공 stage의 operation 오류는 stage retry/error formatter 경로 밖이다. process_document(:5825–5827)는 이 비격리 예외를 새 systemic 보고서 없이 OCRFailureBookkeepingError로 바꾸고, CLI는 generic runtime failure로 출력한다. 따라서 실제 variant 검증 오류가 운영 로그에서 사라졌다. worker-failure.json도 새로 생성되지 않았다.

원래 traceback/document 진단이 보존되지 않았으므로 로그만으로 정확한 원본 stack을 되살렸다고 주장하지 않는다. 다만 **동일 운영 입력을 같은 cache reader로 처리하여 위 ValidationError를 직접 재현**했고 아래 최소 fixture에서도 같은 결함이 독립 재현됐다. 최초 content 후보는 acquired selected 순서 position11(0-based)의 `doc_f0cefe5475d41e852b0d9d3bd73001c983acb13ff05e546658d2156012f57247`, old cache_kind=content, variant `a19cbfdf4a9ba5c63c9fb2bbae07e5b1d52e2c6a4f5a4aa96491d815a5780640`다. 그 이전0–10 lookup은 hit였다. concurrency4에서 현재 시도 완료8건 후 중단된 관찰과도 부합한다.

운영 replay는 state 볼륨 RO/network none, ContentOCRVariantStore._atomic_write를 no-op으로 하여 selection 저장을 비활성화했다. 원격/OCR/provider 실행0이다.

## 최소 재현과 검사

`uv run --no-sync python /tmp/repro-lotte-content-resume.py`: 정상적으로 create/validator 통과한 원래 ContentOCRArtifactManifest를 만든 뒤, 다른 generation에 계승된 seal의 content variant를 local lookup했다. body/PDF identity는 같으며 variant ID를 임의로 위조하지 않았다.

```text
valid_original_manifest=true
lookup_exception=ValidationError
variant_mismatch=true
provider_calls=0 remote_access=false
```

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py apps/cardrag-worker/tests/test_content_cache.py apps/cardrag-worker/tests/test_ocr_recovery.py -q
21 passed, 1 warning in0.65s
 git diff --check
통과
```

기존21건에는 prior binding이 있는 성공 경로가 포함돼 있으나, 이미 완료된 문서에 own OCR이 남은 같은 run resume → no prior → content manifest 재구성 경계는 빠져 있다. 테스트 통과가 해당 운영 경계 통과의 근거는 아니다.

## 판단과 후속

직전 LIVE_REVIEW_07은 파일/체크포인트/일반 resume 테스트는 확인했지만 완료 문서의 no-prior content 재진입 경계를 검증하지 못했다. 안전한 파일 보존과 이 경로의 완주 가능성을 더 분명히 구별했어야 한다. 이번에330건/97chunk 보존은 재확인했다.

이번 실패는 이전 Codex 한도 재초과가 아니라 local cache 재개 경계 결함으로 판단한다. Paddle fallback 등록/정책은 그대로이며 이 metadata/무결성 오류를 Paddle로 우회해 새 OCR을 돌리는 것은 해결책이 아니다. FIX_22 구현·리뷰 후 현재 run을 다시 resume한다. 수정 전 동일 명령만 반복하거나 새 run으로330건을 버리는 방법은 권장하지 않는다. timer/service inactive이며 배치는 재기동하지 않았다. 019와 롯데/신한 최종 인수는 열어 둔다.
