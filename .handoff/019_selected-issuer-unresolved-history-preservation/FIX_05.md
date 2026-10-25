# 019 FIX_05 — replay corpus canonical 순서 불일치 수정 요청

2026-10-19 Reviewer: Codex. **판정: FIX_04의 두 P1은 해결 확인. 아래 P2 1건은 수정 필요.**

## 검토 범위와 해결 확인

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, 원본 REPORT, FIX_01–04 및 각 REPORT와 현재 Git 상태/관련 구현·테스트를 대조했다. HEAD는 `aaeb2c4`이며 017–019 변경이 미커밋으로 공존한다. 기존 이력은 보존하고 다음 correction인 본 문서만 추가한다.

FIX_04 관련 다음 수정과 검증을 확인했다.

- CLI가 `publication_transport`, `serving_dir`, `channel`을 따르며 current 조회 실패/None에서 적용을 차단하고 transport를 정리한다. actual current와 등록 seal/manifest 검증이 필수가 됐다. 기존 latest-ready fallback은 제거됐다.
- replay에서 획득 입력을 별도로 보존하여 OCR 실패 문서의 존재하지 않는 `.source` 속성 접근을 제거했다. 실제 20건 중 1건 OCR 실패와 ledger 정비를 결합한 테스트 및 threshold/역사 실패 검사가 추가됐다.
- CLI 실제 transport 경계 테스트와 DRM 상태 확인이 보완됐다. 운영 명령 `--issuer`도 정정됐다.

남은 문제는 FIX_04가 요구한 **일반 실행과 동일한 canonical acquisition 순서**다. source dictionary의 순회 순서는 이 계약을 대신할 수 없다.

## [P2] ReuseSource replay가 manifest의 document_id 순서로 corpus를 다시 해시해 다음 실행에 불필요한 재게시 발생

위치: `apps/cardrag-worker/src/cardrag_worker/partial_execution.py:489–519,747,842–853`.

일반 실행은 corpus 입력 `acquired`를 `(issuer, product_code, effective_date, source_version, pdf.sha256)`로 정렬한다(`pipeline.py:4666` 이후). 반면 replay는 `source.documents.items()` 순회 순서대로 `replayed_acquired`를 추가하고, 비선택 보존 입력도 끝에 단순 append한 뒤 그 순서 그대로 `_v5_corpus_identity_payload`에 전달한다.

`ReuseSource.documents`는 manifest의 documents 순서를 유지하는 dictionary이고, manifest는 `document_id`로 정렬된다(`pipeline.py:7551`). 따라서 문서가 여러 개면 일반 실행의 canonical 순서와 달라질 수 있다. source 항목을 보존하는 수정은 맞지만 “canonical acquisition 순서 유지”라는 REPORT 설명은 ReuseSource 재처리에 대해서는 맞지 않는다.

별도 실제 pipeline fixture 재현:

1. 동일 카드사의 현재 문서 4건을 게시한다.
2. `PDFSourceIdentity.from_source_record`로 identity를 계산한 과거 source/revision을 추가하고 metadata 부재 ledger 1건을 baseline에 재현한다.
3. baseline `ReuseSource`로 PDF reuse 재처리를 실행한다. 현재 문서 4건은 그대로이고 ledger만 1→0으로 정비된다.
4. 게시 ledger는 0건이지만 replay의 corpus SHA는 일반 실행에서 동일 4건/0 ledger로 계산한 원래 SHA와 다르다.
5. 입력을 바꾸지 않고 일반 실행하면 `no_change` 대신 `succeeded`로 다시 게시하며 corpus SHA가 원래 값으로 돌아간다.

현재 데이터 손실이나 게시 중단을 재현한 것은 아니다. 그러나 재실행/no-change 안정성과 canonical hash 계약이 깨져 불필요한 재게시가 발생하므로 P2로 분류한다. 이전 P1 해결을 부정하는 판정도 아니다.

수정 요구:

- ledger 변경으로 corpus를 재계산할 때 성공/실패/비선택 획득 입력을 모두 포함한 목록을 **기존 일반 실행과 동일한 canonical key**로 정렬한다. 동일 순서 규칙을 공유하거나 명시적으로 재사용한다.
- 입력 바이트/SourceRecord/temporal/supersession 및 기존 immutable baseline은 변경하지 않는다. manifest용 document_id 정렬을 corpus 정렬로 재사용하지 않는다.
- 최소 2건 이상의 `ReuseSource` 재처리 + ledger 정비 테스트를 추가해, 같은 최종 입력의 일반/replay corpus SHA가 일치하고 다음 일반 실행이 no-change로 안정됨을 검증한다. 비선택 보존 입력을 append하는 scoped process에서도 동일 정렬 규칙을 확인한다.
- FIX_04의 허용 OCR 실패 보존과 threshold 차단 회귀를 유지한다.

## Reviewer 검증 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1389 passed, 10 warnings in 52.89s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

git diff --check
통과
```

추가 재현은 `/tmp/cardrag019-review5.dDVOVL/test_review5.py`에서 기존 corpus fixture/mock provider만 사용했다.

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src uv run --no-sync pytest /tmp/cardrag019-review5.dDVOVL/test_review5.py -q -s -o asyncio_mode=auto
REPRO: replay with 4 unchanged documents and ledger cleanup hashes manifest order; unchanged normal run republishes with original canonical hash
1 passed in 1.94s
```

이 pass는 결함 동작이 관측됐다는 뜻이며 승인 테스트가 아니다. 운영 state/serving/카드사 origin에 접근하거나 제품 코드·배포·timer를 변경하지 않았다.

## Executor 인계

본 문서가 최신 미보고 correction이다. 기존 PLAN/REPORT/FIX는 덮어쓰지 않고 구현·검증·편차와 운영 미검증 항목을 `FIX_05_REPORT.md`에 작성한다. 수정 범위는 canonical 순서 일치와 관련 회귀 검증에 집중한다. 전용/관련 및 Worker 전체 테스트·ruff를 실행하고 017–018 공존 변경을 보존한다. 이후 운영은 기존 사용자 수동 우리 실행 → 나머지 7사 → 백업/degraded 확인 → timer 조정 순서를 유지하며 이번 리뷰에서 운영 적용 완료로 판정하지 않는다.
