# 019 FIX_22 — 롯데 같은 run resume의 OCR identity 보존과 진단

2026-10-21 Reviewer: Codex. LIVE_REVIEW_08의 실제 운영 입력 재현 및 최소 fixture를 근거로 발행한다. PLAN/PLAN_AMENDMENT_01, FIX_21/REPORT·REVIEW_10, LIVE_REVIEW_07–08, OPERATIONS_REPORT_07과 현재 Git 상태를 읽고 가장 높은 미보고 FIX를 수행한다. 세 경계가 같은 resume 실패를 구성하므로019 안에서 국소 보완하며 새 계획은 만들지 않는다. 결과는 FIX_22_REPORT.md를 신규 작성한다. 기존 문서와 미커밋 변경은 보존한다.

## 1. [P1] content variant ID와 원본 metadata의 정합성

`content_cache.py:ContentOCRVariantStore._lookup_local`은 기존 content seal의 variant ID에 새 generation-only provenance/created_at 등 재구성한 field를 붙여 Core validator를 위반한다. 정상 원본 manifest에서도 재현되며 Core의 검증을 완화하거나 model_construct로 우회해서는 안 된다.

- 원래 content manifest를 복원할 수 있으면 실제 검증된 원본 metadata/READY/output/source/epoch/variant를 한 묶음으로 대조하여 재사용한다. 단순 64hex ID/파일 존재로 신뢰하지 않는다.
- 원래 full manifest가 없고 검증된 generation seal+동일 body만 있는 경우, 기존 프로젝트의 generation-only/로컬 sealed 재사용 계약을 적용한다. 서로 다른 metadata에 기존 variant ID를 부착하지 않는다. 새로 유도한 imported variant가 필요하면 그것의 identity/selection/publish/backup과 seal 재사용 의미를 일관되게 처리하고, 없는 원격 variant를 이미 게시됐다고 보고하지 않는다.
- 문서 OCR bytes·retained SHA/size·page identity를 유지하며 캐시 metadata 때문에 정상 문서를 provider로 재처리하거나 OCR 실패/retired로 바꾸지 않는다. 잘못된 후보는 명시적 진단/안전한 miss로 다루되 검증된 대체 원본이 있으면 다음 경로를 사용한다.
- fixture는 임의의 'c'*64가 아닌 **실제로 create/validator를 통과한 content manifest**에서 시작한다. 원 generation→다른 generation 계승, provenance/created_at/migrated_from 등 hash field가 다른 재구성, native/adopted/content/generation-only를 구분한다.

## 2. [P2] 동일 run의 정상 own OCR을 무조건 충돌로 보지 않음

pipeline `_retained_identity_conflict`의 own.is_file→True는 같은 run resume에서 검증 가능한 완료 파일을 prior binding에서 제거한다.

- own 파일과 현재 PDF/source/retained seal 및 OCR SHA/size의 동등성을 검증해 같으면 정상 재사용을 허용하고, 바이트가 다른 실제 충돌·symlink/비정상 경계는 보수적으로 거부/진단한다. 파일 존재만으로 충돌 또는 성공 판정을 하지 않는다.
- 성공 stage라도 입력/contract/identity가 변했으면 이전 결과를 무조건 통과시키지 않는다. 반대로 동일한 입력의 완료 OCR·부분 chunk checkpoint는 보존한다.
- 운영을 닮은 실패→resume fixture에서 native/content 완료 문서, own root OCR 존재, prior seal, 별도 문서의 quota 중단/부분 chunks를 함께 구성한다. 같은 run resume이 이전 완료 문서의 provider 호출0·bytes 유지 상태로 미완료 문서에 도달해야 한다. 최신 discovery 정책·비선택 불변·attempt epoch 재개를 유지한다.

## 3. [P2] succeeded-stage OCR 예외의 안전한 진단

`_finite_stage`의 succeeded-operation 재실행에서 발생한 오류는 formatter를 거치지 않아 process_document에서 generic bookkeeping error로 숨겨진다.

- 정상적인 cache/identity 검증 오류, provider systemic, 실제 stage 기록 실패를 구분한다. run/document/stage/제한된 reason code와 error category를 구조화된 보고서/CLI에 남겨 다음 장애가 원인 불명의 runtime error로만 남지 않게 한다.
- 공급자 stderr·exception args/notes/context·인증정보를 그대로 노출하지 않는다. Core Pydantic 전체 input_value를 보고서로 복사하지 않는다. 기존 secret-safe 경계와 typed failure 계약을 유지한다.
- 완료 stage 재검증 실패와 실제 stage DB 기록 실패 각각의 부정 fixture를 추가한다. 진단을 위해 완료 stage/receipt/seal을 수작업 수정하거나 실패를 성공 처리하지 않는다.

## 검증/운영 인계

관련 OCR/content/local-seal/state/pipeline 회귀 뒤 요구 Worker/Core 검사·ruff·diff check를 실행한다. 변경 범위·최소 재현 수정 전/후·잔여 미검증을 보고서에 기록한다. 필요 이상의 cache/schema/정책 재설계는 피한다. Paddle의 systemic fallback 정책은 이번 결함 해결과 별개이므로 임의 확장하지 않는다.

실제 운영 상태: FIX_21 release, run49845932e05849d3a0110a2bc82d6c1c failed; OCR330/707·checkpoint97 모두 보존, stable5545건 유지. 구현 단계에서 운영 DB/원격/seal/cache를 조작하거나 재배치하지 않는다. 검토 통과한 후 기존 승인된 운영 절차로 source 식별·복구점·용량을 확인하여 반영하고 같은 run을 resume한다. 실제 완주·비선택 변경0·scoped backup complete=true·MCP live generation까지 확인하기 전019/8사 인수를 닫거나 timer를 재개하지 않는다.
