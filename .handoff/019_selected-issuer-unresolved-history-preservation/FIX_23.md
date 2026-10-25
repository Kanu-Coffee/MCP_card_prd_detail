# 019 FIX_23 — FIX_22 리뷰 보완 및 Codex 한도→로컬 Paddle 전환

2026-10-21 Codex. 사용자는 Reviewer 검증과 함께 작은 fallback 보완은 현재 수정하여 다음 resume에 적용하도록 명시했다. 현재 run은 failed/비실행 상태다. 기존 FIX_22/REPORT, LIVE_REVIEW_08과 현재 코드에서 아래 국소 보완을 확인했으며 이번 턴에서 직접 구현·검증하고 FIX_23_REPORT.md와 REVIEW_11.md를 신규 작성한다. 기존 handoff를 덮어쓰지 않는다.

## 리뷰 보완

1. ContentOCRVariantStore._lookup_local의 full content manifest 복원은 source/body/epoch뿐 아니라 해당 seal의 content variant ID와 일치해야 원본 복원으로 인정한다. 다른 metadata에서 유도한 valid sibling manifest를 같은 body라는 이유로 원본 variant로 오인하지 않는다. 원본을 복원할 수 없으면 기존 FIX_22의 명시적인 로컬 imported 재사용 경로를 유지하며 remote variant가 이미 게시됐다고 선언하지 않는다.
2. `_finite_stage`의 새 DB 실패 진단은 discovery처럼 document_id가 issuer-lotte인 호출에도 도달한다. 해당 단계에서 strict doc/source hash validator가 2차 ValueError를 내어 원래 DB 진단을 숨기지 않게 한다. document_id는 실제 검증된 doc/source ID일 때만 싣고 stage/reason/category를 유지한다. 임의 ID·예외 text를 공개하지 않는다.

## 작은 fallback 개선

- FailoverOCRResolver에서 primary provider=codex-exec, fallback provider=local-paddleocr이고 typed ProviderSystemicError.reason_code=provider_process_rate_limited인 경우에만 기존 whole-document fallback 경로를 허용한다.
- authentication/configuration/credential/integrity/알 수 없는 process error 등 다른 systemic 오류는 전파한다. remote fallback, 다른 primary의 quota 오류도 이 변경으로 새롭게 허용하지 않는다.
- schema/settings/모델/contract/reuse key/체크포인트 형식·epoch를 바꾸지 않는다. Codex 부분 chunk는 남겨 두고 결과는 기존 정책대로 Paddle의 전체 문서로 검증한다. 두 provider의 OCR 텍스트를 섞지 않는다. fallback 자체 실패/취소는 전파하고 무한 재호출·전체 run circuit 상태를 도입하지 않는다.
- 경고·보고서에 provider stderr, args/notes/context, 인증정보를 포함하지 않는다. 기존 generic fallback warning을 사용한다.

## 검증 및 적용

seal variant mismatch 및 discovery DB 실패 부정 fixture, 실제 OCRResolver를 쓰는 quota→로컬 whole-document fallback/부분 chunk 보존·재시도, 비허용 systemic/다른 provider 조합/취소 회귀를 검증한다. 관련 및 Worker/Core 전체 검사와 ruff/diff check를 통과시킨다. 변경이 위 경계 안에 머무르면 현재 run resume 전에 식별 가능한 운영 release로 반영하고 provider/model/contract/checkpoint가 유지됨을 확인한다. 실제 롯데 resume 자체는 이번 요청에서 시작하지 않는다. Paddle 실제 모델 로딩/추론과 배치 완주는 테스트 대역 결과로 주장하지 않는다.
