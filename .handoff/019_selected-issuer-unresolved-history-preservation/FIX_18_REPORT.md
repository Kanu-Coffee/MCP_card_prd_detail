# 019 FIX_18_REPORT — 과거 OCR CAS scope 분리와 BC native pair 검증 회수 실행

작성: 2026-10-21, Executor OpenCode. `FIX_18.md`의 P2 두 건을 구현하고, LIVE_REVIEW_06의
실제 identity와 보존 증거를 RO로 재확인했다. 기존 handoff 문서는 보존했고 이 파일만
신규 산출물이다. 기준: HEAD `aaeb2c4` + 019 미커밋 작업 트리(FIX_01–17 변경 보존).
코드 변경: `apps/cardrag-worker/src/cardrag_worker/backup.py`,
`apps/cardrag-worker/src/cardrag_worker/cli.py`,
`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`, `docs/OPERATIONS.md`.
이번 턴에서 배포·원격 쓰기·원장 수정·provider/OCR/수집/배치/timer 실행은 하지 않았다.
원격 접근은 FIX_18 §2가 요구한 metadata GET(manifest/READY 4건)뿐이며 RO staging에만
로컬 cache 증거를 썼다(운영 볼륨/원장 무변경).

## 1. [P2] 과거 OCR CAS를 현재 필수로 넣는 오탐 — 해결

`scoped_backup_scope` Phase 2를 보정했다.

- 현재 seal의 PDF/OCR CAS ref, 현재 검증된 bundle, run별 ownership 기록은 기존처럼
  `paths`에 먼저 등록되므로 어떤 경우에도 보호된다. doc_map 태그 하나로 required가 되지 않는다.
- **과거 revision 태그**(doc_info의 PDF/OCR identity가 현재 seal과 다름)는 문서의 현재
  native metadata 유무와 무관하게 이번 selected scope의 필수 항목에서 제외한다. 이전
  구현은 `doc_has_bound_metadata`를 요구해 content/adopted 문서(정상적으로 문서 소유
  native metadata가 없음)에서 과거 CAS가 마지막 `paths.add`로 required/unbound가 됐다.
- 비-cache 객체(CAS)이면서 stale 태그가 아닌 경우: content/adopted는 문서 소유 native
  단위가 아니므로 shared-cache로 제외하고, native/unknown provenance는 보수적으로
  allowed에 남겨 unbound(완료 차단)로 유지한다(blanket 제외 금지).
- receipt·doc_map·doc_info·원격 객체는 삭제/수정하지 않는다. 숫자를 맞추기 위한 정리는 없다.
- 새 내부 카운터 `_last_excluded_stale_object`로 stale CAS 제외 수를 추적한다(CLI schema
  변경 없음).

### 1.1 실제 현대 3건·BC 1건 RO 대조 (LIVE_REVIEW_06 identity 재확인)

`cardrag-worker-state` 볼륨의 원장을 **읽기 전용 복사본**으로 만들어 보존 seal(2 run)과
함께 `/tmp/cardrag-fix18-ro2`에서 계산했다(원장/볼륨 무수정). 실제 값:

| issuer | document_id (앞12) | PDF (동일) | 현재 OCR | 과거 OCR (CAS) | 과거 receipt | doc_map |
|---|---|---|---|---|---|---|
| bc | doc_8356d2d284 | 631a15b88c | 263410c5df | ce35c0b7db | 유지 | 유지 |
| hyundai | doc_951343984f | c128e0d923 | 8b61a91058 | 757f24184f | 유지 | 유지 |
| hyundai | doc_23bd9e4e54 | 6c72fc1c0b | 6224e5a9bc | a7a981a2ba | 유지 | 유지 |
| hyundai | doc_a9ebcdb095 | 2890b8c536 | 6fe298e32b | aa02136a0b | 유지 | 유지 |

(현재 seal 기준 `ocr_cache_kind=content`, old/current PDF 동일 — LIVE_REVIEW_06과 일치.
현재 OCR CAS 4건은 seal ref로 allowed+expected에 바인딩됨을 확인.)

RO scope 변화(staging은 운영 cache 볼륨을 복사하지 않아 bound 절대값은 낮다; 비교는 delta 기준):

| issuer | before required | after required | 변화 | stale CAS allowed/expected | stale receipts | excluded_stale_object |
|---|---|---|---|---|---|---|
| hyundai | 1897 | 1894 | **-3** | 0 / 0 | 4건 유지 | 3 |
| bc | 483 | 482 | **-1** | 0 / 0 | 5건 중 해당 1건 유지(총 4 receipt 유지 확인) | 1 |

LIVE_REVIEW_06의 운영 수치(현대 1897/bound1894/unbound3, BC 483/bound478/unbound5)에
그대로 적용하면 현대 unbound 3→0, BC unbound 5→4(나머지 4는 §2 pair)가 된다. 과거 CAS
receipt/doc_map은 그대로 남아 있다(삭제 0).

## 2. [P2] BC native manifest/READY 4건 검증 회수 — 해결

정식 CLI 경로를 추가했다:

```text
backup recover-metadata --run <RUN_ID> [--issuer <code>...] [--key <64-hex>...]
```

동작(원격 GET-only, provider-free):

1. 보존된 seal의 선택 문서 중 native reuse key의 **unbound** canonical/variant pair만
   대상으로 한다(`--key`로 정확히 지정 가능).
2. 원격 manifest를 GET해 canonical 파싱, `reuse_key`, contract+source에서 파생된 key
   일치, 현재 seal의 PDF SHA/size/page·OCR SHA/size 일치를 확인한다. READY는 manifest로
   부터 파생한 canonical bytes와 **바이트 단위 일치**를 요구한다.
3. manifest/READY 각각 현 root의 `backup_receipts`와 SHA/size가 일치해야 한다
   (root-bound receipt 없으면 True 처리하지 않음).
4. 검증 성공 시에만 `cache/ocr/{key}/`(또는 다른 variant가 canonical을 점유한 경우
   `cache/ocr/{key}/variants/{ocr_sha}/`)에 manifest/READY를 복원한다. 기존 로컬 바이트가
   다르면 덮어쓰지 않고 conflict로 보류한다. 이후 `backup status --run`/`flush --run`의
   기존 scope 계약이 재바인딩한다.
5. 실패(원격 부재/손상/불일치/무receipt/로컬 충돌)는 partial로 보고하고 완료를 막는다.
   receipt·seal·pending·원격 객체는 쓰지 않으며, 재시도 시 이미 일치하는 본문/metadata를
   재업로드하지 않는다(첫 실행에서도 업로드 0).

### 2.1 실제 BC 두 key RO 검증 회수 결과

RO staging(`/tmp/cardrag-fix18-ro2`, 운영 볼륨 아님)에서 실제 원격 GET으로 검증:

| reuse key (앞12) | document_id (앞12) | manifest SHA (검증) | READY SHA (검증) | 결과 |
|---|---|---|---|---|
| bba8ef70412a | doc_8525c5b21c | f658cf459ea4efac… | a04f628d4cb823a6… | restored |
| e7ad55525a3f | doc_0d3baaab7c | a916db9cbeba87f2… | 352cef701fc30b29… | restored |

- LIVE_REVIEW_06의 예상 SHA와 모두 일치. 정확히 4개 파일(manifest/READY × 2 key)만 생성.
- 원장 receipt 수 16888 → 16888 (recovery는 receipt를 쓰지 않음), 원격 쓰기 0.
- 동일 명령 2회차: 두 pair 모두 `already_bound`, unbound_after 불변(멱등).
- staging unbound 240 → 236 (4개 pair 경로). 운영에서는 BC unbound 4 → 0에 해당.
  현대는 native unbound가 없어 recovery 대상 0(§1로 unbound 3 → 0).

## 3. 회귀 테스트

`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`에 11건 추가:

- 과거 CAS 제외(현대/BC 형태), pending row가 있어도 미전송·원장 행 유지,
  다른 현재 문서가 같은 CAS를 참조하면 required 유지, native/unknown provenance는
  blanket 제외 금지.
- recovery 성공(로컬 cache 복원·scope 재바인딩·완료·업로드 0·멱등),
  4개 부정 조합(manifest 바이트 손상/READY 손상/receipt 없음/원격 부재) 차단,
  다른 variant canonical cache 무덮어쓰기, CLI `--key` end-to-end(복구→status→flush).

## 4. 실행 명령과 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1766 passed, 10 warnings in 57.48s   # FIX_17 시점 1755 + 신규 11건

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
pass
```

## 5. 운영 절차 갱신

`docs/OPERATIONS.md` 선택 실행 백업 절에 추가:

```text
operations/worker-compose.sh run --rm --no-deps worker \
  backup recover-metadata --run <RUN_ID> --issuer <code> [--key <64-hex>...]
```

GET-only·root receipt·현재 seal identity 검증, receipt/seal/pending/원격 무수정,
일치 본문 재업로드 금지, 실패 시 unbound 유지로 완료 차단을 명시했다. 과거 OCR CAS가
현재 필수 scope에서 제외되며 receipt·doc_map·원격 객체는 이력으로 보존된다는 정책도
같은 절에 기록했다.

## 6. Deviation / 리스크

- RO staging에는 운영 `cache/ocr` 볼륨을 복사하지 않아 bound 절대값이 LIVE_REVIEW_06보다
  낮다(현대 976, BC 242). 따라서 보고된 것은 **delta**(현대 required -3, BC -1, recovery
  +4 bound)이며, 운영 live 수치는 Reviewer baseline에서 유도했다. 운영 원장/볼륨은
  읽기 전용으로만 접근했다.
- recovery 기본 동작은 선택 범위의 모든 unbound native key를 처리한다. FIX_18의 "정확히
  두 pair" 요구를 위해 `--key` 필터를 추가했고, RO 검증은 두 key로만 수행했다.
- 로컬 canonical cache가 다른 variant를 담고 있으면 recovery는 canonical을 덮어쓰지 않고
  `variants/{ocr_sha}/`에 검증본을 둔다. 이 경우 scope는 원격 receipt 기준 canonical로
  바인딩되며 로컬 canonical 바이트는 보존된다.
- §1의 stale 판정은 doc_info identity 비교에 의존한다(doc_info는 삭제하지 않음). doc_info가
  아예 없는 태그는 stale로 단정하지 않고 native/unknown은 보수적으로 unbound 유지한다.
- 신한/롯데 batch 완주·timer 재개·전체8사 인수는 이번 범위가 아니며 선언하지 않는다.

## 7. 다음 단계

Reviewer가 FIX_18 보고서와 회귀/RO 증거를 통과시키면, 기존 승인 범위에서 새 release와
검증 회수(운영 state의 실제 cache 복원 + 현대/BC scoped flush)를 준비한다. 확인 항목:
현대/BC `required=bound`, `in_scope_unbound=0`, `backup_scope_complete=true`, 원격 index
완료, B/A receipt·원격 객체 보존, 두 번째 재시도 멱등, serving 5537/우리 980 보존.