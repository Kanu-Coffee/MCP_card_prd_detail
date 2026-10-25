# 019 FIX_20 — 커진 backup index의 bounded read와 실패 시 보존

2026-10-21 Reviewer: Codex. FIX_19/REPORT, REVIEW_08, 이전 handoff와 현재 Git 상태, 배포 이미지의 실제 CLI·원격 GET 증거를 검토했다. 국소 백업 보완이므로 019를 유지한다. 다음 Executor는 이 미보고 FIX를 구현하고 FIX_20_REPORT.md를 신규 작성한다. 기존 문서는 보존한다.

## 실제 운영 재현

FIX_19를 `/opt/cardrag/019-fix19-5d0b40a24a6f`에 반영하고 BC의 정확한 두 metadata pair를 GET 검증·복구했다. 현대 required/bound=1894/1894, BC=482/482, remaining/conflicts/missing/unbound/lost_source=0이다. 그러나 두 정식 scoped flush는 `status=unchanged`, `backup_scope_complete=null`을 반환한다. 반복 결과도 동일하다.

실제 `v1/backup/index.json`은 **8,632,661 bytes / 16,888 items**다. WebDAVClient.get_bytes의 기본 CONTROL_OBJECT_MAX_BYTES는 **8,388,608 bytes**이므로 기본 GET은 WebDAVIntegrityError다. 64MiB 한도를 명시한 읽기 전용 GET은 정상이다. SHA256=bff1e91f61571ed95cc194e5a3b5b0f993905ab6d1492bfb0aad5610a5056d64. 이 GET으로 현재 seal 기반 required/expected와 current-root receipt 및 index digest를 대조하면 두 scope 모두 일치한다. 이 별도 검증을 정식 CLI의 true 반환으로 바꿔 기록하지 않았다.

증거: 활성 operations/live의 fix19.independent-completion.json, 두 RUN_ID.backup-check.json, fix19.*-flush-repeat.json. receipt 16,888개 전체 fingerprint는 복구/flush 전후 동일하다.

## 수정 요구

1. [P2] backup index 전용 읽기 정책을 마련하여 성장한 정상 index를 명시적 유한 한도로 읽는다. 공통 control object의 8MiB 기본 한도를 무작정 올리거나 무제한 읽기로 바꾸지 않는다. 크기 초과·invalid JSON·transport 오류를 구분할 수 있는 진단을 제공하고, scope 미검증을 완료로 바꾸지 않는다.
2. [P1] backup.py의 index 읽기 경로를 함께 검토한다: `_remote_index_entries`, `_remote_index_repair_needed`, `_publish_index_and_pointer`, receipt 없는 restore discovery. 특히 commit merge가 기존 index 읽기 오류를 suppress한 뒤 빈 index로 간주하여 local receipts만 재게시하지 않게 한다. 원격에만 있는 정상 항목·다른 세대 항목을 보존한다. 실제로 없는 새 index와 크기 제한·인증·연결·파싱 오류를 구별하며, 불확실하면 기존 index를 덮어쓰지 않는다. 현재 운영에서 원격 항목 유실이 발생했다고 주장하지 않는다(이번 flush 원격 쓰기 0).
3. fixture에는 기본 8MiB 초과 정상 index, 새 상한 초과, 손상/접근 실패, remote-only 항목 보존, no-pending completion/repair, restore discovery를 포함한다. 실제 WebDAVClient의 max_bytes 전달 경로를 검사한다. get-only/fake client 계약도 유지한다.
4. 관련 테스트와 Worker/Core 회귀·ruff를 수행하고, 원격 객체/receipt/seal을 조작하지 않는다. 검토 통과 후 운영 CLI에서 현대/BC scope_complete=true와 재시도 uploads=0을 확인한다. 실제 원격 증거 없이 완료 필드를 수동으로 true로 수정하지 않는다.

## 운영 경계

FIX_19 배포·BC metadata 복구·독립 원격 검증은 완료됐다. 신규 카드사 실행 및 timer 재개는 이번 작업에서 하지 않았다. 정식 완료 판정과 index merge 안전성 보완이 남아 있으므로 다음 배치 전 이 FIX 검토·반영을 권장한다. 019/8사 인수는 닫지 않는다. 최신 실제 serving은 5545건, 현대 generation이며 라이브 MCP OCR 2건 호출도 정상이다.
