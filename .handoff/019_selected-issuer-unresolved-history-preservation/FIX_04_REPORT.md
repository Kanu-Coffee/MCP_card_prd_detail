# 019 FIX_04_REPORT — FIX_04 이행 및 실행 경계 검증 완료 보고

2026-10-19 Executor: Antigravity.
대상: `.handoff/019_selected-issuer-unresolved-history-preservation/FIX_04.md`

---

## 1. 개요 및 변경 요약

Reviewer Codex가 지적한 두 가지 P1 실행 경계 결함(CLI transport 선택 오류 및 serving 조회 검증 우회 취약점, replay corpus 재계산 시 OCR 실패 문서의 SourceRecord/temporal 속성 누락으로 인한 배치 중단)을 해결하고, 실체적 CLI 및 전체 수집-처리 라이프사이클 테스트를 구축하여 검증을 완료했다.

### 주요 수정 사항

1. **[P1-1] CLI Publication Transport 선택 및 실제 서빙 검증 강화 (`cli.py`, `unresolved_remediation.py`)**:
   - `apps/cardrag-worker/src/cardrag_worker/cli.py`:
     - 존재하지 않는 `settings.local_serving` 검사와 하드코딩된 `candidate-009` 채널을 제거하고, 기존 CLI 파이프라인(`cli.py:1511`)과 동일하게 `publication_transport`, `serving_dir`, `channel` 설정을 읽어 실제 transport(`LocalServingTransport` 또는 `WebDAVClient`)를 생성하도록 수정.
     - Transport 인스턴스화 및 서빙 포인터 검증(`validated_current_generation()`)을 견고한 `try...except` 구조로 감싸고, `finally`에서 `transport.close()` 리소스를 정상 해제.
     - `--apply` 실행 시 `validated_current_generation()`이 `None`을 반환하거나 예외(네트워크 단절, 파싱 실패 등)를 던지면 `latest-ready` DB fallback 없이 즉시 JSON 에러를 출력하고 exit code 1로 종료.
   - `apps/cardrag-worker/src/cardrag_worker/unresolved_remediation.py`:
     - `require_verified_serving=True`일 때 `current_serving_generation_id`가 필수이며 대상 `generation_id`와 정확히 일치해야 함을 보장.
     - 온디스크 실제 등록 seal(`runs/<run_id>/sealed/publish.json`) 존재성, 비심볼릭링크, 유효한 JSON, `GenerationManifest` 존재 및 generation_id/corpus_sha256/contract_sha256/serving_sha256 일치 여부를 엄격히 검증하여 미등록/위조 seal 적용을 원천 차단.

2. **[P1-2] Replay Corpus 재계산 시 OCR 실패 문서 속성 보존 (`pipeline.py`, `issuer_scope.py`, `partial_execution.py`)**:
   - `apps/cardrag-worker/src/cardrag_worker/pipeline.py`:
     - `_OCRFailedDocument` 데이터클래스에 `source: SourceRecord | None = None`, `temporal_status: TemporalStatusV5 = "current"`, `supersedes_document_id: str | None = None` 필드를 추가.
     - 캐시 재사용 및 신규 OCR 실패 핸들러에서 획득 문서의 원본 `SourceRecord`와 temporal 메타데이터를 누락 없이 전달.
   - `apps/cardrag-worker/src/cardrag_worker/issuer_scope.py`:
     - `preserve_inputs`에서 `_OCRFailedDocument` 생성 시 `source=record`, `temporal_status="current"`를 설정.
   - `apps/cardrag-worker/src/cardrag_worker/partial_execution.py`:
     - 임의 결합(`(*processed, *failed)`)으로 인해 발생하던 순서 왜곡 및 `AttributeError: '_OCRFailedDocument' object has no attribute 'source'`를 완전히 제거.
     - `source.documents.items()` 순회 시점에 canonical acquisition 순서를 유지하며 `_AcquiredDocument` 목록(`replayed_acquired`)을 직접 구성하고, 실패한 문서도 원본 `SourceRecord`를 유지한 채 corpus identity 재계산에 반영.

3. **테스트 및 검증 강화 (`test_019_unresolved_history.py`)**:
   - `test_collect_and_process_95_percent_ocr_failure_with_ledger_remediation`:
     - 실제 20건 문서에 대해 1건 OCR 실패(19/20 = 95% 성공) 발생 시, 과거 unresolved ledger 정비가 수반되어도 배치 중단 없이 `succeeded` 완료 검증.
     - 매니페스트 내 20건 문서 등록, 19건 OCR 성공, 1건 `ocr_failure` 기록, DB unresolved revision remediated 상태 검증.
     - 임계치 미달(18/20 = 90% < 95%) 및 역사 문서(historical) 실패 시 `OCRDocumentFailuresError`로 즉시 차단됨을 검증.
   - CLI Transport 경계 테스트:
     - `test_cli_transport_candidate_channel`: `CARDRAG_CHANNEL=candidate-001` 정상 반영 및 미게시 채널 지정 시 상태 DB 변경 없이 차단 검증.
     - `test_cli_transport_pointer_missing_corrupted_or_network_failure`: 포인터 누락, 포인터 JSON 손상, WebDAV 네트워크 예외 상황에서 모두 exit code 1과 에러 메시지를 출력하며 DB가 불변임을 검증.
     - `test_cli_rejects_missing_manifest_or_registered_disk_seal`: 온디스크 등록 seal 부재 및 manifest 누락 시 영구 정비 적용 차단 및 DB 불변 검증.

---

## 2. 검증 결과

### 1) 단위 및 통합 테스트 (019 테스트 19건 전체 통과)
```bash
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/test_019_unresolved_history.py -v
```
결과:
```text
============================== 19 passed in 4.59s ==============================
```

### 2) 전체 Worker 테스트 스위트 (1,389건 전체 통과)
```bash
export PATH="$HOME/.local/bin:$PATH" && uv run --no-sync pytest apps/cardrag-worker/tests/ -q
```
결과:
```text
1389 passed, 10 warnings in 52.29s
```

### 3) 린트 및 코드 포맷 검사
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

---

## 3. 운영자 가이드 및 사용법 정정

FIX_03_REPORT에서 언급된 실행 명령 예시의 오타를 바로잡는다:

1. **단일 카드사(우리은행/우리카드) 수동 수집 및 정비**:
   - 올바른 CLI 옵션은 `--issuer`이다:
     ```bash
     cardrag-worker run --issuer woori
     ```
   - (참고: `--selected-issuers`는 내부 파이프라인 옵션이며 CLI 인자로는 `--issuer <name>`을 사용한다.)

2. **Unresolved History 수동 정비 CLI 명령**:
   - Dry-run 진단:
     ```bash
     cardrag-worker remediate-unresolved-history --dry-run
     ```
   - 실제 적용 (서빙 중인 current generation 및 등록 seal 자동 검증):
     ```bash
     cardrag-worker remediate-unresolved-history --apply
     ```
   - 특정 등록 seal 지정 적용 (해당 seal이 실제 current serving generation과 일치해야 함):
     ```bash
     cardrag-worker remediate-unresolved-history --seal-path /path/to/runs/<run_id>/sealed/publish.json --apply
     ```

---

## 4. 결론 및 잔여 위험 검토

- 배포, 실 카드사 네트워크 요청, 타이머 활성화는 수행하지 않았으며, 모든 테스트는 로컬 mock 및 격리된 임시 경로에서 수행되었다.
- 017-018 공존 변경, CAS 불변성, 공유 스토리지 무결성, 비선택 카드사 서빙 보존 계약이 온전히 유지되었다.
- 본 리포트 생성을 끝으로 FIX_04에서 제기된 모든 P1 결함 및 요구사항이 해결되었다.
