# Handoff 릴리스 인덱스 — v1.0.38

2026-10-31 KST 기준, 020(Paddle 임시 링크 격리/복구) 및 021(OCR 로깅) 완료, 022(우리카드 RAONK 세션 인증 복구 및 운영 배포·재개 완주) 인수 후 v1.0.38 소스 릴리스 및 운영 정합화를 진행 중인 인덱스입니다.

001–019는 `STATUS_v1.0.37.md`에 따라 이미 CLOSED 처리된 역사 기록이며, 다시 열리지 않습니다.

| 과제 | 현재 상태 | 종료·인수 근거 |
|---|---|---|
| 001–019 | CLOSED | [v1.0.37 종료 인덱스](STATUS_v1.0.37.md) 참조 |
| [020_paddle-fallback-symlink-capacity-recovery](020_paddle-fallback-symlink-capacity-recovery/PLAN.md) | CLOSED | [020 CLOSURE.md](020_paddle-fallback-symlink-capacity-recovery/CLOSURE.md) |
| [021_ocr-provider-call-logging](021_ocr-provider-call-logging/PLAN.md) | CLOSED | [021 REPORT.md](021_ocr-provider-call-logging/REPORT.md) 및 운영 021 적용 |
| [022_woori-raonk-session-auth](022_woori-raonk-session-auth/PLAN.md) | RELEASE_PENDING | [REVIEW_01.md](022_woori-raonk-session-auth/REVIEW_01.md), [REVIEW_02.md](022_woori-raonk-session-auth/REVIEW_02.md), FIX_06 릴리스 배포 진행 |

---

## v1.0.38 인수 및 운영 상태 요약

1. **우리카드 RAONK 세션 인증 복구 (022)**:
   - `REVIEW_01.md`에서 구현 인수 완료.
   - `REVIEW_02.md`에서 운영 배포 및 동일 실패 run(`df29d8e5dbb34048942a5296bb2d6ac8`) 재개 완주 인수 완료.
   - 918건 PDF 수집 성공, 4건 DRM 보호 문서 안전 스킵, 원본 서비스 오류 0건.
   - 신규 서빙 generation `g-df29d8e5dbb34048942a5296-5a3a99224f92` 게시 및 라이브 `cardrag-mcp` 반영 확인.
   - 비선택 7사 매니페스트 레코드 변경 0건(완전 보존).
   - 대상 백업 scope 완결 (`backup_scope_complete: true`).

2. **Paddle 심볼릭 링크 격리 및 복구 (020)**:
   - 임시 PDF 입력을 `/tmp`로 격리하여 상태 트리 오염 방지.
   - `cardrag-worker paddle-alias-recovery` 도구를 통해 잔존 링크 안전 점검 및 복구 지원.

3. **OCR 프로바이더 구조화 로깅 (021)**:
   - `OCR_EVENT`(`cardrag.ocr-event.v1`)를 통한 프로바이더 호출 추적.
   - 민감정보(텍스트, 키)를 제외한 안전한 운영 진단 확보.

4. **운영 환경 및 백업 정책 정비**:
   - 단일 독립 스냅샷 정책 준수: 과거 019/021 스냅샷 정리 후 독립 022 스냅샷 1개 유지.
   - 코드 롤백(021)과 DB 복원 분리.
   - 타이머 유지: `cardrag-worker.timer` 매일 **10:00 Asia/Seoul** (Persistent=false) 정상 활성 유지.
   - 범위 밖 과거 백업 대기 항목(4건, 300,444 bytes)은 보존.
