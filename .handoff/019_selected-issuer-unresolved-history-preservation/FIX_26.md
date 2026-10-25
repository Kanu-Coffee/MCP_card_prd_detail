# 019 FIX_26 — 과거 이미지·복구본·개발 잔여물의 증거 기반 공간 정리

2026-10-22 Codex, Reviewer/Planner. 사용자가 과거 이미지/복구본/개발 잔여물 정리 FIX 발행을 요청했다. 본 문서는 정리 실행 전 인계이며 이번 예약시각 변경과 별개다. 기존 handoff는 보존한다.

## 목표와 범위

실제 불필요한 CardRAG 산출물을 식별·정리해 공간을 확보하고 삭제 전후 회수량을 보고한다. 최신 라이브/활성 배치·자동 순회 DB·OCR/PDF cache·백업 pending/receipt 및 재개 근거를 보호한다. 호스트 전체 다른 서비스의 이미지/volume을 CardRAG 잔여물로 간주하지 않는다.

최근 관찰은 호스트 전체 images57개46.59GB/reclaimable22.26GB, build cache8.02GB, volumes168.8GB이며 삭제 권한이나 예상 회수량의 확정 근거는 아니다. 독립 deployment snapshot fix23/fix24가 임시로2개 남아 있다. 실행자는 최신 상태를 다시 조사한다.

## 착수와 보호 대상

PLAN/AMENDMENT, FIX_25/REPORT/REVIEW_13, OPERATIONS_REPORT_10, LIVE_REVIEW_10–11과 최신 Git·systemd·container·pointer·backup ledger를 확인한다. 예약은 매일 KST10:00으로 전환 중이며 실제 enabled/next 상태를 재확인한다. 실행 중 Worker/backup/복구 작업이나 임박한 예약과 정리를 겹치지 않게 한다. 필요한 기존 locks/manual-start 절차를 사용하고 timer를 멈추면 원래 승인된 상태·10시 일정을 안전하게 복구한다.

항상 보호할 대상:

- active /opt/cardrag/current release/image와 검증된 직전 rollback image/release. 배포 인계에서 요구하는 최신 검증된 독립 복구본1개. 최신 DB의 generation/cursor를 오래된 snapshot으로 되돌리지 않는다.
- cardrag-worker-state/auth/paddleocr-models/serving/mcp-state 및 타 서비스의 모든 사용 volume. 개발 volume이라는 명칭만으로 삭제하지 않는다.
- 현재/retained serving generation, resume 가능한 run/checkpoint, seal/receipt/ownership이 참조하는 PDF/OCR/embedding. pending 범위 밖이라는 이유만으로 원장/파일을 손으로 지우지 않는다.
- Git 작업 트리/미커밋 파일 및 .handoff 전체. 비밀 설정/auth와 실제 운영 로그·재현 증거의 유일한 사본.

## 실행 지침

1. image digest/tag/layer 및 container 참조, release/current/rollback 연결, snapshot metadata와 checksum/SQLite 검사, 개발 산출물의 소유·참조를 목록화한다. 각 삭제 후보에 경로/ID·사유·보호 대상 미참조 근거·추정 독점 점유량을 기록한다. 공유 layer의 apparent size 합을 실제 회수량으로 계산하지 않는다.
2. 독립 복구본은 최신 검증된1개를 남긴다는 기존 정책을 적용한다. 최신 snapshot이 유효하고 실제 배포 복구점임을 검사한 뒤 불필요한 이전 snapshot만 제거하며 retention metadata는 기존 규약으로 갱신한다. 현재 live DB를 ad-hoc SQL로 바꾸지 않는다.
3. 종료된 오래된 CardRAG 테스트 container와 미참조 image/release, 확인된 일회성 build/output/temp 파일을 좁은 명시적 대상으로 정리한다. 필요한 실행 결과/오류 로그를 먼저 보존한다. cache/retained run은 지원되는 GC/retention 계약을 우선하며 raw rm으로 우회하지 않는다.
4. 호스트 전체 docker system prune -a, volume prune, blanket rm/find-delete를 금지한다. 다른 서비스나 유일한 증거/복구점인지 판단 불가한 대상은 남기고 이유를 적는다. 별도 전역 tool 설치·설정 변경은 하지 않는다.
5. 정리 전후 df/du/Docker 사용량, 실제 image/mount 존재, 운영 latest run/stable/MCP generation 일치·대표 source page, backup 필수 범위 및 예약10시 상태를 확인한다. root가 필요한 작업은 필요한 구체적 명령을 준비하되 권한 우회는 하지 않는다.

## 완료 보고

FIX_26_REPORT.md에 삭제한 정확한 대상·남긴 대상·실제 확보 bytes·검증 결과·미처리 이유·예약 복구 상태를 기록한다. 장기 보존 정책은 active+검증 rollback 및 독립 snapshot1개를 명시하되 추가 자동삭제 daemon을 이번 작은 FIX에 도입하지 않는다. 운영 소스 변경이 없는 정리에 새 Worker image를 만들지 않는다.
