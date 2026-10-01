# 003 — 일배치 복구: 정당한 은퇴(retirement) 분류와 롤링 corpus 베이스라인

## Objective

2026-09-30 03:00 KST 일배치가 `corpus_diff_missing` 8건으로 실패했다. 원인은 데이터 손실이
아니라 **롯데카드 8개 상품의 실제 upstream 상장폐기(게시 중단)** 다. 현재 무결성 게이트는
"사라진 문서"와 "정당하게 은퇴한 문서"를 구분하지 못하고, 비교 기준선이 v1.0.28 시드
ledger에 영구 고정되어 있어 **앞으로 매일 03:00 배치가 같은 사유로 실패한다.**

목표는 무중단 운영(MCP 서빙 연속 + 일배치 유지)을 전제로 다음 두 가지를 동시에 도입해
무인 일배치를 복구하는 것이다.

1. **롤링 베이스라인**: 성공한 generation마다 기준선을 전진시켜, 매일의 비교가
   "직전 성공 corpus" 기준으로 이뤄지게 한다.
2. **정당한 은퇴 분류**: durable 증거·grace·상한·봉인을 충족한 lineage만 `retired_lineages`로
   분류하고, 나머지는 기존처럼 fail-closed로 유지해 무손실 불변식을 지킨다.

둘 중 하나만으로는 부족하다. 롤링 베이스라인만 있으면 8건은 여전히 "직전 corpus에서 사라진
문서"로 실패하고, 은퇴 분류만 있으면 기준선이 v1.0.28에 고정된 채 시간이 갈수록
gate의 의미가 훼손된다.

## Verified facts (Planner가 직접 측정한 증거)

- 실패 run `b40f8688e3394cd982a98148a6271d58`: 03:00:31 시작(UTC 18:00:31), 03:11:19 종료,
  exit 1, `reason_code=corpus_diff_missing`, `missing_count=8`, elapsed 677초.
  typed CLI payload·`corpus-diff.json` 사전 기록은 FIX_01 의도대로 동작했다.
- 사라진 8건은 전부 lotte이고 전부 seed 기준 `prior_current`다. product_code:
  `1151, 1835, 1836, 1862, 1863, 1864, 1865, 1885`.
- 발급사별 discovery record_count 비교(직전 성공 run `5f74295d…` vs 실패 run):
  lotte 529 → 521 (-8), bc 121 → 122 (+1), hana/hyundai/kb/samsung/shinhan/woori 변동 0,
  합계 5,054 → 5,047. lotte 신규 추가 0건.
  → 스크래핑/파서 회귀가 아니라 **해당 상품 게시 자체가 사라진 것**이다.
  기존 발급사 retention guard(`pipeline.py:2574`, ratio 0.5)는 98.5%라 정상 통과했다.
- 8건의 durable 상태는 온전하다: seed ledger `source_records` 포함, revision row 각 1건,
  PDF CAS 객체 디스크 존재, successor source row 존재(source_version 20260923/20260928,
  superseded_at=null) — 그러나 **successor 역시 현재 discovery에 없다.**
- 은폐 메커니즘: revision 확장은 "현재 discovery된 문서"만 순회한다
  (`pipeline.py:2981 for current_document in current_acquired`). lineage 전체가 discovery에서
  사라지면 `pdf_cache_lineage_history`를 호출할 current 문서가 없어 predecessor가
  historical로 materialize되지 못하고 corpus에서 증발한다.
- 기준선 고정: `audit-reports/state-seed/` 에는 v1.0.28 ledger 1건
  (`8ffdcd8e…`, `cardrag.state-seed-ledger.v2`, 5,192 docs, prior_cur 5,044 / prior_hist 148,
  run `bd9a4c51…`, gen `g-bd9a4c51…`)만 존재하며 갱신되지 않는다.
  `corpus_diff.py:225 missing = sorted(prior_all - all_acquired_ids)` 가 매일 같은 8건을
  재검출한다.
- `retired_lineages`는 `corpus_diff.py:130, 250` 에서 하드코딩 빈 튜플이다. FIX_02/FIX_03에서
  비상항목으로 이월했던 것이 **운영 블로커로 승격**됐다.
- 실패 run 잔여물: run row `failed`, run 디렉터리는 `discovery/`·`reports/`만 존재
  (`checkpoints/`, `sealed/` 없음). 미해결 revision ledger는 실패 시 기록되지 않는다(관측성 공백).
- 서비스 연속성 정상: `cardrag-stable-v1026-mcp-1`(image `53382bc3…`) Up healthy,
  `/health/ready` 200, 서빙 generation `g-5f74295db4b64f10837eba3e-a102228926a6`
  (2026-09-29T10:20:52Z publish). timer는 다음 발화 2026-10-01 03:00 KST.
- 직전 일배치들은 성공했다: 9/27 3건(2건은 corpus 동일), 9/28→9/29 1건(corpus `a1022289…`).
  즉 파이프라인 자체는 건강하고 게이트만 막혀 있다.
- 디스크: `/` 392G 중 171G 여유(55% 사용). 운영 볼륨 `cardrag-worker-v129-state` 43.8G,
  run 디렉터리 3개. generation당 ≈14GB(index 4.6GB + vectors 9.5GB).
- 운영 env: `CARDRAG_CHANNEL=stable`, `STABLE_PUBLICATION_APPROVED=true`,
  `REMOTE_GC_APPROVED=true`, `COLLECT_REMOTE_GARBAGE=true`, `OCR_CACHE_MODE=read-only`,
  `OCR_PROVIDER=local-paddleocr`, `EXTERNAL_OCR_ALLOWED=false`, `RETAIN_GENERATIONS=2`,
  worker image `…candidate@sha256:b42dfb61…`.
- FIX_03 정리로 v1.0.28 시드 원본 볼륨(`cardrag-worker-v114-candidate-state`)은 **삭제됐다.**
  따라서 새 후보 볼륨은 운영 볼륨(read-only) 또는 원격 generation에서 시드해야 한다.

## Constraints (무중단 운영 — 위반 시 즉시 중단하고 보고)

1. 서빙 중인 MCP(`cardrag-stable-v1026-mcp-1`, 볼륨 `cardrag-mcp-v129-candidate-state`)는
   작업 전 기간 healthy를 유지한다. 불필요한 restart/recreate 금지, 볼륨 삭제 절대 금지.
   호스트 nginx가 컨테이너 이름을 하드코딩하므로 이름을 바꾸지 않는다.
2. `cardrag-worker-v129-state`는 라이브 운영 상태다. 실험·검증에 쓰지 않는다.
   모든 검증은 별도 후보 볼륨 + candidate 채널로 수행한다.
3. `cardrag-worker.timer`는 기본적으로 enabled를 유지한다(실패는 11분 내 fail-closed이고
   서빙 generation을 건드리지 않는다). 다만 운영 볼륨을 다루는 작업 중에는
   03:00~05:00 KST 시간창을 피하고, 부득이 timer를 stop하면 시작·재활성화 시각을 기록한다.
   수동 worker 실행은 worker lock·디스크 충돌을 피하기 위해 timer 창 밖에서 수행한다.
4. 후보 볼륨 시드는 운영 볼륨을 **read-only**로 마운트해 수행하고, 시드 전후 운영 볼륨
   `worker-state.sqlite3` 의 device/inode·크기·SHA-256가 동일함을 입증한다.
   WAL/SHM 사이드카 부재, active writer 부재를 먼저 확인한다.
5. 디스크 하한: 작업 전·후 `df -h /` 를 기록하고, 여유 80GB 미만이면 신규 볼륨 생성/실행을
   중단한다. 후보 볼륨은 태스크 종료 시 정리한다(운영 볼륨·서빙 볼륨·롤백 이미지는 보존).
6. 무손실 불변식 유지: 은퇴는 durable 증거 + grace + 상한 + 봉인 + 로그를 모두 충족할 때만
   허용한다. transient 장애로 문서가 조용히 은퇴되는 경로를 만들지 않는다.
7. OCR 정책 유지: `CARDRAG_EXTERNAL_OCR_ALLOWED=false`, `local-paddleocr` / `PaddleOCR-VL-1.6`.
   외부 OCR 호출 0건을 계속 입증한다.
8. 롤백: 현재 worker digest `b42dfb61…` 와 env 백업을 보존한다. 신규 이미지는 새 digest여야 하며,
   롤백은 env digest 되돌리기로 완료돼야 한다(worker는 oneshot, MCP 무영향).
9. Handoff 이력은 append-only: `001_*`, `002_*` 문서를 수정하지 않는다.

## Scope

- 포함: 롤링 베이스라인 artifact, 은퇴 분류·grace ledger, corpus-diff 보고서 확장,
  실패 시 관측성, 단위·통합 테스트, 후보 볼륨 검증 2회, 운영 반영, 감독 하 일배치 1회,
  디스크/롤백 정리.
- 제외: MCP 도구 스펙 변경, v5 문서 계약 변경, OCR/임베딩 provider 변경, 발급사 파서 변경
  (파서 회귀 증거 없음), stable pointer 구조 변경, 컨테이너 명명 체계 개편,
  ledger source closure 5,187/5,203 확장(별도 추적 — 이번 실패와 무관함이 증거로 확인됨).

## Relevant files

- `apps/cardrag-worker/src/cardrag_worker/corpus_diff.py` (225 missing 계산, 130/250 retired_lineages, 262 raise)
- `apps/cardrag-worker/src/cardrag_worker/pipeline.py` (2968 ledger load, 2981 revision 확장, 3084 corpus-diff 호출, 2418 cleanup, 2574 retention guard)
- `apps/cardrag-worker/src/cardrag_worker/state_seed_v122.py` (ledger v2 스키마, `load_state_seed_ledger`, `acquisition.v1.json` 파싱 재사용)
- `apps/cardrag-worker/src/cardrag_worker/revision_history_v5.py` (lineage plan, unresolved 사유)
- `apps/cardrag-worker/src/cardrag_worker/cli.py` (typed payload 확장)
- `apps/cardrag-worker/src/cardrag_worker/state.py` (run/publish/pdf_cache 조회, baseline 저장 헬퍼)
- tests: `test_corpus_diff.py`, `test_pipeline.py`, `test_state_seed_v122.py`, `test_cli_settings_provider.py`
- 배포: `deploy/worker/compose*.yaml`, `/etc/cardrag/worker.env` (신규 knob 도입 시에만)

## Implementation guidance

### A. 롤링 corpus 베이스라인

1. 성공 봉인 직후 `audit-reports/corpus-baseline/<sha256>.json` 을 원자 기록한다
   (canonical JSON, tmp→rename, 0600, 파일명=내용 sha256 — state-seed/embedding-seed와 동일 패턴).
   내용: schema_version, generation_id, run_id, corpus_sha256, contract_sha256,
   current/historical doc id 목록, 발급사별 count, 은퇴 ledger 참조, counts.
2. 다음 run에서 최신 baseline을 로드하고 (a) 파일명 해시 일치, (b) 스키마,
   (c) state DB에 해당 run `succeeded` + publish `ready` 존재를 검증한다. 불일치 시 fail-closed.
3. 우선순위: 최신 유효 corpus-baseline → 없으면 state-seed ledger(시드 직후 첫 run) →
   둘 다 없으면 기존 무기준 동작.
4. seed ledger는 OCR 재사용·PDF identity 용도로 그대로 보존한다(삭제·재작성 금지).
   corpus-diff 비교에만 롤링 baseline을 쓴다.
5. 보존 개수는 `RETAIN_GENERATIONS` 와 정합(기본 2, 감사용 3 권장)으로 두고 기존 cleanup
   단계에서만 prune한다. 최신본은 절대 prune하지 않는다.

### B. 정당한 은퇴 분류

1. 은퇴 후보 = `prior_all - acquired_ids` (오늘의 `missing`).
2. 다음을 **모두** 충족할 때만 `retired_lineage`로 분류한다.
   - lineage key `(issuer, product_code, document_type)` 가 현재 canonical discovery snapshot에
     전혀 없고, `pdf_cache_source` 의 successor 중 현재 discovery된 것이 없다.
   - durable 증거가 온전하다: source row 존재, revision ≥1, CAS 객체 존재·해시 일치,
     OCR 산출물 해결 가능(seed ledger entry 또는 직전 봉인 generation).
   - 연속 결석이 grace를 충족한다: `RETIREMENT_GRACE_RUNS`(기본 2) 이상 연속 성공 run 결석
     또는 `RETIREMENT_GRACE_DAYS`(기본 3). durable `retirement_candidates` ledger에
     first_absent_run/at, consecutive_absences를 누적 기록한다.
   - 상한 이내: run당 은퇴 ≤ `RETIREMENT_MAX_PER_RUN`(기본 25) **그리고** baseline corpus의
     0.5% 이하. 초과 시 run fail-closed.
   - 같은 run에서 발급사 retention guard(`pipeline.py:2574`)를 통과했다.
3. 위 조건 미충족분은 기존처럼 `missing_unjustified` 로 hard fail 한다(행동 불변).
4. `retired_lineages` 에 증거 레코드를 채운다: document_id, source_id, lineage key,
   last_observed_run/at, first_absent_run/at, consecutive_absences, CAS sha, OCR sha,
   판정 입력값. grace 미충족 후보는 `retirement_candidates` 로 별도 노출해
   발효 전에 운영자가 볼 수 있게 한다.
5. 은퇴는 **삭제가 아니다.** PDF CAS·OCR·lineage row·remote 객체는 보존되고,
   보고서에 "history 복원 가능" 문구를 명시한다.
6. 정책 결정(Reviewer 권고: 1번) — Executor는 택일 후 근거를 REPORT에 남긴다.
   - 1안: 활성 corpus에서만 은퇴시키고 durable 산출물은 보존(corpus 크기 유한).
   - 2안: 마지막 known revision을 historical로 재materialize(corpus 단조 증가,
     폐기 상품도 historical로 조회 가능). 2안 선택 시 discovery에 없는 lineage도
     ledger/snapshot source record만으로 계획하되 메타데이터를 절대 발명하지 않는다.
7. 재게시 처리: 은퇴된 lineage가 discovery에 재등장하면 current로 재활성화하고
   은퇴 레코드를 `reinstated` 로 닫는다. 재취득을 막지 않는다.
8. run당 bounded WARNING 로그와 CLI 성공 payload·`performance.json` 에
   `retired_count`, `retirement_candidate_count` 를 노출한다.

### C. 실패 run 관측성

1. run이 corpus gate에서 실패할 때도 미해결 revision ledger를 `reports/` 에 기록한다
   (corpus-diff는 이미 raise 전 기록 — 동일 시점에 함께).
2. typed CLI payload에 은퇴/후보 count를 추가해 journal alone으로 진단 가능하게 한다.
   URL·원문·자격증명은 출력하지 않는다.

### D. 테스트 (신규, 실패 재현 → 수정 후 통과)

1. 롤링 baseline: 성공 시 기록, 해시 검증, run/publish 불일치 시 거부, seed ledger fallback,
   prune이 최신본을 보존.
2. 은퇴 성공: durable 증거 온전 + grace 충족 → `retired_lineages` populated, run 성공,
   산출물 불변.
3. grace 미충족 → fail-closed(조기 은퇴 금지).
4. 증거 결손(CAS 없음 / OCR 없음 / source row 없음) → unjustified.
5. 상한 초과(건수·비율) → fail-closed.
6. transient 장애: 발급사 snapshot이 retention ratio 미만으로 붕괴 → 기존 guard가
   은퇴 로직보다 먼저 run을 실패시킨다.
7. **2026-09-30 실형태 회귀**: lotte 8 lineage 결석(successor 포함), 타 발급사 불변, bc +1
   → 1회차 run은 candidates 기록, 2회차 연속 run에서 은퇴, corpus count 정합.
8. 2안 선택 시: discovery에 없는 lineage가 historical로 materialize되고 메타데이터가
   durable 값과 정확히 일치(발명 없음).

## Rollout (무중단)

1. feature 브랜치에서 구현 + 전체 로컬 게이트. 운영 무변경.
2. 신규 커밋으로 candidate worker 이미지 빌드: digest·SBOM·provenance·revision label 기록.
   `b42dfb61…` 재사용 금지.
3. 후보 볼륨 `cardrag-worker-v130-candidate-state` 생성 후 운영 볼륨(read-only) 또는
   원격 generation에서 시드. 시드 전후 운영 DB SHA 동일성 입증.
4. candidate 채널로 1회차 배치: exit 0, lotte 8건이 `retirement_candidates` 로 기록,
   `missing_unjustified` 0 또는 gate가 candidates 경로로 통과, 외부 OCR 0건.
5. 2회차 배치로 grace 충족: 8건 `retired_lineages` 분류, run 성공, generation 봉인.
6. 검증 완료 후에만 `/etc/cardrag/worker.env` 의 image digest를 교체(백업 선행),
   channel stable·승인값은 현행 유지. 당일 신선도가 필요하면 timer 창 밖에서
   감독 하 1회 실행하고, 그렇지 않으면 다음 03:00 timer에 위임한다.
7. 운영 run 직후 검증: MCP healthy 유지 + 새 generation 서빙, `tools/list` 12,
   coverage 수치, 외부 OCR 0, 디스크 예산. `df -h`, `docker system df`, 볼륨 크기 전후 기록.
8. 후보 볼륨·폐기 이미지 정리. 롤백용 직전 digest와 env 백업은 최소 1개 일배치 주기까지 보존.

## Acceptance criteria

- 연속 2회 일배치가 무인 성공하고, lotte 8 lineage가 candidates → retired 순으로
  봉인 증거와 함께 처리된다.
- 두 run 모두 `missing_unjustified` 0. 정당한 은퇴 외 baseline 문서 손실 없음.
- MCP 무중단: unhealthy 구간 0, 운영 run 후 generation 전진.
- 외부 OCR 0건, OCR 재사용 우세, 임베딩 provider 배치 유한·보고.
- 롤링 baseline이 성공 run마다 전진하고 해시 검증된다. v1.0.28 ledger는 비교 기준에서
  벗어나되 OCR 재사용용으로 존재한다.
- 신규 테스트 포함 전체 스위트 통과, ruff·mypy·`git diff --check` clean.
- 운영 볼륨은 실험에 사용되지 않았고 DB SHA 불변이 입증된다.
- 종료 시점 디스크 여유 ≥80GB, 후보 산출물 정리, 롤백 산출물 보존.
- `003_*/REPORT.md` 에 커맨드·증거 경로·편차 기록. 001/002 문서는 불변.

## Assumptions

- 8건은 실제 upstream 폐기다(위 증거). 재게시 시 재활성화 경로를 반드시 둔다.
- grace 기본값(2 run / 3일)과 상한(25건 / 0.5%)은 Reviewer 제안이며, Executor는
  근거를 남기고 조정할 수 있으나 최소 2회 연속 성공 run 요건은 유지한다.
- MCP는 은퇴 분류가 포함된 corpus를 추가 변경 없이 서빙할 수 있다(문서 수 감소만 반영).
- 이번 실패는 v1.0.29 코드 회귀가 아니다. 파서·discovery·OCR·임베딩 지표는 정상 범위로
  측정됐으므로 이미지 롤백은 불필요하다.
