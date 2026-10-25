# 019 Reviewer 최종 코드 검토 — FIX_05 해결 확인

2026-10-19 Reviewer: Codex. **판정: 현재 코드·테스트 범위에서 리뷰 통과. 추가 수정 요청 없음. 운영 검증 완료 판정은 별도다.**

## 기준과 검토 범위

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, 원본 REPORT와 FIX_01–05/각 REPORT, 현재 Git 상태와 관련 구현·테스트를 대조했다. HEAD `aaeb2c4` 위에 017–019 변경이 미커밋으로 공존하는 상태를 검토했으며, 특정 커밋이나 배포 이미지에 대한 인증은 아니다. 기존 문서는 보존하고 이 검토 기록만 추가한다.

직전 리뷰에서 FIX_04의 두 P1은 해결 확인했고, 이번에는 남은 FIX_05의 canonical 정렬 및 관련 회귀를 확인했다. 과거 이력 제외를 허용한 PLAN_AMENDMENT_01 정책을 기준으로 판정한다.

## FIX_05 해결 근거

- `pipeline.py`의 `canonical_acquired_document_key`가 기존 일반 실행의 `(issuer, product_code, effective_date, source_version, pdf.sha256)` 정렬 계약을 표현한다.
- 일반 실행과 replay가 같은 key를 사용한다. replay는 비선택 보존 입력을 합친 뒤 정렬하며, `_v5_corpus_identity_payload`도 전달된 전체 입력을 같은 key로 정렬해 documents와 materialized revision identity에 사용한다. manifest의 document_id 순서에 corpus SHA가 의존하던 문제가 해소됐다.
- 새 실제 pipeline 회귀 테스트 `test_reprocess_hash_matches_canonical_normal_run_hash_and_yields_no_change`가 4문서 ReuseSource 재처리와 ledger 1→0 정비 후 일반 실행과의 corpus SHA 일치 및 후속 no-change를 검증한다.
- Reviewer 보조 검증에서도 두 issuer의 획득 입력 3개를 섞은 모든 6가지 순열이 동일한 corpus SHA를 생성했다. 이는 입력 결합 순서에 대한 helper 검증이며, 실제 8사 운영 실행 검증을 대신하지 않는다.

이번 변경과 관련해 추가로 수정해야 할 결함은 발견하지 않았다. FIX_01–05에서 요청한 수정 검토를 현재 코드·테스트 범위에서 마무리한다.

## Reviewer 직접 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1390 passed, 10 warnings in 53.04s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

git diff --check
통과
```

전체 Worker 테스트에 019 전용 20건과 관련 scope/partial-execution/OCR/cache 회귀가 포함된다. warning 10건은 기존 invalid prior OCR/원격 OCR variant 충돌/whole-document fallback 테스트에서 발생했다. 새로운 실패는 없었다. 별도 보조 검증은 SourceRecord fixture 필수값을 갖춘 임시 Python 실행으로 수행했으며 네트워크/운영 파일을 사용하지 않았다.

## 운영 인계와 남은 검증

제품 코드, 운영 DB/serving, 배포 이미지 및 timer는 이 리뷰에서 변경하지 않았다. 코드 검토 통과가 실제 baseline 22건의 처분 완료, 8사 5,536문서 보존, 최신 상품/OCR 정상 반영, 이번 run의 백업 완료를 의미하지 않는다.

후속 운영은 기존 018/019 경계를 따른다. 식별 가능한 이미지/release와 복구점을 준비한 뒤 사용자가 우리카드(`run --issuer woori`)를 수동 실행한다. container exit/run 결과, 최종 ledger/정비 inventory, 현재·비선택 serving 및 처리 미호출, degraded/unsupported, 해당 run의 백업 등록·완료를 확인한다. `backup_pending=[]`만으로 이번 run 백업 성공을 판정하지 않는다.

우리 성공 후 나머지 7사를 순차 검증하고 결과에 따라 timer를 조정한다. 자동 round-robin cursor 검증과 수동 8회 실행을 구분하고, 실제 운영 검증 결과는 후속 운영 문서에 남긴다. 이번 코드 리뷰 때문에 자동 배치를 즉시 재개하지 않는다.
