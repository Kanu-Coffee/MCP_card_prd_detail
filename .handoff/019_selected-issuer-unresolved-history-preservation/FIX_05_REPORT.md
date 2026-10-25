# 019 FIX_05_REPORT — Replay Corpus Canonical 순서 일치 및 재실행 안정화 완료 보고

2026-10-19 Executor: Antigravity.
대상: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_05.md`

---

## 1. 개요 및 변경 요약

Reviewer Codex가 `FIX_05.md`에서 지적한 P2 결함(ReuseSource replay가 manifest의 `document_id` 순서로 corpus identity를 재계산하여, 동일 입력의 일반 실행 대비 corpus SHA가 불일치하고 다음 일반 실행에서 불필요한 재게시가 발생하는 문제)을 해결했다.

### 주요 수정 사항

1. **Canonical Acquired Document 정렬 키 명시화 및 공유 (`pipeline.py`)**:
   - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
     - 일반 실행의 정렬 계약과 동일한 정렬 함수 `canonical_acquired_document_key`를 명시적으로 정의:
       ```python
       def canonical_acquired_document_key(item: _AcquiredDocument) -> tuple[str, str, Any, str, str]:
           return (
               item.source.issuer,
               item.source.product_code,
               item.source.effective_date,
               item.source.source_version,
               item.pdf.sha256,
           )
       ```
     - `pipeline.py` 내의 수집 barrier 정렬(라인 4658 및 4680)에서 인라인 lambda를 `canonical_acquired_document_key`로 대체.
     - `_v5_corpus_identity_payload` 진입부에서 전달받은 `acquired` 시퀀스를 `canonical_acquired_document_key` 기준으로 재정렬(`ordered_acquired = tuple(sorted(acquired, key=canonical_acquired_document_key))`)하여, `"documents"`와 `"revision_history.materialized_revisions"`가 어떤 호출 경로에서도 항상 일반 실행과 동일한 canonical 순서로 해시되도록 보장.

2. **Replay 및 Scoped Process 정렬 보정 (`partial_execution.py`)**:
   - `apps/cardrag-worker/src/cardrag_worker/partial_execution.py`:
     - `canonical_acquired_document_key`를 임포트.
     - `replayed_acquired.extend(preserved_acquired)` 수행 직후 `replayed_acquired.sort(key=canonical_acquired_document_key)`를 호출하여, ReuseSource 순회 항목(`source.documents`)과 비선택 보존 항목(`preserved_acquired`)이 결합된 전체 획득 목록을 canonical 순서로 일괄 정렬.

3. **회귀 검증 테스트 추가 (`test_019_unresolved_history.py`)**:
   - `test_reprocess_hash_matches_canonical_normal_run_hash_and_yields_no_change`:
     - 4건의 문서로 구성된 baseline을 일반 실행으로 게시.
     - Baseline seal에 과거 unresolved entry를 인위적으로 주입.
     - `ReuseSource` 기반의 PDF reuse 재처리(`ExecutionPlan(frozenset({"pdf"}), baseline.run_id)`)를 실행하여 ledger 정비(1건 → 0건).
     - 재처리 결과의 manifest corpus SHA가 baseline의 manifest corpus SHA와 완전히 일치함을 검증 (`published.manifest.corpus_sha256 == baseline.manifest.corpus_sha256`).
     - 입력을 변경하지 않은 후속 일반 실행이 `no_change` 상태로 안정화되며 corpus SHA 드리프트가 발생하지 않음을 검증.

---

## 2. 검증 결과

### 1) 단위 및 통합 테스트 (019 테스트 20건 전체 통과)
```bash
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/test_019_unresolved_history.py -v
```
결과:
```text
============================== 20 passed in 5.13s ==============================
```

### 2) 전체 Worker 테스트 스위트 (1,390건 전체 통과)
```bash
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/ -q
```
결과:
```text
1390 passed, 10 warnings in 52.95s
```

### 3) 린트, 코드 포맷 및 diff 무결성 검사
```bash
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync ruff check apps/cardrag-worker/
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync ruff format --check apps/cardrag-worker/
git diff --check
```
결과:
```text
All checks passed!
125 files already formatted
(git diff --check 통과)
```

### 4) Reviewer 재현 스크립트 대조
Reviewer의 `/tmp/cardrag019-review5.dDVOVL/test_review5.py`에서 기존 불일치 assertion(`assert published.manifest.corpus_sha256 != baseline.manifest.corpus_sha256`)이 실패하고 실제 두 해시가 동일함(`fac846939a54e7ff8c9bf9a45e7517a5bdfc5674bcd6236a7c8358284dc85e16` == `fac846939a54e7ff8c9bf9a45e7517a5bdfc5674bcd6236a7c8358284dc85e16`)을 확인.

---

## 3. 운영 및 위험 관리 준수 사항

- 실 카드사 origin 접근, 실제 네트워크 호출, 프로덕션 배포, 타이머 활성화는 일체 수행하지 않았다.
- 017–018 브랜치 공존 코드, CAS 객체 불변성, 공유 서빙 스토리지 보호, 비선택 카드사 서빙 보존 계약이 온전히 유지되었다.
- 본 리포트 생성을 통해 FIX_05에서 요청된 P2 정렬 계약 결함 해결을 완료했다.
