# 019 FIX_06 — 라이브 후속 보완: 선택 백업 완결·공통 처리 비용·scope 진단

2026-10-20 Reviewer: Codex. 사용자 요청에 따라 추가 보완 사항을 검토해 발행한다. **현재 게시 성공과 비선택 7사 보존은 확인됐다. 백업 미완료 결함과 성능/표시 보완은 아래 범위로 수정한다.**

## 기준, 현재 상태와 변경 경계

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, REPORT 및 FIX_01–05/각 REPORT, `REVIEW_01.md`, `OPERATIONS_REPORT.md`, `LIVE_REVIEW_01.md`, 현재 Git status와 관련 구현을 대조했다. 기존 문서는 보존한다. 기존 correction의 최고 번호는05이며 모든 FIX에 보고서가 있으므로 새 번호06을 사용한다.

- Git HEAD `aaeb2c4` 위에 017–019 변경이 미커밋으로 공존한다. Executor는 착수 시 실제 Git/소스를 다시 확인하고 기존 변경을 덮어쓰지 않는다.
- 현재 release `/opt/cardrag/019-4dba44664659`, image `cardrag-worker:019-4dba44664659`다. 검토 시 현재 worktree worker/core 소스 SHA256은 운영 image에 고정한 `4dba4466465948c04fb07491b2f0191d69ab43665b77ad3981a37a6a80ec6a6b`와 같았다.
- 우리 run `41ce678e665b47179f8bfb2d82273882`는 succeeded/exit0이며 generation `g-41ce678e665b47179f8bfb2d-e884484ab66b`를 게시했다. 우리979→980, 전체5536→5537. 신규 KG이니시스 우리카드1건 외 계약 행 변경0, 비선택 origin/stage0이다.
- 코드 리뷰 통과는 운영 백업 완결 및 성능 목표 달성의 증거를 대신하지 않는다. 이번 FIX는 정상 게시된 결과를 되돌리는 지시가 아니다.

## 1. [P1] 선택 카드사의 신규 OCR metadata가 scoped backup 허용 경로에서 누락됨

관련 파일/경로:

- `apps/cardrag-worker/src/cardrag_worker/backup.py`: `record_document_ocr`, `record_run_success`, `scope_remote_paths`, `flush`, canonical remote root/receipt 처리.
- `cli.py`: `run_pipeline`의 선택 backup 등록/허용 경로/최종 결과.
- `partial_cli.py`: process 후 같은 backup 경로.
- 필요 시 `ocr.py`, `pipeline.py`, 관련 OCR/result/seal 계약. 기존 optional field를 허위 값으로 채우거나 과거 seal을 rewrite하지 않는다.
- `tests/test_017_scope.py`의 scoped backup 테스트, `test_local_serving_and_backup.py` 및 필요한 전용 회귀.

### 확정 근거

라이브 run의 `backup_status=degraded`, pending2건/2076bytes다. 신규 PDF285264bytes와 OCR 본문15126bytes는 해당 canonical remote root의 검증 receipt가 있다. 남은 것은 같은 신규 문서의 native OCR `manifest.json`1791bytes와 `READY.json`285bytes다.

최종 generation document에는 `ocr_reuse_key=null`, `ocr_cache_kind=null`이지만 `record_document_ocr()`는 실제 생성된 OCR manifest를 읽어 metadata를 pending에 등록했다. `scope_remote_paths()`는 seal document에 reuse_key가 있을 때만 두 metadata 경로를 포함한다. 이번에는 키가 없어서 선택 문서 소유의 pending도 제외됐다.

FIX 발행 시 readonly 재확인:

```text
selected=[woori], allowed_path_count=3660
ocr_manifest 1791bytes: allowed=False, doc_info 매핑 issuer=woori
ocr_ready     285bytes: allowed=False
해당 문서의 sealed ocr_reuse_key=None
```

READY에는 직접 doc_info가 없어도 같은 검증된 manifest/reuse_key의 짝이라는 증거가 있다. prefix/파일명만 보고 임의로 허용해서는 안 된다.

### 수정 요구

1. 선택 문서에 실제로 연결된 OCR body/manifest/READY를 하나의 검증 가능한 backup 단위로 다룬다. recorder와 scope planner가 동일한 document↔OCR artifact 근거를 사용하게 한다. seal의 optional reuse_key가 없는 기존 정상 결과에서도 실제 manifest/receipt/보존된 검증 입력을 통해 안전하게 연결할 수 있어야 한다.
2. 연결을 검증할 때 document_id, PDF/OCR hash·크기, reuse_key/contract/kind 및 artifact hash를 정합성 있게 대조한다. 운영 source/product ID, 이번 key/path, 카드사 이름을 처리 조건으로 하드코딩하지 않는다. `native`/`content` 등 기존 cache 계약이 다르면 각각의 증거에 맞게 처리하고 임의 변환하지 않는다.
3. 전체 backup 허용으로 넓히거나 비선택 issuer pending까지 전송해서 누락을 가리지 않는다. 공유 CAS는 선택 문서가 실제 참조하는 바이트만 허용하며 unrelated cache/metadata는 제외한다. doc_map 문자열 하나나 임의 local path만으로 귀속을 신뢰하지 않는다.
4. 기존 pending도 같은 정식 재시도 경로에서 회수 가능하게 한다. 수작업 SQL/행 삭제, 허위 receipt 삽입, immutable remote overwrite, OCR/embedding/수집 재실행을 복구 전제로 삼지 않는다. 재시도는 hash/root에 묶인 검증 및 index commit 후에만 완료 처리하며 멱등적이어야 한다.
5. 등록 여부와 백업 완료를 구분한다. 선택 run의 남은 artifact, 범위 밖 pending, budget 초과, 전송/검증/index commit 실패를 구별해 결과를 보고하고, 이번 선택 run의 metadata 누락을 전역 pending만으로 모호하게 만들지 않는다. 현재 상태/원래 run 종료 시점의 상태를 뒤섞지 않는다.

### 인수 기준

- 라이브와 같은 fixture: 신규 OCR 성공, seal reuse_key/kind는 null, 실제 OCR metadata와 recorder pending은 존재. 선택 허용 경로에 검증된 body/manifest/READY가 포함되고 mock flush 후 세트 전체의 receipt/index가 맞으며 해당 pending이0이 된다.
- 같은 fixture에 비선택 issuer pending과 임의 doc_map/경로 충돌을 추가하고 미전송/거부를 확인한다. 연결 없는 READY, hash/PDF/document 불일치, 손상 manifest는 안전하게 차단한다.
- 정상 키 존재, 구형 optional metadata, native/content 계약, 공유 CAS, 재실행/중단 후 재시도, 원격 동일 바이트 재사용/다른 바이트 충돌, budget/index commit 실패를 검사한다.
- 일반 선택/round-robin/전체 배치/실제 collect→process가 같은 backup 의미를 사용한다. backup 보완 때문에 비선택 adapter/OCR/provider를 호출하지 않는다.
- 이번 실제 pending2건에 적용할 **읽기 전용 대상 확인/복구 절차**를 REPORT에 작성한다. 실행 허가 없이 실제 flush/원격 쓰기/배포는 수행하지 않는다.

## 2. [P2] 선택 수집의 이점이 전체 export·대량 cache 조회 비용에 묻힘

관련 위치:

- `pipeline.py:_build_v5_generation`: 전체 `ordered_view_pairs`, token/cache binding, unique cache lookup, `lazy_cache_vector`, exporter 호출.
- `state.py:get_embedding_v5`, `exporter_v5.py:LazyEmbeddingVector` 및 serving exporter의 DB/vector/FTS/index/검증 경계.
- `issuer_scope.py`의 검증된 비선택 결과 재사용 및 기존 capacity/메모리/해시/프로필 계약.

### 관측과 해석

직전 전체 배치와 이번 우리 단독의 performance elapsed는5987.622→5397.323초로 약9.9% 감소했다. 비교 배치는6사 성공/2사 carry였고 신규 OCR0건, 이번은 신규 OCR1건이므로 통제된 속도 실험은 아니다.

- OCR/local processing1492.362→361.798초로 감소.
- export2387.409→2493.494초로 감소하지 않음. 이번 컨테이너90분34초 중41분33초가 export 계측이다.
- embedding cache 조회395137→395174, export cache 조회619092→619134로 거의 같음.
- 새 generation 전체의 DB/벡터 조립 비용이 남는다. 비선택 origin/OCR 재실행이나 데이터 덮어쓰기와 혼동하지 않는다.

현재 코드는 embedding 단계에서 unique key별 `get_embedding_v5()`를 수행한 뒤 exporter lazy loader가 view별로 같은 계열의 DB 조회·identity/hash 검사를 다시 수행한다. 중복 조회와 export 내부 비용은 개선 후보이나, 숫자만으로 전체41분을 특정 함수 하나의 순수 비용이라고 단정하지 않는다.

### 수정 요구 및 인수 기준

1. export 경계의 cache fetch/vector 검증/sidecar 쓰기/SQLite 행 쓰기/FTS·index/최종 무결성 검사를 구분해 필요한 계측을 추가한다. 중첩·누적 시간을 wall time과 혼동하지 않고 IO/조회 횟수도 함께 기록한다.
2. 동일 입력의 전후 비교를 수행하고 **측정 근거가 있는 최소 성능 개선**을 적용한다. 우선 후보는 제한된 배치 조회, 중복 vector key의 반복 DB fetch 제거, 검증된 비선택 artifact의 재사용이다. 현재 계약을 유지하는 가장 작은 방법을 선택한다.
3. 모든 embedding을 Python 메모리에 올리는 방식은 금지한다. 운영은 약61.9만 view와4096D 벡터를 다룬다. bounded buffer/cache/streaming과 기존 capacity/WAL/free-space gate를 유지하고 peak RSS·임시 디스크 증가를 측정한다.
4. profile/input SHA/vector bytes/hash/정규화·dimension, cache row 유실/변경 검증, canonical row 순서, corpus/contract hash, 원자적 게시와 재개/fence를 약화시키지 않는다. 검사 생략으로 시간을 줄이거나 현재 serving DB를 in-place 수정하지 않는다.
5. 반복 cache key가 있는 여러 문서와 선택+비선택 조합에서 실제 DB fetch 횟수 감소 및 출력 의미/벡터 바이트 동일성을 검사한다. 시간은 같은 fixture/설정에서 반복 측정해 비교하고, host 전체 운영 DB를 복제한 무거운 반복 benchmark는 기본 검증으로 요구하지 않는다.
6. 단일 issuer 실행이 전체의1/8이 돼야 한다는 근거 없는 목표는 두지 않는다. 대규모 증분 serving DB/별도 shard 구조 전환이 필요하다면 이 FIX에 섞지 않고 별도 설계 과제로 제시한다. 개선하지 못한 지배 비용은 REPORT에 남기며 계측만 추가한 것을 전체 성능 문제 해결로 보고하지 않는다.

## 3. [P2] scope 표시와 운영 결과의 진단 의미를 일치시킬 것

### scope 전달 누락

라이브 scope는 `selected=[woori]`인데 `mode=all`이다. CLI `run_command()`는 `--issuer`에 대해 mode를 selected로 계산하고 `scope_session`도 올바른 assignment를 만든다. 그러나 main CLI의 `WorkerPipeline(...)` 생성은 `assignment.scope.selected`만 전달하고 `issuer_execution_mode`를 넘기지 않아 기본 all이 pipeline의 frozen scope/performance에 기록된다.

- assignment의 mode/pool/selected를 pipeline/collect-process/회전 진단까지 일관되게 전달한다.
- 새 수동 selected/명시적 all/round-robin 각각의 실제 CLI 및 저장된 scope를 검사한다. 수동 실행이 날짜 cursor를 소비하지 않는 계약을 유지한다.
- 기존 immutable scope/seal을 다시 쓰지 않는다. 과거 `mode=all, selected=subset` run의 resume 호환은 별도 기존 의미로 처리하며 새 표시 수정 때문에 복구를 막지 않는다.

### host 도구 수정 유지 및 완료 판정

이전 점검에서 host operations의 setgid/group read와 backup_status 표시를 이미 수정했고 실제 명령이 동작한다. 새 release 준비 시 이 수정이 사라지지 않게 운영 인계 자료/공개 가능한 helper 재료를 보존한다. 비공개 overlay·proxy/auth 값은 저장소에 넣지 않는다.

- `run-issuer.sh`의 live group 상속(mode2750/cardrag), status/summary의 backup_status/pending 표시를 유지한다.
- cached run 결과는 종료 당시 snapshot임을 명확히 하고, 이후 backup 재시도 확인은 현재 ledger/receipt 상태를 별도로 읽는 경로로 제공한다. 과거 degraded 로그를 고쳐 succeeded로 만들거나 오래된 캐시로 현재 복구 실패/성공을 오판하지 않는다.
- `backup_recorded=True`, exit0, pending 전역0 중 어느 하나만으로 전체 검증 완료를 판정하지 않는다.

## 공통 보존 조건, 검증 명령 및 산출물

우리만980건으로 게시해 나머지7사를 제거하는 변경은 금지한다. 실제 serving5537건/8사와 비선택 문서·OCR·구조/embedding 참조, 기존 unresolved 정비, 최신 정상본/DRM/ZIP 정책, 90일 감사·freshness·lock·cursor·backup/GC pin을 유지한다. 기존1390건 통과를 이번 변경 검증 대신 쓰지 않는다.

권장 검증:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_local_serving_and_backup.py apps/cardrag-worker/tests/test_partial_execution.py -q
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
uv run --no-sync ruff check apps/cardrag-worker/
uv run --no-sync ruff format --check apps/cardrag-worker/
git diff --check
```

수정 shell helper가 있으면 shellcheck, host와 컨테이너 UID/GID가 다른 상태의 실제 metadata read 검사를 수행한다. 성능 변경은 추가로 동일 fixture의 전후 계측·peak RSS/IO 및 integrity/출력 동등성 증거를 남긴다.

Executor는 착수 시 Git/기존 handoff와 관련 파일을 다시 검사하고 `FIX_06_REPORT.md`에 항목별 변경, 명령/실제 결과, 편차, 성능 개선/잔여 비용, pending 복구 계획 및 운영 미검증 항목을 작성한다. 1번 백업 결함을 먼저 검증 가능하게 완료하고 성능 변경을 분리해 리뷰 가능하게 제시한다. 대형 변경을 이유로 백업 누락을 미해결로 두지 않는다.

## 이번 발행에서 수행한 확인과 운영 경계

현재 소스/라이브 증거/기존 테스트 구조를 읽고, 운영 image에서 network=none·SQLite mode=ro로 pending2건의 허용 경로 누락을 재확인했다. 현재 source digest도 운영 snapshot과 대조했다. `git diff --check`는 통과했다. 제품 코드 변경이 없는 FIX 발행이므로 Worker 전체 테스트를 새로 실행했다고 보고하지 않는다.

이번 요청은 FIX 발행이며 코드 구현·실배포·원격 백업 쓰기·추가 카드사 실행·timer 활성화를 수행하지 않았다. 기존 사용자 수동 우리→나머지7사 검증 순서를 유지한다. 자동 실행 설정은 관리자 소유 환경/compose 경로와 수동 overlay의 정합성까지 확인한 후 별도 운영 단계에서 맞춘다. 이번 run의 백업2건 보완 전 전체 운영 검증 완료로 판정하지 않는다.
