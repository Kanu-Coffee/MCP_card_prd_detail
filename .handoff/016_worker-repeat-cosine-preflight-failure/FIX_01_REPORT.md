# 016 FIX_01 REPORT — P1 1건·P2 2건 반영

- 작성: 2026-10-16, Executor (OpenCode). 대상: `FIX_01.md`(Reviewer Codex, 07:45 KST).
- 판정 요약: **3건 모두 처리.** ① ec1255d9 보존 run의 seal과 참조 closure를 검증된 원본(serving generation 파일·manifest·015 증거 stdout payload)으로 복원하고 실제 consumer 검증 함수(`_validate_local_seal`)로 통과 확인(objects 배열 순서만 원본과 byte 동일성 불증명 — 아래 한정 공개). ② quality_details를 reason별 필드 schema 경계로 재설계 — 생성부·CLI 분류·history recorder 모든 출력 지점에서 재검증, 사후 변조/request-ID성 문자열/거대 mapping 요구 불순통과 테스트 통과. ③ 원인 확정·변동 확률·probe 비용 서술을 증거 수준으로 정정(아래 §4)하고 운영 문서의 호출/sample 구분·retention 확인 절차·로그 선저장 순서를 고정.

## 1. [P1] ec1255d9 run — 조사 결과와 복원

### 1.1 보존·참조 상태 조사(요구 1)

- native retention 확인: `state.retained_publication_run_ids(limit=2)` = **(`9270ff0a…`, `ec1255d9…`)** — reviewer 판단대로 ec1255d9는 보존 범위 **밖이 아니라 안**이었다. REPORT P-3의 "3rd로 밀려남" 전제는 finalize_pdf_activity 실행 시점의 retained list를 과대 해석한 것이었다(§4.1 정정).
- 삭제 시점 이후 실제 참조 consumer를 코드 전수 확인: `finalize_pdf_activity` retained-seal hard 검사(3604+)는 publication 이후 실행(finalize 결과 래퍼)이나 오늘 상태로는 **retained 2개 모두 seal 부재 시 prune만 failed로 soft 기록**(general `except Exception` 경로를 fixture `test_missing_or_unsafe_retained_seal_fails_prune_without_deleting_cas`가 이미 보증)했으나, retained인 동안 `_validate_local_seal`이 요구하는 seal·sealed 파일·objects 파일들이 실제로 존재해야 하므로 **복원이 정당**하다. resume-publication·no-change replay·cache healing(4638 hard)·repair candidate(4743, `limit=max(2,5)` → ec1255d9 포함 스캔) 모두 실패 지점. carry(3263)/baseline(4186)/retirement prior(4522/4590)는 soft skip이었음을 확인.
- 백업: ledger read-only 재확인 — pending 0/lost 0, ec1255d9 publication분까지 10/15 09:38 flush 커버(원인 없음 추가).

### 1.2 복원 내용(검증된 원본·사본 사용)

`runs/ec1255d9…/`에 복원(모두 산출물 무결성 검증 후 확정, 기존 current·serving·MCP 상태 무변경):

| 대상 | 원천 | 검증 |
|---|---|---|
| `sealed/index.sqlite3` (4,912,087,040 B) | serving `g-ec1255d9…/index.sqlite3`(게시 검증된publication 사본) | sha256 = manifest.serving_database.sha256 = `7bebfc63…` 일치 |
| `sealed/vectors.f32` (10,100,326,400 B) | 동 generation `vectors.f32` | sha256 = manifest vector_sidecar = `cd85bf75…` 일치 |
| `documents/<doc_id>/ocr/ocr.md` × 5,521 | serving `v1/objects`(manifest ocr 참조) | 복사 후 파일 sha256 == manifest ocr.sha256 전수 확인(mismatch 시 즉시 중단 설계) |
| `reports/` 4종 | 016 evidence `ec1255d9-reports.tar`(삭제 전 아카이브) | tar 원본 그대로 |
| `sealed/publish.json` | serving `manifest.json`(canonical 검증) + DB publish row + 015 stdout payload(`ec1255d9-run-json-payload.json`) | canonical 직렬화, run/generation/corpus/contract identity, db/vector hash·size·path |

- `v5_metrics`·`ocr_cache_publication_deferred`의 근거 없는 값 지어내기는 배제: 값은 015에 원문 보존된 ec1255d9 stdout payload의 필드(37개, gate 통과 배치 결과)를 그대로 사용(`ec1255d9-run-json-payload.json`, 검증: payload run_id/generation_id 일치 + vector_sidecar_size_bytes = sidecar 실측).
- **복원 검증(요구 4)**: 운영 이미지에서 실제 consumer 함수 `WorkerPipeline._validate_local_seal`을 두 sealed run(9270ff0a control, ec1255d9 복원분)에 대해 read-only 호출 — **모두 통과**(objects 11,048 / 11,042, deferred 0, harness 출력 `deployment-016-bb56d20/retained-seal-validation-harness.txt`). full corpus 실행 없음. objects 각 항목 파일 실재 + pdf-cache root/approved-root 검사 포함.
- **한정 공개(disclosed)**: 원본 publish.json의 `objects` **배열 순서**는 어디에도 external pin이 없어(DB publish는 `manifest_sha256`만 저장) 복원본과 byte 동일성을 증명 불가. content(전체 CAS 항목 set·media·size·path)는 전수 검증했고, consumer는 set/dict 소비(ordering 비의존)임을 코드 확인으로 근거 제시. canonical seal hash 값(`c533bcee…`)은 복원본의 것이며 원본 해시를 대체하는 증거로 쓰지 않는다.
- **잔여 소실 범위**: 원본 publish.json bytes와 run-local intermediate(pdf ingest temp, checkpoint 등)는 원천 소실 — 위 closure 밖의 파일은 어떤 consumer 경로도 참조하지 않음을 §1.1 조사로 확인.

### 1.3 절차 고정(요구 3) — docs/OPERATIONS.md

정리 절차에 다음을 명문화: run 디렉터리 정리 전 `publish(status='ready')` retained 목록·`execution-plan.json` transitive `source_run_id`·백업 pending read-only 확인, native 보호 대상(current/previous publication run·최신 incomplete keep) 수동 삭제 금지, 회수 우선순위 = 미참조 빌드 캐시 → 미참조 이미지 → 보존 밖 run, 명명 컨테이너는 `docker logs`+`inspect` evidence 저장 후 삭제.

### 1.4 회귀 fixture(요구 4)

신규 `test_cleanup_local_runs_preserves_retained_current_previous_and_source_runs`: retained current/previous·실행 중 run·transitive source·incomplete keep 내 run 보존, 보존 밖 completed/초과 incomplete 삭제. 기존 `test_source_retention_and_baseline_preserved`·retained-seal prune fixture와 함께 통과.

## 2. [P2] quality_details 출력 경계 재설계

- `embedding_v5.py`: `QUALITY_DETAIL_SCHEMAS`(reason→필드→타입 cosine|threshold|count|hex64) 단일 선언부로 수렴. generic identifier/request-ID/None/미선언 key는 **모든 지점에서 차단**. `validated_quality_details(reason_code, details)`가 **schema key만 lookups**(huge mapping 비전개, hostile `items()` 호출 차단 fixture 검증)하고 타입·range(cosine∈[-1,1], threshold∈[0,1], count∈[0,2³²-1])·hex64만 통과.
- 생성부(`EmbeddingV5Error.__init__`)는 위 함수 호출, **출력 지점도 동일 함수를 재호출**: `_classify_startup_exception`(dict 복사 대신 재투영)→ stage 로그(`_classified_failure_fields`)→ CLI JSON(`quality_details`)→ `StartupAttemptRecorder.record`(reason과 함께 재투영). 객체 변조는 경계에서 무효.
- 인수 테스트(reviewer 무네트워크 시나리오 포함): constructor token형 string(`request_id: sk-test-PRIVATE-DIAGNOSTIC-TOKEN`)·생성 후 `provider_response` 변조·nested·NaN/inf·2^70·100 keys mapping 주입 → 예외 stdout·caplog journal·history JSONL 어디에도 sentinel 미노출, 원측정(gate_threshold/minimum/…/first_failed_index 5/17)은 정확 보존, `ExplodingMapping(items()=raise)`로 비전개 증명. threshold 0.999·결정적 비재시도·deadline·취소 계약은 기존 테스트 그대로 통과.
- 참고: capacity_details(V5CapacityError)는 본 요청 범위 밖(선언적 int/None sanitizer 유지).

## 3. 운영 반영·검증 (D)

- 커밋 `bb56d20 fix(worker): schema-bound quality details at every output edge; seal-retention and cost procedure docs`(6 files). push 없음.
- 테스트: 대상 4파일 -k 선별 21 + embedding/cli 177 + **전체 2,536 passed**, `ruff check`/`format --check` clean, `mypy` strict 108 files clean.
- 빌드 1회: `cardrag-worker:016-bb56d20` (ID `f4e97de4…`, OCI revision=HEAD full SHA), 이미지 설치 소스 5종 SHA = repo 파일 SHA 전수 일치(`HASH_MATCH`). rollback: `016-afaef39`(직전)·`015-741cfe0` 보존. compose.secrets image 1줄 변경, `config --quiet` 통과.
- production startup-check **1회(최종 이미지, 08:21)**: exit 0, `attempts=1/2`, deepinfra minimum_repeat=1.000000, cross 0.995595, tokenizer 동일 sha — gate 무변경·live 통과 확인. 컨테이너는 **logs/inspect 저장 후 삭제**(절차 준수, worker evidence 컨테이너 0 → orphan warning 소멸).
- 유료 probe 지출 정정(§4.3 참조): 016 라운드累计 embedding batch POST 18회(metadata GET 5회), sample 432개. 8시 check 포함.
- 최종 상태: timer active(다음 10/17 03:00), MCP current `g-9270ff0a…` 유지, `/health/ready` true, free **84,683,747,328 B**(복원 +15GB 후 빌드 -10GB 반영; 게이트 예상 required 73.7GB + 증가분 약 2GB 대비 여유 ≈ 9GB), backup pending 0, 서빙 generations 2(retention), MCP state 3세대 유지(R-2 follow-up).

## 4. [P2→정정] REPORT 원인·확률·비용 서술의 근거 수준 (§4 요구)

REPORT를 덮어쓰지 않고 여기서 정정한다.

1. **원인 확정 과장 정정**: 확정 사실은 두 관측뿐이다 — (a) 05:38 batch에서 대응 sample **index 0 하나**의 repeat cosine이 0.9972087로 gate 미달, (b) 동일 이미지의 05:36과 05:39 검사에서 최소 1.0. 오프라인 fixture 통과로 gate·rebinding·경계 로직의 **로컬 회귀는 배제**하지만, 운영 경로 전체(응답→normalize→cosine 입력까지의 실제 페이로드 대조)와 provider 내부 backend 교체 여부를 구분할 per-request vector hash 증거는 아직 없다. 최종 분류: **"DeepInfra 반복 응답 변동 유력; 상세 근본 원인(backend 경합·route 교체·응답 처리 차이) 미확정"**. (REPORT §2.2 'provider 측 … 확정'으로 읽힐 수 있는 문장은 본 문단이 우선한다.)
2. **변동 확률 정정**: 2/5는 **적응적으로 재시도한 소표본 관측치**이며 40%는 확률 추정치가 아니다. 미래 실패 확률·정책 변경 근거로 쓰지 않는다. 자동 재검증을 구현하지 않은 결정은 유지하되, 그 근거도 "확률"이 아니라 "재시도 1회로 통과본만 선택되는 편향 위험 + external 표본 부족"(PLAN C.2)으로 재정의한다.
3. **probe 비용 정정**: '96-call'이 아니라 **96 sample embedding/1 startup-check**(metadata GET 1 + provider·회차당 batch POST, 정상 통과 시 POST 4). 내부 retry 발생 시 HTTP 요청 수가 늘어난다. 코드 문서는 이미 OPERATIONS.md/§8로 정정 반영; REPORT §3/§7의 표현은 본 정정이 대체한다.
4. **P-5(성공 run stdout 유실) 후속**: 015 라운드에서 보존된 ec1255d9 payload와 달리 **9270ff0a의 stdout에만 존재했던 통계**(v5_metrics 등)는 복원 불가 — 다만 run DB·performance.json·serving manifest이 동등 수치를 보유하며, seal은 그 어느 곳에서도 원본 hash를 요구하지 않아functional 손실 없음(§1.2 검증). 삭제 전 `docker logs→rm` 순서 고정을 OPERATIONS에 명문화(§1.3)했고, 라운드 이후 컨테이너는 해당 순서로 처리했다.
5. REPORT §2.2의 "run 미생성·fail-fast 20초" 사실, §5 수치, §6 공간 수치는 그대로 유효하다.

## 5. 편차·미해결

- 편차 없음(FIX_01 요구 1–4·§5 지시 범위에서 수행). 복원된 publish.json은 원본과 byte 동일성 증명 불가 항목을 §1.2에 한정 공개.
- 미해결(승인 대기 아님, 관측 유지): ① 동일 sample index 0의 sha `0c692a4b…` 재이력 축적(provider 제보 팩) — 코드 변경 없이 history/log로 누적; ② MCP state 3세대 GC(R-2, 016 §7延续); ③ shinhan 12연속/last_ok 10-05(016 §5) 어댑터 fixture 재현(R-3); ④ run evidence footprint 대 gate 구조(R-4)는 retention 정책 변경으로 별도 검토.
- 인수 상태: **restore·경계 구현·절차 문서화 완료. “운영 복구 완료”는 10/16 배치 성공(run `9270ff0a…`)까지 유효하며, 10/17 예약 결과는 초기화 통과/전체 완료/degraded 수집으로 구분 관찰 필요.** Reviewer 재검토(FIX_02) 대기.

## 6. 재검토 자료

- 코드 diff: `git show bb56d20`(embedding_v5/cli/tests/docs), 커밋 순서: `afaef39`(016 구현) → `3b90664`(015 docs 보존+016 REPORT) → `bb56d20`.
- 운영 evidence: `/opt/cardrag/016-evidence/operations/deployment-016-bb56d20/`(deployment.json, image/repo hashes, startup-check.log[삭제 전 저장], retained-seal-validation-harness.txt), `evidence-20261016/ec1255d9-run-json-payload.json`, `ec1255d9-rebuilt-seal-canonical-sha.txt`, `run-db-state-9270.txt`.
- 검증 재현: `uv run --no-sync pytest -q`(2,536 passed) · `uv run --all-packages ruff check …` · `uv run --no-sync mypy <3 src>` · harness는 §3의 read-only docker run.
