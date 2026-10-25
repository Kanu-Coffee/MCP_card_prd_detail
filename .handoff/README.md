# Handoff Index

최종 갱신: 2026-10-21  
현재 활성 과제: **019_selected-issuer-unresolved-history-preservation**

이 문서는 `.handoff/`의 현재 상태를 한눈에 보기 위한 인덱스입니다. 기존
`PLAN.md` / `REPORT.md` / `FIX_*` / `REVIEW_*` / `OPERATIONS_REPORT_*`는 역사
증거로 그대로 보존하며, 과거 디렉터리를 삭제·이름 변경하지 않습니다.

## 상태 기준

- **ACTIVE**: 현재 진행 중이며 다음 실행·검증이 남아 있음.
- **CLOSED**: 계획 범위의 구현·검증·운영 인수가 끝남.
- **SUPERSEDED**: 후속 과제가 이어받았거나 당시 범위가 종료됨. 잔여 항목은
  후속 과제에서 관리.

## 전체 과제

| No | 과제 | 핵심 내용 | 판정 | 최종 근거 | 잔여 / 이관 |
|---|---|---|---|---|---|
| 001 | `v1029-pdf-lineage-ocr-cache-recovery` | v1.0.28 seed에서 PDF lineage·OCR을 보존하고 신규 14건만 Paddle OCR | CLOSED (002에서 운영 인수) | `001.../REPORT.md` §7 | 후보 run 최종 관측은 002/004 운영 단계로 이관 |
| 002 | `production-recovery-gc-release-disk` | OCR seed 복원, 원격 GC 버그 수정, v1.0.29 공개 릴리스, 디스크 정리 | CLOSED | `002.../REPORT.md` | 없음 |
| 003 | `daily-batch-retirement-rolling-baseline` | 정당한 은퇴 분류와 rolling corpus baseline으로 03시 배치 실패 해결 | SUPERSEDED by 004 | `003.../REPORT.md` §3–4, Addendum | 연속 예약 실행 관측은 004로 이관 |
| 004 | `worker-cutover-integrity-closeout` | Worker lock·은퇴 판정·OCR 무결성 수정, stable 배치, 볼륨 정리 | CLOSED | `004.../REPORT.md` §6–8 | GC 재활성화는 운영 권고로만 남음 |
| 005 | `public-release-evidence-sealing` | v1.0.32 공개 Docker/GitHub 릴리스와 증거 봉인 | CLOSED | `005.../FIX_03_REPORT.md` | 문서·잔여 항목은 후속 릴리스에서 관리 |
| 006 | `opencode-ocr-provider` | OpenCode OCR 호출자 구현·실사 프로브 | SUPERSEDED | `006.../REPORT.md` | 운영 활성화는 채택되지 않았고 현재 런타임은 `codex-exec` |
| 007 | `content-addressed-ocr-cache` | PDF 내용 기준 OCR cache 전면 개편·기존 산출물 이전 | CLOSED via 008 | `007.../EXECUTOR_CLOSEOUT_20261007.md`, `008.../EXECUTOR_COMPLETION_20261007.md` | 006/007 통합 운영 인수는 008에서 완료 |
| 008 | `parallel-issuer-collection` | 카드사 병렬 수집·장애 격리, 006/007 통합 운영 | CLOSED (핵심 기능 인수) | `008.../EXECUTOR_COMPLETION_20261007.md` | OpenCode 활성화·main 병합·릴리스는 후속 과제로 이관 |
| 009 | `product-summary-field-classification` | 상품 요약 필드 분류와 Worker stage skip | CLOSED via 010 | `009.../ACCEPTANCE.md`, `010.../REPORT.md` | 운영 반영은 010에서 완료 |
| 010 | `009-production-cutover` | 인수된 009의 운영 반영 | CLOSED | `010.../REPORT.md` | 다음 03시 관측은 012 운영 리뷰로 이관 |
| 011 | `cross-issuer-summary-semantics` | 카드사 공통 상품 요약 문장·표 셀 분류 | CLOSED via 012 | `011.../ACCEPTANCE.md`, `012.../REPORT.md` | v1.0.34에 포함되어 운영 반영 |
| 012 | `v1034-release-cutover` | v1.0.34 발행·운영 반영 | CLOSED | `012.../REPORT.md`, `012.../OPERATIONS_REVIEW_20261009.md` | 예약 배치 관측은 013 이후로 이관 |
| 013 | `local-serving-incremental-webdav-backup` | 로컬 serving과 WebDAV 증분 백업 분리 | CLOSED | `013.../CLOSEOUT_v1.0.35.md` | 다음 systemd 03시 관측은 014로 이관 |
| 014 | `worker-startup-failure-codex-validation` | 03시 Worker 초기화 실패 진단과 Codex 업데이트 검증 | SUPERSEDED by 015 | `014.../RELEASE_REPORT_v1.0.36.md` | 03시 실패 원인 미상, startup retry/disk는 015로 이관 |
| 015 | `worker-startup-retry-disk-capacity` | startup 재시도·디스크 확보·용량 예측 재검토 | SUPERSEDED by 016 | `015.../ACCEPTANCE_FIX_02.md`, `015.../RUNTIME_REVIEW_20261015.md` | 최종 이미지 full batch 관측과 반복 cosine은 016으로 이관 |
| 016 | `worker-repeat-cosine-preflight-failure` | 반복 임베딩 품질 검증 실패 진단·복구 | CLOSED (운영 평가 배포) | `016.../DEPLOYMENT_EVALUATION_20261016.md`, `016.../ACCEPTANCE_FIX_04.md` | 품질 calibration은 후속 평가, 롯데/신한 연결·수집은 017로 이관 |
| 017 | `issuer-incremental-acquisition-review` | 증분 수집·90일 감사·프록시·카드사 선택·일일 순회 | SUPERSEDED by 018/019 | `017.../REVIEW_02.md` | 실제 배포·라이브 검증·미해결 이력은 018→019로 이관 |
| 018 | `worker-manual-live-validation` | Worker 운영 적용과 수동 8사 라이브 검증 준비 | CLOSED | `018.../CLOSURE.md` | 8사 수동 검증·백업·timer는 019로 이관 |
| 019 | `selected-issuer-unresolved-history-preservation` | 선택 카드사 실행에서 미해결 개정 이력 보존 | **ACTIVE** | `019.../PLAN.md`, `REVIEW_11.md`, `OPERATIONS_REPORT_09.md` | 롯데 resume 완주, 백업, 신한, 8사, timer |

## 현재 활성: 019

- 최신 코드 검토: `FIX_22`·`FIX_23`까지 완료, `REVIEW_11.md`에서 검증 통과.
- 최신 운영 반영: `OPERATIONS_REPORT_08.md`에서 `/opt/cardrag/019-fix23-e9b9cdca1a22` 배포 완료.
- 최신 실행: `OPERATIONS_REPORT_09.md`에서 기존 롯데 run
  `49845932e05849d3a0110a2bc82d6c1c`을 새 이미지로 resume.
  - container: `cardrag-live-lotte-resume-20261021T053259Z`
  - 2026-10-21 14:32:59 KST 시작, 실행 중
  - 직전 확인: OCR 475/707 완료, failed 0
- 아직 완료로 판정하지 않은 항목:
  - 이전 실패 지점 통과와 실제 Paddle fallback 추론
  - 롯데 배치 완주·게시·비선택 문서 변경 0
  - `check-issuer.sh lotte --flush`의 scoped backup `backup_scope_complete=true`
  - live MCP generation 확인
  - 신한 배치와 나머지 카드사, timer 재개

## 다음 진입점

1. 현재 실행 상태: `docker logs -f cardrag-live-lotte-resume-20261021T053259Z`
2. 종료 후: `/opt/cardrag/current/operations/check-issuer.sh lotte --flush`
3. 019가 끝나기 전에는 timer를 재개하거나 과제를 CLOSED로 바꾸지 않습니다.

## 종료 처리 메모

- 001–017은 개별 `CLOSURE.md`가 없는 과제도 있습니다. 이 인덱스의 판정과 최종
  근거 문서를 기준으로 CLOSED/SUPERSEDED 처리합니다.
- 018은 `018.../CLOSURE.md`로 명시 종료되어 있습니다.
- 019는 ACTIVE이며, 위 잔여 항목이 끝난 뒤 별도 `CLOSURE.md`를 작성합니다.