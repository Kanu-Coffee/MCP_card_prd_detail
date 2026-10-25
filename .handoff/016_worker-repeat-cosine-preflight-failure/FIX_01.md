# 016 FIX_01 — Reviewer 수정 요청

- 작성: 2026-10-16 07:45 KST, Reviewer Codex.
- 대상: PLAN/REPORT, 구현 `afaef390bc5e84fbd7f9528c10bffe4b93c8ea74`, 문서 HEAD `3b90664`, 현재 Git·운영 상태·private 증거.
- 시작 시 working tree clean, 기존 FIX 없음. 기존 문서는 보존하고 새 수정 요청만 작성한다.
- 판정: **수정 필요 — P1 1건, P2 2건.** 새 품질 수치 계측과 실제 복구 성공은 인정하며, 아래 보존/진단 경계/근거 과장 문제를 해소해야 최종 인수한다.

## 1. 독립 검증한 성과

- PLAN 지정 embedding/CLI/lock 테스트 **178 passed / 4.32초**, 구현 diff 공백 검사 통과. 전체 2,535개/lint/type는 Executor 보고로 구분하며 이번 리뷰에서 재실행한 것으로 표현하지 않는다.
- 05:38 실패 로그에 minimum **0.9972087002816739**, mean **0.9998836958450696**, threshold **0.999**, failed **1/24**, index **0**, input/metadata SHA가 실제로 남는다. quality 실패 비재시도·기존 threshold/provider pinning을 유지한 변경 확인.
- 운영 Compose는 `016-afaef39`, actual ID `sha256:0f4360aac6a8c14b31c7912ecaa549523e7810152096872307f4dfe132ffc2d2`, OCI revision=구현 full commit. 이전 Worker `015-741cfe0` 이미지도 보존.
- 보존된 성공 컨테이너 inspect JSON: exit **0**, 시작 **10/16 05:40:49 KST**, 종료 **07:32:42 KST**, 실제 이미지 일치. 성공 stdout 원본은 없으며 `recovery-batch-success.log`는 **No such container** 오류 75B 파일이다. 그 파일을 성공 로그로 취급하지 않는다.
- state DB에서 run **9270ff0ac2584c979b8b609137656ff7** succeeded, publish ready, 보존 performance에서 OCR 5,524 성공/실패 0/cache 5,521/provider 문서 3 확인. MCP current **g-9270ff0ac2584c979b8b6091-f2dacc76549c**, `/health/ready` true. backup-status-post-run 증거는 pending/lost 0, 마지막 백업 07:32:36 KST.
- root available **109,536,325,632 B**, timer active, 다음 10/17 03:00 KST. 실제 복구 배치 성공을 부정하지 않는다.
- state DB 조회 시 writer 부재와 WAL 파일 부재를 확인한 뒤 checkpointed DB를 immutable read-only로 조회했다. live WAL DB에 immutable을 사용하지 않았다.

## 2. [P1] 삭제한 ec1255d9 run은 여전히 보존 대상이며 seal이 사라짐

**위치/근거:** REPORT §6/P-3, private cleanup-016.log의 07:35:44 삭제 기록, `state.py:retained_publication_run_ids`, `pipeline.py:_cleanup_local_runs`, retained PDF pruning/로컬 OCR 재사용 경로.

독립 조회한 ready publication 최신 2개는 아래와 같다:

| run | Worker run dir / sealed publish.json |
|---|---|
| `9270ff0ac2584c979b8b609137656ff7` | 존재 / 존재 |
| `ec1255d924ab4c87a8a12491b762fc02` | **없음 / 없음** |

native cleanup은 `retained_publication_run_ids(limit=self.retained_generations)`와 transitive execution-plan source를 보호한다. 기본 retention=2에서 ec1255d9는 “보존 범위 밖”이 아니라 **명시적 보호 대상**이다. REPORT는 현재 current+previous serving generation이 남았으니 Worker run 전체 삭제가 안전하다고 판단했으나, serving generation 사본과 Worker seal/OCR/실행 의존성은 같은 자산이 아니다.

`pipeline.py`의 retained-publication PDF prune 경로는 각 retained run의 `sealed/publish.json` 및 참조 산출물을 요구하고 없으면 `retained publication seal is unavailable`로 거절한다. carry/로컬 OCR 재사용/부분 실행도 Worker run을 조회한다. 즉 당장 MCP ready가 유지돼도 Worker 보존·복구 계약이 이미 깨진 상태다. 다음 성공으로 ec가 retention 밖으로 밀려나는 것을 복구 조치 대신 사용하지 않는다.

**수정 요구:**

1. ec1255d9의 보존 대상 여부와 남은 source-run/backup/cache 참조를 조사하고, 확인된 원본·검증된 사본으로 필요한 seal 및 그 참조 의존성을 안전하게 복원한다. 없는 원본을 빈 seal로 꾸미거나 publish DB 행을 제거해 문제를 숨기지 않는다. 기존 current를 변경하지 않는다.
2. 복원이 불가능하면 정확한 소실 범위와 영향/대체 복구 경로를 명시한다. 보존 정책 변경이 필요하면 Worker/serving/MCP/부분 실행/PDF pin 계약을 함께 검증하는 명시적 변경으로 다루고, “serving만 있으므로 동일”로 간주하지 않는다.
3. 앞으로 공간 회수를 위해 native 보호 대상 run을 수동 삭제하지 않는다. 문서의 정리 절차에 retained IDs·transitive source/pending backup 검사와 삭제 전 evidence archive를 포함한다. 빌드 cache/미참조 이미지/보존 밖 run을 우선한다.
4. 작은 fixture로 보호 대상 current/previous/source run 거절 및 retained seal 검증/정리 경로를 확인한다. 실제 운영의 복원된 참조 closure를 read-only 검증한다. 이를 위해 새 full corpus를 실행할 필요는 없다.

## 3. [P2] quality_details가 secret-safe 출력 경계를 보장하지 못함

**위치:** `embedding_v5.py:_sanitize_quality_details`(문자 shape만 허용), `cli.py:_classify_startup_exception`/`_classified_failure_fields`/`StartupAttemptRecorder`의 그대로 dict 복사.

허용 key는 이름 형태만 검사하며 string도 `[A-Za-z0-9_.-]{1,64}`이면 통과한다. 이는 인증 토큰에도 흔한 형태다. 또한 생성 후 변경 가능한 `error.details`를 CLI에서 재검증하지 않아 raw response/중첩값 등이 journal/JSON/history로 전달될 수 있다. 현재 repeat-gate 생성부가 안전한 수치/hash를 넣는 점은 인정하나, 새 예외 API와 출력 경계 자체의 “secret-safe” 계약은 만족하지 않는다.

**Reviewer 무네트워크 재현:** synthetic sentinel `sk-test-PRIVATE-DIAGNOSTIC-TOKEN` 사용.

1. `EmbeddingV5Error(..., details={'request_id': sentinel})`를 `WorkerStartupFailure`로 감싸 CLI run에 주입 → **constructor_details_token_leaked=true**, exit 1.
2. 정상 details로 생성한 뒤 `error.details['provider_response']='Bearer '+sentinel`을 추가하여 동일 CLI 호출 → **mutated_details_token_leaked=true**, exit 1.

실제 운영 secret은 사용하지 않았다. 기존 신규 테스트는 긴 공백 포함 sentinel을 예외 message에만 넣고 정상 details를 검증하므로 이 경로를 놓친다.

**수정 요구:** 현재 필요한 field별 허용 schema로 제한한다(숫자/count/index, 64hex 입력·metadata hash). generic short string/request ID는 명확한 허용 요구가 없으면 제외한다. 외부 출력 시점에서도 key/type/range/개수/중첩값을 검증하고, 예외 객체 mutation으로 우회하지 못하게 한다. recorder가 직접 호출돼도 동일 경계를 사용한다. Mapping을 전부 `list(details.items())`로 펼치지 말고 실제 순회/출력 크기도 제한한다.

**인수 테스트:** constructor의 토큰 모양 string, 생성 후 details 변조, 임의 key/중첩·NaN·무한·거대 값·큰 mapping을 주입해 stdout/journal/history에 sentinel 미노출 및 유효한 원측정 보존. 기존 threshold/비재시도/deadline/취소 계약 유지.

## 4. [P2] 원인 확정·변동 확률·probe 비용을 증거보다 강하게 서술함

**위치:** REPORT 요약/§2.2/§3/§7/§8, `docs/OPERATIONS.md`의 검증 비용 설명.

- 확인한 사실은 05:38 같은 batch의 대응 sample 한 개가 gate 미달이고 전후 다른 검증은 통과했다는 것이다. 오프라인 fixture 통과로 운영의 모든 로컬 결함/입력·route 차이를 **배제**하거나 **provider 측 원인 확정**까지 할 수 없다. 요청별 profile/입력/벡터 hash 등은 아직 충분하지 않다. “provider 반복 응답 변동 유력, 상세 근본 원인 미확정”으로 분리한다.
- 5회 중 2회 실패는 적응적으로 재실행한 작은 관측 표본이다. **관측 2/5**와 실제/미래 실패 확률 **40%**를 구분하며 이를 다음 재실패 확률 계산 근거로 쓰지 않는다. 자동 retry를 변경하지 않은 선택은 허용된다.
- REPORT의 **“유료 96-call 고정 예산”**은 API 호출 수가 아니다. 24 sample×2회×2provider = **96개 sample embedding**이며 정상 preflight는 metadata GET 1회 + batch embedding POST 4회(내부 HTTP retry 전)다. 실패 위치/HTTP retry에 따라 실제 요청·처리 sample 수가 달라진다. 코드/문서에서 비용·호출·sample을 정확히 구분한다.
- 최종 stdout 유실(P-5)은 명시돼 있으므로 복구 성공을 허위로 판정하지 않는다. 다만 원문에만 있던 통계는 재구성 증거와 연결하거나 미확정으로 남긴다. 삭제 전 로그 저장 및 컨테이너 inspect/exit 기록 순서를 문서화한다.

**수정 요구:** 기존 REPORT를 덮어쓰지 않고 FIX_01_REPORT에서 근거 수준/오기/제약을 정정하고 제품 운영 문서의 비용 설명을 고친다. 단정적 원인 확정을 위해 유료 probe를 추가로 실행할 필요는 없다.

## 5. 실행·재검토 자료

1. 위 3건을 수정하고 `.handoff/016_worker-repeat-cosine-preflight-failure/FIX_01_REPORT.md`를 새로 작성한다. 원본 PLAN/REPORT/FIX 및 기존 private 증거를 보존한다.
2. CLI/embedding의 실제 출력 경계, retention/seal/PDF pin/부분 실행 관련 선택 테스트부터 검증하고 기존 lint/type 검사 수행. 새 코드 변경이 있으면 필요한 이미지에만 반영하고 source/image 검증·운영 반영 여부를 정확히 기록한다.
3. 이미 통과한 배치/유료 startup-check를 이유 없이 반복하지 않는다. 코드 변경의 unit/fixture 검증과 필요한 참조 복구 점검으로 증명 가능한 항목은 그것으로 완료한다.
4. 각 요청의 변경·명령/결과·실제 회수/복원 byte·계획 편차·미해결 사항을 보고한다. restore 또는 정책 정합성 확인이 끝나기 전 “rollback 완전 무결/인수 완료”로 표시하지 않는다.
