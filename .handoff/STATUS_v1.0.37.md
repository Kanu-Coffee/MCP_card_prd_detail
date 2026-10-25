# Handoff 종료 인덱스 — v1.0.37

2026-10-25 사용자 운영테스트 정상 판정 및 모든 열린 plan 종료 지시에 따라 **001–019 전체 CLOSED, 활성 계획 없음**으로 확정한다. 이 문서가 현재 상태의 기준이다. 2026-10-21 README.md와 과거 PLAN/REPORT/FIX/REVIEW의 ACTIVE·미완료 표현은 당시의 역사 기록으로 보존하며 현재 재개 지시가 아니다.

| 과제 | 현재 상태 | 종료 근거 |
|---|---|---|
| [001_v1029-pdf-lineage-ocr-cache-recovery](001_v1029-pdf-lineage-ocr-cache-recovery/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [002_production-recovery-gc-release-disk](002_production-recovery-gc-release-disk/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [003_daily-batch-retirement-rolling-baseline](003_daily-batch-retirement-rolling-baseline/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [004_worker-cutover-integrity-closeout](004_worker-cutover-integrity-closeout/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [005_public-release-evidence-sealing](005_public-release-evidence-sealing/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [006_opencode-ocr-provider](006_opencode-ocr-provider/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [007_content-addressed-ocr-cache](007_content-addressed-ocr-cache/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [008_parallel-issuer-collection](008_parallel-issuer-collection/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [009_product-summary-field-classification](009_product-summary-field-classification/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [010_009-production-cutover](010_009-production-cutover/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [011_cross-issuer-summary-semantics](011_cross-issuer-summary-semantics/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [012_v1034-release-cutover](012_v1034-release-cutover/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [013_local-serving-incremental-webdav-backup](013_local-serving-incremental-webdav-backup/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [014_worker-startup-failure-codex-validation](014_worker-startup-failure-codex-validation/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [015_worker-startup-retry-disk-capacity](015_worker-startup-retry-disk-capacity/PLAN.md) | CLOSED | 과거 인수·종료 및 후속 과제 이관 기록, 사용자 일괄 마감 |
| [016_worker-repeat-cosine-preflight-failure](016_worker-repeat-cosine-preflight-failure/PLAN.md) | CLOSED | [016 평가 종료](016_worker-repeat-cosine-preflight-failure/CLOSEOUT_v1.0.37.md) |
| [017_issuer-incremental-acquisition-review](017_issuer-incremental-acquisition-review/PLAN.md) | CLOSED | [017 종료](017_issuer-incremental-acquisition-review/CLOSURE.md) |
| [018_worker-manual-live-validation](018_worker-manual-live-validation/PLAN.md) | CLOSED | [018 종료](018_worker-manual-live-validation/CLOSURE.md) |
| [019_selected-issuer-unresolved-history-preservation](019_selected-issuer-unresolved-history-preservation/PLAN.md) | CLOSED | [019 종료](019_selected-issuer-unresolved-history-preservation/CLOSURE.md) |

## 인수와 운영 전환

수동8사 검증과 후속 자동 순회/운영/MCP/범위별 원격백업 검증을 사용자 승인으로 인수한다. 자동8일 전체 관측을 완료했다고 주장하지 않는다. 테스트는 종료하며 기존 매일14:30 KST 예약은 실제 운영으로 유지한다. 재실행·timer 해제·cursor 초기화를 하지 않는다.

우리카드 외부 서비스 오류는 정상 bounded retry 종료로 처리한다. 기존 범위밖 backup pending3건30862bytes는 백업 최소화 정책에 따른 대기로 인수한다. embedding warning band와 전체 export 성능 개선 여지는 운영 관찰사항이며 열린 plan이나 미완료 릴리스 blocker로 남기지 않는다. 필요하면 새 사용자 요청으로 후속 과제를 만든다.

v1.0.37은 GitHub 소스 릴리스다. 공개 이미지 workflow를 변경·우회하거나 운영 이미지/볼륨/서빙 포인터를 교체하지 않는다. 커밋·PR·CI·머지·tag·릴리스 실제 식별자는 후속 RELEASE_REPORT_v1.0.37.md에 기록한다.
