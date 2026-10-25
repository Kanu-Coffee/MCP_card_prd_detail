# 019 OPERATIONS_REPORT_08 — FIX_22–23 검증본 반영, quota fallback 준비

2026-10-21 Codex. 사용자는 Reviewer 검증과 함께 작은 quota fallback 수정은 다음 resume에 적용하도록 요청했다. REVIEW_11/FIX_23_REPORT의 검증 소스를 운영에 반영했다. **롯데 실제 resume는 시작하지 않았다.** 기존 run/330완료/97chunks 및 serving을 보존했다.

## 활성 release 및 소스 검증

- `/opt/cardrag/current` → `/opt/cardrag/019-fix23-e9b9cdca1a22`, 이전 fix21 code/image 보존.
- image `cardrag-worker:019-fix23-e9b9cdca1a22`, ID `sha256:15124ebcc9147de54db5ac238bb488f9a31eead2e5cd1b0a89c334f60550972e`.
- source Python80개 SHA `2647e7547717a7136c2d6a5e964ede341d40019b8d87b9b9b40a03b1969e2967`, build input SHA `e9b9cdca1a22c087cc19dc288c794a5c31fcdf0f333716a7ca41e5ff9679dcce`.
- root/worker/core 의존성 선언 및 uv.lock 동일 확인 후 기존 runtime 기반 offline image build. 설치81개 파일(py.typed 포함)과 frozen source를 바이트 비교했고, 종료 시 현재 작업 트리 소스도 snapshot과 일치함을 재검증했다. UID10001 CLI help와 실제 설치 runtime의 quota→local fallback policy smoke 및 host helper shellcheck 통과.
- Worker/Core1808 passed/10 warnings, ruff check/format158 files, diff check는 FIX_23_REPORT/REVIEW_11의 이번 실제 실행 결과다. image policy smoke는 dummy resolver이며 실모델 추론이 아니다.

## 재개 영향 대조

기존 image(fix21)와 새 image(fix23)에 같은 운영 설정을 읽혀 메모리-only state adapter로 실제 `_ocr_resolver`를 구성했다. provider 호출·운영 DB open 없이 계산한 **OCR contract SHA가 둘 다 `3d4152fd74ac796fef9b49b3781783e4228b2ac89d358a7d82ed507823f20f69`**로 일치한다.

primary codex-exec/gpt-6.1-sol, fallback local-paddleocr/PaddleOCR-VL-1.6, cache read-write/epoch0도 전후 동일하다. schema/체크포인트·모델 설정을 바꾸지 않고 whitelist quota 오류의 fallback 분기만 추가했다. 기존 Codex partial chunk는 보존하며 최종 Paddle OCR은 whole document로 검증한다. 인증·설정·credential·계약/기타 systemic 및 타 provider 조합은 차단한다.

state 전후 audit는 Worker가 없고 WAL이 없거나0인 offline 상태에서 immutable read로 수행했다. run row·전체 stage digest·97checkpoint digest 및 파일SHA·330개 완료 OCR body fingerprints·receipt 행 fingerprint·serving pointer/manifest를 모두 비교하여 **전후 동일**함을 확인했다. run49845932e05849d3a0110a2bc82d6c1c는 기존 failed 상태를 유지한다. 이 상태를 succeeded로 수정하지 않았다.

## 최신 독립 복구점과 용량

manual-start guard와 Worker/backup 잠금 아래 최신 두 DB를 SQLite online backup/quick_check/SHA 검증하고 release를 전환했다.

복구점 `/state/deployment-backups/019-fix23-e9b9cdca1a22`:

- worker-state.sqlite3: 7,315,009,536bytes, SHA `74235af5724ac9b8e5389975213fe58fcf0272c0572264cd4152f651a217c899`.
- backup-ledger.sqlite3: 24,416,256bytes, SHA `564df8b3acfbb13b855e4af84daa19a5de0b7d4b6c944d948e96a37dca0ef660`.
- 最新 run=49845932e05849d3a0110a2bc82d6c1c/failed、stable=현대generation。

docs/OPERATIONS의 명시적 사용자 단일 복구본 정책에 맞춰 새 복구본의 독립성과 무결성을 확인한 뒤 구형 fix16/fix19/fix21 snapshot만 정리했다. 최초 guard는 SQLite가 만든0-byte WAL/32KiB SHM을 예측하지 않아 정리를 중단했으며, 그 시점 삭제는0이었다. known-empty sidecar를 확인한 후 manual/worker/backup 잠금을 다시 잡고 전체 후보를 먼저 검증한 뒤 정리했다. 실제 DB·OCR·PDF/model cache·serving·이전 release/image는 삭제하지 않았다.

남은 복구본은 독립 fix23 한 개다. 구형 snapshot allocated22,006,894,592bytes(약20.50GiB)를 회수했고 가용95,163,117,568bytes(약88.63GiB)다. 새 복구본과 이전 code/image로 rollback 가능하며, 정리된 구형 snapshot 경로는 더 이상 유효하지 않다. 활성 deployment metadata와 snapshot-retention.json에 보호 대상/정리 결과를 명시했다. 오래된 문서의 고정 fix16 보호명을 동적으로 참조하도록 보완했다.

## 라이브와 종료 상태

라이브는 총5545건, generation `g-10f236331a2744f78980f2b8-9d16cfdbae66`를 유지한다. 실제 MCP get_source_page2건 성공, MCP current=Worker stable 확인. 카드사 배치·OCR/provider 추론·새 게시·scoped flush는 실행하지 않았다.

운영 증거는 새 operations/deployment.json, backup-verified.json, snapshot-retention.json 및 live/fix23.state.before/after.json, fix23.contract.before/after.json, fix23.mcp-smoke.jsonl에 보관했다. timer/service inactive, Worker 없음. 다음 명시 resume부터 FIX_22의 identity/진단 보완과 FIX_23의 좁은 Codex quota→로컬 Paddle 예외가 적용된다. Paddle 실제 CPU 모델 로딩/추론 완주·현재 롯데 배치/백업 완주·신한/019 인수는 아직 미확인이다.
