# 007 Executor 마감 보고 — 운영 이전 완료, 카드사 연결 장애로 배치 인수 미완료

작성 역할: Executor. 2026-10-07 KST. 기준 Git `feat/007-content-ocr-cache`, `ff943472715424e3fd1ee3337fee5a0d0b6bdf2c`. 기존 REPORT/FIX 보고서는 시점별 이력으로 보존한다. 본 문서는 현재 세션의 마감 요약이며 007 전체 완료 선언이 아니다. 후속 작업은 `.handoff/008_parallel-issuer-collection/PLAN.md`로 인계한다.

## 완료한 개발과 검증

- 006 OpenCode provider 코드·고정 바이너리·opt-in Compose·격리 설정을 007 브랜치에 통합했다. 운영 provider는 아직 기존 `codex-exec / qwen3.8-flash`다. OpenCode 운영 활성화는 하지 않았다.
- 007은 PDF SHA/크기/쪽수/epoch 기반 content 키, 불변 OCR variant/provenance, run 목록 snapshot과 문서별 선택 고정, generation의 정확한 variant 참조, provider와 generation 계약 분리, 이전/검증/조회/재처리/복원 CLI, content 참조 GC 보호를 구현했다.
- 서비스 중 같은 PDF의 서로 다른 OCR도 문서별 SHA로 pin하여 유지한다. SQLite 인덱스·레거시 실시간 역조회 대신 평면 원격 index와 run JSON snapshot, 전량 이전 게이트를 사용한다. 이 편차를 기능 실패로 취급하지 않는다.
- 배포 코드 `31edb1da1033b85e81bb30e0cf98e7e055e5d892`의 CI run `37565367823` 성공(전체 테스트 2,327개 등). 이후 커밋은 보고서이며 제품 코드는 동일하다. 이번 마감에서는 테스트를 다시 실행하지 않았다.

## 실제 운영 수행 결과

| 항목 | 결과 |
|---|---|
| 현재 배포 경로 | `/opt/cardrag/current -> /opt/cardrag/007-31edb1d` |
| MCP | `cardrag-mcp:007-31edb1d`, 기존 stable 서비스 중, 이번 재점검도 healthy |
| Worker | `cardrag-worker:007-31edb1d`, 기존 state/auth 볼륨 유지 |
| migration apply | 13:32 시작, 보존된 Docker 종료 코드 0. 연결 stdout 소실로 apply JSON은 비었으며 성공 JSON을 조작하지 않음 |
| 독립 전량 verify | 14:56~14:58 종료 코드 0, 5,172 variant / stable OCR 5,512문서 전량 대응 |
| stable 기준 | `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd` |
| 첫 배치 | `cardrag-prod-007-fix01`, run `bb471003c6844c7394aae97fd9f85f26`, 15:12~15:17 종료 코드 1, OOM 아님 |
| 첫 배치 실패 | 우리 915 / KB 751 discovery 이후 신한 첫 GET의 연결 reset. OCR 및 새 generation 검증 단계에 도달 못함 |
| 예약/GC | timer active 유지. 원격 GC 승인·수집 모두 false |
| 롤백 | `/opt/cardrag/v1.0.29` 및 `operations/rollback.json`의 이전 설정·이미지 1세트 유지 |

2026-10-07 마감 재점검: 실제 배치 컨테이너 Exited(1), verify 컨테이너 Exited(0), MCP healthy, timer active, current 새 경로, 루트 가용 약 69G를 확인했다. 오래 걸리는 Worker/migration을 새로 기동하지 않았다. `/etc/cardrag/*.env`는 수정하지 않았다. timer 중지 권한은 이전에 거부되었으며 같은 worker.lock과 실행 여부 점검으로 중복을 방지했다.

## 신한 연결 장애 근거와 판정

호스트 직접 HTTP, 별도 Chromium, 사용자 Firefox 모두 reset. 기존 PC URL뿐 아니라 공식 모바일 상품공시실과 AJAX·홈페이지에서도 실패했다. DNS 조회 일치, TLS 1.3 인증서 검증 성공 후 HTTP 응답 전 연결이 끊긴다. URL/파서 오류로 입증되지 않았으므로 adapter URL·source identity는 변경하지 않았다.

사용자 추가 확인: 다른 장소·다른 회선 컴퓨터는 정상. 같은 공유기의 별도 Ubuntu 서버 `192.168.0.3`에서는 홈페이지 `curl -IL`과 `curl -Ik` 모두 `curl (56) Recv failure: Connection reset by peer`. 따라서 공통 출구 IP 접근 제한 또는 공유기/회선 경로 문제가 우선 의심되지만 **신한 측 IP 차단 확정 증거는 없다**. 공인 IP 변경·공유기 재설정·프록시 설치는 하지 않았다.

## 운영 증거 위치

`/opt/cardrag/007-31edb1d/operations/`의 `rollback.json`, `stable-ocr-baseline.json`, `migration-exit-code.txt`, `migration-verify.json`, `migration-recovery-verified.json`, `worker-start.json`, `worker-result.json`, `worker-first-run.log`, `post-failure-stable.json`, `shinhan-connectivity-diagnostic.json`, `transition-status.json`. baseline은 문서별 PDF/OCR SHA·크기이며 OCR 본문·비밀값은 없다. `transition-status.json`은 현재 실패 기록이고 Docker 상태를 대신하는 지속 모니터가 아니다.

## 미완료와 후속 인계

007 첫 정상 배치의 기존 OCR 무재호출·SHA 보존·generation 게시·MCP 반영은 아직 미검증이다. 006 운영 provider 활성화도 미실행이다. 신한 복구만 기다리는 대신 사용자가 요청한 카드사별 병렬 수집·실패 격리·기존 카드사 데이터 보존을 신규 008로 계획한다. 008은 006/007 코드와 운영 이전을 재사용하고 위 잔여 인수를 함께 검증한다. 기존 migration을 무조건 반복하거나 전량 OCR/Paddle를 돌리지 않는다. PR #40은 Draft이며 merge·정식 공개 릴리스는 수행하지 않았다.
