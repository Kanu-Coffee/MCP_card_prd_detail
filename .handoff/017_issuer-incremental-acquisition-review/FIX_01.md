# 017 Reviewer FIX_01 — 코드 결함 수정 및 운영 문서 보완

작성: 2026-10-19, Reviewer (Antigravity). PLAN.md·ANALYSIS.md·REPORT.md, Git diff(21개 파일 +1,229/−223행), 신규 7개 파일(1,534행), 테스트 전체 suite(2,566 passed) 재실행, ruff/gitleaks 검증, 코드 심층 리뷰(3개 병렬 서브에이전트 + 직접 검토)를 교차 수행했다.

**전체 판정: 조건부 승인.** 핵심 아키텍처(선택 실행 시 비선택 0-call 보존, KST 순회 슬롯, collect/process 분리, 불변 OCR 큐 분할, 프록시 호스트 제한)는 PLAN.md와 높은 충실도로 구현되었고 회귀 시험 2,566건을 통과했다. 그러나 코드 심층 리뷰에서 **운영 시 런타임 장애를 유발하는 코드 결함 3건**과 **문서 누락 2건**을 발견했다. 이를 수정한 후 배포를 진행해야 한다.

---

## A. 필수 코드 수정 (Must Fix — Runtime Defects)

### A1. `issuer_metrics.py` — `used()` 예산 쿼리 의미론 오류 🔴

**위치:** [`issuer_metrics.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py#L59-L65)

**결함:** `WHERE (issuer=? OR host=?)` 조건이 issuer 전체 요청량과 host 전체 요청량을 **합산**한다. 서로 다른 issuer가 같은 host를 공유하는 경우(현재는 1:1이지만 향후 변경 가능), 또는 같은 issuer가 여러 host를 사용하는 경우 예산이 교차 간섭한다.

**예시:** Issuer A가 Host 1에 5,000건, Host 2에 4,000건 요청하고, Issuer B가 Host 1에 3,000건 요청한 경우:
- Issuer A / Host 1에 대한 `used()` = 5,000 + 4,000 + 3,000 = **12,000 ≥ 10,000** → 예산 소진으로 오판.
- 실제로는 Issuer A 총 9,000건, Host 1 총 8,000건이므로 둘 다 예산 내.

**조치:** issuer 합계와 host 합계를 **별도 쿼리**하고 `max(issuer_total, host_total)`로 비교하라. 보정 후 기존 테스트가 새 쿼리와 호환되는지 확인하라.

```python
# 현재 (결함)
def used(self, issuer, host, metric):
    return self.state.connection.execute(
        "SELECT coalesce(sum(value),0) FROM issuer_http_usage WHERE run_id=? "
        "AND (issuer=? OR host=?) AND metric=?",
        (self.run_id, issuer, host, metric),
    ).fetchone()[0]

# 수정안
def used(self, issuer, host, metric):
    by_issuer = self.state.connection.execute(
        "SELECT coalesce(sum(value),0) FROM issuer_http_usage WHERE run_id=? "
        "AND issuer=? AND metric=?",
        (self.run_id, issuer, metric),
    ).fetchone()[0]
    by_host = self.state.connection.execute(
        "SELECT coalesce(sum(value),0) FROM issuer_http_usage WHERE run_id=? "
        "AND host=? AND metric=?",
        (self.run_id, host, metric),
    ).fetchone()[0]
    return max(by_issuer, by_host)
```

### A2. `issuer_metrics.py` / `rate_limit.py` — 단일 일시적 timeout이 즉시 issuer 전체를 5분 차단 🔴

**위치:** [`issuer_metrics.py:130-143`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/issuer_metrics.py#L130-L143), [`rate_limit.py`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/rate_limit.py)

**결함:** `note_transport_failure(exc)`가 **모든** `httpx.RequestError`(일시적 timeout 포함)에 대해 즉시 300초 cooldown을 설정한다. `retry_bounded_async`가 1초 후 재시도하면 `before()`에서 cooldown을 확인하고 `IssuerCollectionError("issuer_origin_cooldown")`을 발생시키며, `pipeline.py`의 `non_retryable_predicate`에 `"issuer_origin_cooldown"`이 포함되어 있어 **재시도가 즉시 중단**된다.

결과: 단일 네트워크 glitch가 해당 issuer의 모든 동시 다운로드와 discovery를 5분간 차단하며, PLAN §A.3의 "제한된 재시도" 의도와 모순된다.

**조치 (택1):**
1. `note_transport_failure`에서 cooldown 설정을 **제거**하고, cooldown은 403/429(`after()` 내)와 terminal retry failure 후에만 적용하라.
2. 또는 `non_retryable_predicate`에서 `"issuer_origin_cooldown"`을 제거하여 cooldown 중에도 기존 retry 한도 내에서 재시도를 허용하라.

### A3. `pipeline.py:3900` / `origin_policy.py` — Negative cache 타입 불일치로 런타임 TypeError 🔴

**위치:** [`pipeline.py:3900`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/pipeline.py#L3897-L3900), [`origin_policy.py:91`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/origin_policy.py#L91)

**결함:** 롯데 어댑터([`lotte.py:267`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/issuers/lotte.py#L266-L268))가 404 관측을 `{"status": 404}`로 저장하지만, `pipeline.py`의 PDF 다운로드 루프에서는 **모든** negative를 DRM으로 간주하고 `raise ProtectedDocumentError(**negative)`를 호출한다. `ProtectedDocumentError.__init__`은 `magic`, `sha256`, `size_bytes` 키워드만 받으므로 `{"status": 404}`에 대해 `TypeError`가 발생한다.

현재는 롯데 어댑터가 자체적으로 negative cache를 확인하고 `continue`하여 pipeline 다운로드 루프까지 도달하지 않지만, 아래 경우에 크래시한다:
- `pdf_cache_force_revalidate=True` 설정 시 (어댑터의 negative 체크를 건너뜀)
- 롯데 이외의 adapter에서 `remember_negative`를 DRM이 아닌 형식으로 사용할 경우

**조치:** `pipeline.py:3899`에서 negative payload의 타입을 구분하라:
```python
negative = self.origin_policy.negative(identity.source_id, now=datetime.now(UTC))
if negative is not None:
    if "magic" in negative:
        raise ProtectedDocumentError(**negative)
    # 404 or other non-DRM negative: skip with warning, not crash.
    raise IssuerCollectionError("issuer_origin_negative_cached", "OriginNegative")
```

### A4. `origin_policy.py` — `inherit()`에서 DB commit 누락

**위치:** [`origin_policy.py:64-69`](file:///home/lee/projects/MCP_card_prd_detail/apps/cardrag-worker/src/cardrag_worker/origin_policy.py#L64-L69)

**결함:** `INSERT OR IGNORE INTO origin_audit` 후 `self.state.connection.commit()`이 없다. 상품명 변경 시 audit schedule 상속이 다음 commit boundary까지 영속화되지 않으며, 중간 크래시 시 유실된다.

**조치:** `inherit()` 끝에 `self.state.connection.commit()`을 추가하라.

---

## B. 필수 문서 수정 (Must Fix — Documentation)

### B1. `docs/OPERATIONS.md` — 롯데 ZIP 첨부 장애 현황 누락

**발견:** REPORT §4에서 실측 확인된 롯데 DOCID 1893의 ZIP 첨부(`2023-A00026_20261012_10004.zip`)에 의한 `Lotte disclosure attachment filename is invalid` 파서 예외가 OPERATIONS.md에 기술되어 있지 않다. 현재 문서(행 ~433)는 "네트워크/egress 문제 해결 시 자동 재개"라고만 안내하여, 운영자가 프록시만 활성화하면 롯데 수집이 정상화된다고 오인할 수 있다.

**조치:** 롯데 장애 항목에 ZIP 첨부 현황, archive-aware 파서 미구현으로 인한 보류 상태, issuer failure/carry 자동 적용을 명시하라.

### B2. `docs/OPERATIONS.md` — 상단 설정값 요약 표에 신규 환경변수 4종 누락

**발견:** 본문에는 `CARDRAG_ISSUER_EXECUTION_MODE`, `CARDRAG_ISSUER_REQUEST_BUDGET`, `CARDRAG_ISSUER_BYTE_BUDGET`, `CARDRAG_ISSUER_PROXY_URL`이 설명되어 있으나, 상단 **설정 요약 표**에 누락되어 있다.

**조치:** 요약 표에 4개 환경변수, 기본값, 설명을 추가하라.

---

## C. 테스트 보충 권장 (Should Fix)

### C1. 프록시 단절 시 직접 연결 폴백 차단 검증
`_IssuerProxyTransport`에서 `ConnectError`를 주입하고, 파이프라인이 해당 issuer를 실패/carry로 격리하며 직접 연결을 시도하지 않음을 검증하라.

### C2. 스트리밍 중 바이트 예산 초과 차단 검증
`_CountedStream`이 `byte_limit` 초과 시 `IssuerCollectionError`를 발생시키고 파이프라인이 deferred로 격리하는 흐름을 검증하라.

### C3. 우리카드 1,000건 한도 에러 및 DetailInventory 검증
`woori.py`의 1,000건 도달 시 `IssuerMarkupChanged` 예외와 DetailInventory 캐시 재사용을 검증하라.

### C4. 304 Not Modified 시 origin audit 기한 갱신 검증
304 응답 주입 시 `origin_audit` 테이블의 `next_audit_at`이 실제로 갱신되는지 직접 검증하라.

### C5. Overdue 양수 발생 검증
`origin_checked_at`을 91일 이전으로 설정하여 `report(now=now)["overdue"]`가 양수임을 확인하라.

### C6. A1 수정 후 예산 교차 간섭 검증 테스트 추가
2개 issuer가 같은 host를 공유하는 시나리오에서 예산이 정확히 독립 집계되는지 확인하라.

### C7. A3 수정 후 404 negative cache pipeline 처리 테스트 추가
`origin_negative`에 `{"status": 404}`를 저장한 후 pipeline PDF 루프에서 `TypeError` 없이 정상 처리되는지 확인하라.

---

## D. 개선 권장 (Nice to Have)

### D1. 날짜 경과 미완료 순회 처리 검증
`scope_session`의 `old.day != today` 분기를 테스트하라. 전날 미완료 slot → `interrupted` 기록 → 오늘자 새 issuer 배정.

### D2. 비선택 카드사 단종 평가 동결 검증
선택 실행 전후의 retirement ledger 동결을 직접 확인하라.

### D3. 순회 상태 트러블슈팅 가이드
`issuer_rotation_day` 조회 쿼리, `--all`/`--issuer` 수동 우회법을 OPERATIONS.md 트러블슈팅 섹션에 추가하라.

### D4. `--dry-run`과 scoped execution 호환
현재 `cli.py:1970`에서 `--dry-run`이 `--issuer`/`--round-robin`과 함께 사용될 때 `ValueError`로 거부된다. PLAN §G에 "dry-run은 cursor/원격 부작용 없이" 가능해야 한다고 명시되어 있으므로, rotation slot/cursor를 건드리지 않는 조건 하에 허용하라.

### D5. Issuer 시작 시간 분산 미구현
PLAN §A.4에 명시된 issuer 시작 시간 분산이 구현되지 않았다. 의도적 생략이면 REPORT에 명시하고, 필요 시 후속 task에서 구현하라.

### D6. DetailInventory 정리 메커니즘 부재
`issuer_detail_inventory` 테이블에 정리/prune 로직이 없어 단종 상품 행이 무한히 누적된다. 단기 운영에는 문제없으나 장기적으로 retention 정책을 추가하라.

---

## E. 코드 품질 관찰 (정보 참고용, 수정 불요)

1. **`issuer_http.py` 보안 설계 우수**: `trust_env=False`로 ambient proxy를 차단하고, `_IssuerProxyTransport`에서 scheme/host/port를 이중 검증하며, 예외 메시지에서 프록시 URL을 마스킹한다. SSRF 및 credential leak 방어가 충실하다.
2. **`issuer_scope.py` preserve_inputs**: `next(r for r in source.failed ...)` (행 142)에서 일치하는 항목이 없으면 `StopIteration`이 발생하나, 이 경로는 baseline의 무결성 검증을 전제하므로 실질적 위험은 낮다.
3. **`rotation.py` fail-closed 설계**: sealed publish.json이 존재하지만 run 상태가 terminal이 아닌 경우 `PartialExecutionError`로 강제 해결을 요구하는 방어적 설계가 적절하다.
4. **`DocumentRecord` 행 213**: `record.product_name`이 `name`과 `title` 양쪽에 전달되는 것은 보존 문서의 표시명 일관성을 위한 의도적 설계로 판단된다.
5. **신한 batch mobile token map 미구현**: PLAN §C에 명시된 카테고리별 세션 토큰 재사용은 구현되지 않았고, prefix 검색 fallback만 추가되었다. REPORT §3.4에서 세션 범위 미입증으로 의도적 생략을 보고했으므로 수용한다.

---

## F. 수행 순서 권고

| 우선순위 | 항목 | 영향 | 차단 여부 |
|---|---|---|---|
| 🔴 **1순위** | A1 (예산 쿼리), A2 (timeout→cooldown), A3 (negative TypeError), A4 (inherit commit) | 운영 시 issuer 조기 차단, 재시도 실패, 런타임 크래시 | **배포 차단** |
| 🔴 **2순위** | B1 (롯데 ZIP 문서화), B2 (환경변수 표) | 운영자 오인 | **배포 차단** |
| 🟡 **3순위** | C1~C7 (테스트 보충) | 장기 운영 안정성 | 순회 모드 활성화 전 완료 권장 |
| 🟢 **4순위** | D1~D6 (개선) | 코드 건전성 | 후속 task |
