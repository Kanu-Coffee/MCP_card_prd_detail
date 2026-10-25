# 019 OPERATIONS_REPORT_05 — FIX_19 운영 배포 및 현대·BC 복구 검증

2026-10-21 Codex. 기존 PLAN/변경계획/FIX_01–19/REPORT/REVIEW_08과 Git HEAD aaeb2c4 위 미커밋 작업을 보존했다. 구현 리뷰 통과한 FIX_19를 배포했고, 실제 운영에서 새로 확인한 index 읽기 문제는 FIX_20으로 발행했다. 019와 전체 8사 인수는 미완료다.

## 배포 및 복구점

- 활성 `/opt/cardrag/current` → `/opt/cardrag/019-fix19-5d0b40a24a6f`; 이전 fix16 release 보존.
- image `cardrag-worker:019-fix19-5d0b40a24a6f`, ID `sha256:9f58fb01c19db58f9e54851cfdc216d71ab3754e4f1b5b81c7b9c83afda1cb51`.
- source tree `ab7b3d167b637438bbc90516aa9efa24372a670709e634621f30ef5b65939d67`; Python 80개와 py.typed 포함 81개 설치 바이트가 frozen source와 일치. 의존성 선언/uv.lock 동일한 기존 runtime 기반 offline build. UID10001 CLI help 및 monitor 정상. 관련 host helper shellcheck 통과.
- timer/service 비활성, 실행 중 Worker 없음 확인 후 worker.lock/backup.lock을 함께 유지한 SQLite online backup/quick_check/SHA 검증 뒤 전환. snapshot `/state/deployment-backups/019-fix19-5d0b40a24a6f`.
- worker DB 7,313,784,832 bytes / SHA f1e72343cfe017fb74f1438ed5d870b31dba7ef8d93135f31f291b295ebf926b. ledger 24,416,256 bytes / SHA 138e362fe721dc58c7563cf6dcce48b84b25f1e006f9de124fce90b22430533c. deployment.json에 증거 기록. lock은 전환 후 해제했다.

## 실제 복구 및 정식 flush

`worker-compose.sh run --rm --no-deps worker backup recover-metadata --run 8810e405d952441faf9e560ac19fd7b0 --issuer bc --key bba8ef70412a10d5589d560964ba75f8b256fea551dfe89eb8641d2050303dd2 --key e7ad55525a3f18d0a9e7a2e7980755a771b6e8952ec76d11d61a2e3587536faa`

status=succeeded, verified_count=2, unbound 4→0. 원격에서 검증한 manifest/READY 두 쌍의 로컬 cache만 복원했다. 반복은 already_bound 2개, failed=0. OCR/provider/카드사 본배치는 재실행하지 않았다.

현대 `10f236331a2744f78980f2b878475b64`, BC `8810e405d952441faf9e560ac19fd7b0`에 대해 각각 정식 scoped flush와 반복 flush를 실행했다. 두 번 모두 pending/conflicts/missing/unbound/lost_source=0, uploaded_count/bytes=0, status=unchanged이나 **backup_scope_complete=null**이다. 정식 CLI 완료를 주장하지 않는다.

원인은 index 8,632,661 bytes > 기본 8MiB GET 제한이다. 64MiB로 제한한 독립 읽기 검증에서는 현대 required=bound=matching_receipts=1894, BC=482, 두 scope index mismatch=0이었다. 운영 원장/receipt/seal/완료 필드를 수정하거나 원격 index를 재게시하지 않았다. 이 read-only verifier를 operations/verify-fix19-readonly-completion.py에 보관했다. 정식 CLI 원인과 merge 시 오류 보호는 FIX_20에 인계한다.

복구/flush 전후 receipt 전체 16,888개의 fingerprint `6d220c011043ec6d194151521f69813f8130663b4835552eb2e4579ae5ce0ada` 동일, pending 테이블 비어 있음. 증거는 operations/live/fix19.*.json과 RUN_ID.backup-check.json. summary-issuers.sh는 matching RUN_ID/issuer의 마지막 backup-check가 있을 때 last_check로 표시하도록 보완했으며 과거 batch snapshot은 보존했다. 따라서 현대/BC가 unchanged/None으로 표시되는 것은 현재 CLI의 정확한 결과다.

## 라이브 MCP·용량·다음 단계

Worker는 cardrag-serving RW, MCP는 같은 cardrag-serving RO와 cardrag-mcp-state의 실제 live copy를 사용한다. test-live-mcp.sh의 실제 JSON-RPC get_source_page 2건 성공: Purple osée 3605 chars/2 pages, GENESIS 선택서비스 4698 chars/3 pages. MCP current와 Worker stable 모두 `g-10f236331a2744f78980f2b8-9d16cfdbae66`, 총 문서 5545 유지. 개발용 serving 볼륨으로 전환하지 않았다.

최종 state 가용 87,067,877,376 bytes(약81.1GiB), 최근 gate+4GiB buffer 필요 78,295,838,296 bytes(약72.9GiB), 여유 약8.2GiB로 시작 준비 gate 통과. 이후 실제 배치 시 다시 검사한다. 자동 timer/service는 계속 inactive이며 롯데/신한 배치는 시작하지 않았다. 운영 proxy 설정을 보존했고 이전 live 연결 검증과 구분하여 이번에는 전체 배치 성공을 주장하지 않는다.

현대/BC 본배치 재실행은 필요하지 않다. 다음은 FIX_20의 정식 index 판정/merge 보호 보완 후 롯데 → 신한 순차 수동 배치다. 기존 check-issuer.sh --flush는 true를 요구하므로 현재 null에서는 정확히 실패한다. 이 검증을 우회하여 전체 성공으로 선언하지 않는다.
