# 009 최종 인수 검토

- 작성일: 2026-10-08. 역할: Reviewer.
- 최종 구현: `b544a80`, `codex/009-summary-stage-reuse`.
- 검토 문서: PLAN/REPORT, FIX_01 및 REPORT, FIX_02 및 REPORT. 실제 코드와 Git/테스트 증거를 대조했다.
- **판정: 009 개발 범위 인수 가능. 운영 반영 단계로 진행 가능. 추가 FIX 없음.**

## 확인 결과

1. 요약 분류: 500107의 실제 할인 문구/연회비/근거 개선, 변경 고지 배제, 명시적 할인 제외와 비율 표 행 보정 확인. 8개 카드사30상품 fixture 및 다양한 혜택 검증 인정.
2. 부분 실행: 각 stage의 명시적 skip, 출처/산출물 검증, provider 호출 억제, local-only와 게시 완료 구분, stable frozen-source 경합 차단 및 exact resume 확인.
3. OCR 실패 복구: frozen PDF source의 실패 문서 재처리, 성공/실패 count 재계산, finite retry/95%/historical 규칙 유지 확인.
4. FIX_02: 수동 요청 전체의 hash와 PDF/OCR/content variant에 결합된 성공 proof를 저장하고, 검증된 seal/게시 generation과 대조한 후에만 요청을 완료한다. 실패·미검증 요청은 batch 게시가 성공해도 pending이다. partial seal resume에서도 proof 누락/변조 또는 실패 disposition으로 false receipt가 생성되지 않는다.
5. 기본 전체 수동 요청의 정상 성공 게시도 확인했다. 요청이 없는 기본 no-change 의미는 유지된다.

Reviewer 재실행:

```sh
.venv/bin/pytest -q apps/cardrag-worker/tests/test_partial_execution.py \
  apps/cardrag-worker/tests/test_ocr_requests.py apps/cardrag-worker/tests/test_seal.py \
  apps/cardrag-worker/tests/test_webdav.py apps/cardrag-mcp/tests/test_summary_fields.py \
  apps/cardrag-mcp/tests/test_metadata_tools.py
```

**195 passed / 17.64초**. Executor의 전체 **2462 passed / 68.97초**, Ruff/mypy 성공 기록도 확인했다. 코드 inspection에서 새 인수 차단 사항은 발견하지 않았다. 전체 suite를 이번 검토에서 반복하거나 실제 유료 추론/long Worker를 추가할 필요는 없다.

## 운영 반영과의 구분

현재 운영은 아직 기존 이미지다. 이 문서는 수정본이 운영 배포됐다는 보고가 아니다. 운영 readiness read-only 조회는 true였다. 실제 전환 절차는 새 `.handoff/010_009-production-cutover/PLAN.md`에 정리했다.

배포 전 확인한 실제 Compose 최종값은 Worker `cardrag-worker:008-6b42a1a`, OCR `opencode` / `alibaba-token-plan/qwen3.8-flash`, MCP `cardrag-mcp:007-31edb1d`다. `/etc/cardrag/*.env`의 과거 이미지/Codex 값보다 운영 snapshot의 host-local compose.secrets.yaml override가 우선한다. 그 파일을 저장소 기본본으로 덮으면 Opencode 설정과 이미지 pin이 사라지므로 전환 계획에 보존 지시를 포함했다.

GitHub CI는 feature push만으로 실행되지 않는다(현 CI trigger는 PR/main push/workflow_dispatch). 현재 브랜치의 실행 목록과 열린 PR은 비어 있었다. 개발 인수 근거는 위 검증이며, 전환 과정에서 기존 PR CI를 수행한다. 새 공개 릴리스나 오프라인 평가 gate를 009 운영 반영 조건으로 추가하지 않는다.

FIX_02 이전 실행에 성공 proof가 없으면 완료를 추정하지 않고 pending을 유지하는 보수적 동작, 최대5개 요약 슬롯으로 일부 정상 혜택이 뒤로 밀리는 제한을 인정한다. 이것들을 해결하기 위한 추가 재설계/전량 OCR를 이번 인수 조건으로 두지 않는다.

운영 볼륨/설정/이미지/타이머/소스는 이번 Reviewer 턴에서 변경하지 않았다. 사용자 Excel3개 및 과거 handoff 문서는 보호했다.
