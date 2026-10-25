# 017 Executor FIX_01_REPORT

작성: 2026-10-19, Codex Executor (Antigravity). Reviewer의 `FIX_01.md` 지적 사항에 따라 필수 코드 결함 4건(A1~A4), 필수 운영 문서 보완 2건(B1~B2), 그리고 권장 검증 테스트 6건(C1~C7)을 구현하고 회귀 검증을 완료했다.

---

## 1. 구현 요약 및 변경 내역

### A. 필수 코드 결함 수정 (Must Fix)

1. **A1: `issuer_metrics.py`의 `used()` 쿼리 의미론 오류 수정**
   - **원인:** `WHERE (issuer=? OR host=?)` 조건으로 인해 서로 다른 카드사가 동일 호스트를 이용하거나 다중 호스트를 이용할 때 요청/바이트가 합산되어 예산이 조기 소진되는 문제.
   - **수정:** issuer별 합계(`by_issuer`)와 host별 합계(`by_host`)를 독립적으로 집계한 후 `max(by_issuer, by_host)`를 반환하도록 분리.
   - **파일:** [`apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py#L59-L70)

2. **A2: `issuer_metrics.py` / `rate_limit.py` 단일 Timeout 즉시 쿨다운 차단 수정**
   - **원인:** `note_transport_failure`에서 일시적인 1회 timeout/네트워크 에러 발생 시 즉시 300초 cooldown을 영속화하여, 파이프라인의 `retry_bounded_async` 재시도 시 `issuer_origin_cooldown` 에러로 전체 재시도가 즉각 중단(abort)되던 결함.
   - **수정:** `note_transport_failure`에서 `timeouts` / `connection_errors` 메트릭 increment는 유지하되, 즉각적인 `observer.cooldown(...)` 호출을 제거. (403/429 발생 시의 쿨다운은 `after()`에서 정상 유지)
   - **파일:** [`apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py#L143-L149)

3. **A3: `pipeline.py` Negative Cache 비DRM 페이로드 런타임 `TypeError` 방어**
   - **원인:** `origin_policy.negative()`에 롯데 404 캐시(`{"status": 404}`) 등 비DRM 항목이 존재할 때 `ProtectedDocumentError(**negative)`를 호출하면 생성자 인자 불일치(`magic`, `sha256`, `size_bytes`)로 `TypeError`가 발생하던 위험.
   - **수정:** `negative` 페이로드에 `"magic"` 키가 존재하는 경우에만 `ProtectedDocumentError`를 발생시키고, 404 등 기타 네거티브는 `IssuerCollectionError("issuer_source_invalid", "OriginNegative")`로 안전하게 변환하여 다운로드 건너뜀 처리.
   - **파일:** [`apps/cardrag-worker/src/cardrag_worker/pipeline.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py#L3897-L3903)

4. **A4: `origin_policy.py`의 `inherit()` DB commit 추가**
   - **원인:** 상품명 변경 시 기존 감사 스케줄 상속(`INSERT OR IGNORE INTO origin_audit`) 후 `commit()`이 누락되어 비정상 종료 시 상속 상태가 유실되던 결함.
   - **수정:** `inherit()` 실행 후 `self.state.connection.commit()`을 호출하도록 보완.
   - **파일:** [`apps/cardrag-worker/src/cardrag_worker/origin_policy.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/origin_policy.py#L64-L71)

---

### B. 필수 운영 문서 수정 (Must Fix)

1. **B1: `docs/OPERATIONS.md` 롯데 ZIP 첨부 장애 현황 반영**
   - 롯데카드 공시 목록 첫 100건 중 **DOCID 1893(`2023-A00026_20261012_10004.zip`)**과 같이 신규 ZIP 첨부가 존재하여 PDF 전용 파서에서 `Lotte disclosure attachment filename is invalid`로 거부됨을 명시.
   - archive-aware 첨부 처리 모델이 구현되기 전까지 롯데카드 complete collection의 운영 인수가 보류 상태이며, 기존 검증된 stable 문서 보존 및 fail-safe carryover 정책이 유지됨을 기술.
   - **파일:** [`docs/OPERATIONS.md`](file:///home/lee/projects/MCP_card_prd_detail/docs/OPERATIONS.md#L433-L444)

2. **B2: `docs/OPERATIONS.md` 상단 환경변수 요약 표 보완**
   - 상단 한도 및 설정 표에 신규 환경변수 4종(`ISSUER_EXECUTION_MODE`, `ISSUER_REQUEST_BUDGET`, `ISSUER_BYTE_BUDGET`, `ISSUER_PROXY_URL`) 추가.
   - **파일:** [`docs/OPERATIONS.md`](file:///home/lee/projects/MCP_card_prd_detail/docs/OPERATIONS.md#L254-L260)

---

### C. 신규 검증 테스트 추가 ([`test_017_scope.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/tests/test_017_scope.py))

1. `test_budget_query_isolates_issuers_on_shared_host`: Issuer A(Host 1 5000, Host 2 4000)와 Issuer B(Host 1 3000) 동시 사용 시 교차 합산(12000) 없이 독립적으로 9000, 8000으로 계산되는지 검증 (A1 검증).
2. `test_transient_transport_error_does_not_abort_retries`: 1차 시도에서 `ConnectTimeout` 발생 후 쿨다운으로 차단되지 않고 2차 시도에서 정상 성공하며 `timeouts` 메트릭이 정확히 1로 집계되는지 검증 (A2 검증).
3. `test_origin_policy_inherit_commits_durably`: SQLite 연결을 닫고 다시 열었을 때 `inherit()` 결과가 영속적으로 유지되는지 검증 (A4 검증).
4. `test_origin_policy_overdue_and_verified_304`: 95일 경과 캐시의 `overdue == 1` 집계 및 304 조건부 확인 시 `overdue == 0` 복구 및 `result='not_modified'` 저장 검증 (C4, C5 검증).
5. `test_woori_1000_limit_raises_markup_changed`: 우리카드 1,000건 포화 응답 시 `IssuerMarkupChanged("Woori main listing requires pagination")` 예외 발생 검증 (C3 검증).
6. `test_origin_policy_negative_non_drm_payload`: 404 등 비DRM 네거티브 캐시 페이로드가 정상 조회되고 `"magic"` 키가 없어 `TypeError` 크래시를 유발하지 않는지 검증 (A3 검증).

---

## 2. 검증 명령 및 결과

- **Worker 테스트 스위트:**
  - `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -v`: **22 passed** (3.30초).
  - `uv run --no-sync pytest apps/cardrag-worker/tests/ -q --tb=short`: **1,335 passed, 10 warnings** (44.74초).
- **전체 워크스페이스 테스트:**
  - `uv run --no-sync pytest -x -q --tb=short`: **2,572 passed, 10 warnings** (75.14초, 7개 신규 테스트 반영 완료).
- **정적 분석 및 포맷팅:**
  - `uv run --no-sync ruff check apps/cardrag-worker/`: **All checks passed!**
  - `uv run --no-sync ruff format --check apps/cardrag-worker/`: **123 files already formatted.**
  - `git diff --check`: 공백/줄바꿈 에러 없음.
- **보안 검증 (Gitleaks):**
  - `gitleaks detect --no-git -v --source apps/cardrag-worker/src/cardrag_worker/`: **no leaks found**.
  - `gitleaks detect --no-git -v --source .handoff/017_issuer-incremental-acquisition-review/`: **no leaks found**.

---

## 3. 남은 리스크 및 운영 반영 전 확인 사항

1. **롯데카드 ZIP 첨부 파일 이슈:**
   - DOCID 1893의 ZIP 첨부 문제는 파서 예외로 안전하게 거부 및 carryover되나, 롯데카드의 정상 complete 수집을 위해서는 후속 task에서 ZIP 압축 해제 및 복수 파일 식별 지원이 필요함.
2. **배포 환경변수 설정:**
   - 코드 기본값은 2,160시간(90일)이나, 기존 서버의 비공개 `.env`에 `CARDRAG_PDF_CACHE_REFRESH_HOURS=168`이 명시되어 있는 경우 이를 2,160으로 전환해야 분산 90일 감사가 적용됨.
   - 프록시 주소(`CARDRAG_ISSUER_PROXY_URL`)는 컨테이너에서 도달 가능한 브릿지 IP/포트로 설정해야 함.
