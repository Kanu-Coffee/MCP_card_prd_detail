# 019 OPERATIONS_REPORT_06 — FIX_20–21 운영 반영 및 현대·BC 완료 확인

2026-10-21 Codex. 사용자 요청 "배포하고 완료확인까지 진행해"에 따라 REVIEW_10에서 통과한 FIX_20–21을 운영에 반영하고 실제 retained run 두 건을 정식 CLI로 검증했다. **현대·BC 모두 backup_scope_complete=true이며 재시도도 true/업로드0이다.** 기존 계획/수정/리뷰/운영 기록과 HEAD aaeb2c4 위 기존 미커밋 변경을 보존했다. 롯데·신한 카드사 배치와 timer는 기동하지 않았다. 019/전체8사 인수는 계속 열린 상태다.

## 배포 증거

- `/opt/cardrag/current` → `/opt/cardrag/019-fix21-afdcabf41ea2`.
- image `cardrag-worker:019-fix21-afdcabf41ea2`, ID `sha256:f1df6c1c9e8a95634c54bb40b66f138a55445333a265ed6c2b4385226e3bf7e5`.
- 직전 FIX_19 release/image는 rollback 대상으로 보존했다. 의존성 선언(root/worker/core pyproject)·uv.lock 동일을 확인한 기존 runtime 기반 offline build다.
- source Python 80개 digest `da0fd32bd259a437fee45ad6a65b7d6364242037c36497dae30fb3dd1b56d48d`; py.typed 포함 설치 소스81개가 frozen snapshot과 바이트 일치. 종료 시 작업 트리 소스도 frozen snapshot과 다시 비교하여 일치함을 확인했다.
- 실제 UID10001 CLI help/monitor 및 관련 host helper shellcheck 통과, compose의 실제 image 식별 일치. 본 작업은 구현 변경이 없으며 REVIEW_10의142개 관련 검사/Executor의1791개 전체 결과를 빌드 후 다시 반복 실행하지 않았다.

## 복구점과 전환

시작 전 timer/service inactive·Worker 컨테이너 없음·이전 pointer/image 일치를 확인했다. maintenance holder에서 worker.lock와 backup.lock을 함께 보유한 채 SQLite online backup → quick_check → SHA 검증을 수행하고 같은 잠금 아래 release pointer를 전환했다. 종료 후 holder를 제거하여 잠금을 해제했다.

복구점 `/state/deployment-backups/019-fix21-afdcabf41ea2`:

- worker-state.sqlite3: 7,313,784,832 bytes, SHA `f1e72343cfe017fb74f1438ed5d870b31dba7ef8d93135f31f291b295ebf926b`.
- backup-ledger.sqlite3: 24,416,256 bytes, SHA `818ccb258a5caf1351270795f6ee671b5fe8f49fcadaba621f5635c2689d1006`.

전체 증거는 새 operations/deployment.json, backup-verified.json에 기록했다. immutable seal/원장 receipt/백업 완료 필드를 수작업 수정하지 않았다.

## 실제 정식 CLI 완료

```bash
OPS=/opt/cardrag/current/operations
"$OPS/check-issuer.sh" hyundai --flush
"$OPS/check-issuer.sh" bc --flush
```

두 helper 모두 exit0이며 성공 run/컨테이너·비선택 문서 변경0 검사 및 정식 status/flush를 수행했다.

| 카드사 | retained run | required/bound/receipt | scope_complete | index 오류 | 업로드 |
|---|---|---|---|---|---|
| 현대 | 10f236331a2744f78980f2b878475b64 | 1894/1894/1894 | true | null | 0건/0bytes |
| BC | 8810e405d952441faf9e560ac19fd7b0 | 482/482/482 | true | null | 0건/0bytes |

status=unchanged는 이미 검증된 artifact를 재업로드하지 않았다는 의미이며, 이번에는 **정식 CLI가 remote committed index까지 검증하여 true를 반환**했다. remaining/pending/missing/conflicts/unbound/lost_source는 모두0이다. 현대의 excluded_shared_cache24, BC2는 기존 shared-cache 제외 정책에 따른 값이다.

각 run에 `worker-compose.sh run --rm --no-deps worker backup flush --run RUN_ID --issuer CODE`를 다시 실행했다. 두 번째도 complete=true, uploaded_count/bytes=0, error/index_read_error=null로 멱등을 확인했다. 카드사 수집·OCR/provider·본배치는 재실행하지 않았다.

## 원격·receipt·serving 불변과 라이브 MCP

배포 전후 `WebDAVClient.get_bytes(index,max_bytes=64MiB)`로 읽기 검증했다. index는 동일한 **8,632,661 bytes / 16,888 items**, SHA `bff1e91f61571ed95cc194e5a3b5b0f993905ab6d1492bfb0aad5610a5056d64`다. current-root receipt16888개 전체의 SHA/size와 index mismatch0, 전후 index 바이트 해시 동일이다. receipt 전체 행 fingerprint도 `6d220c011043ec6d194151521f69813f8130663b4835552eb2e4579ae5ce0ada`로 전후 동일이며 pending 테이블은 비어 있다.

Worker monitor baseline 전체 JSON이 전후 일치한다. generation `g-10f236331a2744f78980f2b8-9d16cfdbae66`, 총5545건: 하나735/삼성766/롯데647/우리980/신한931/현대607/국민757/BC122. MCP는 실제 cardrag-serving RO와 cardrag-mcp-state를 사용하며 개발용 볼륨으로 전환하지 않았다. `test-live-mcp.sh` 실제 JSON-RPC get_source_page 2건(Purple osée3605chars/2pages, GENESIS선택서비스4698chars/3pages)이 성공했고 MCP current=Worker stable을 확인했다.

`summary-issuers.sh`도 현대·BC를 pending0/scope_complete=True/backup_source=last_check로 표시한다. 우리/삼성 등의 deferred/None은 기존 run_snapshot이며 이번에 해당 오래된 run까지 새 검증했다고 주장하지 않는다. 신한/롯데는 NOT_FINISHED를 유지한다.

## 결과 파일과 다음 단계

새 operations/live에 다음 증거를 보관했다:

- fix21.acceptance.json 및 fix21.remote.before/after.json, fix21.receipts.before/after.json.
- RUN_ID.backup-check.json, fix21.hyundai-check.log, fix21.bc-check.log, fix21.*-flush-repeat.json.
- fix21.independent-completion.json, fix21.serving.before/after.json, fix21.mcp-smoke.jsonl.

복구본 생성 후 최종 가용79,567,605,760bytes(약74.1GiB), 최근 capacity gate+4GiB buffer 필요78,295,838,296bytes(약72.9GiB), 여유1,271,767,464bytes(약1.18GiB)로 현재 시작 준비 검사를 통과했다. 다음 실제 배치도 run-issuer.sh가 용량을 다시 점검한다. 이번에 임의 백업 삭제/용량 정리는 하지 않았다.

최종 timer/service inactive, 실행 중 Worker 없음. 현대·BC 본배치 재실행 없이 다음은 기존 helper로 **롯데→신한 순차 수동 배치**다. 각각 check-issuer.sh CODE --flush에서 성공·비선택 변경0·scope_complete=true를 확인한 후 다음 카드사로 진행한다. 전체8사 검증 전에는 timer 재개나019종료를 선언하지 않는다.
