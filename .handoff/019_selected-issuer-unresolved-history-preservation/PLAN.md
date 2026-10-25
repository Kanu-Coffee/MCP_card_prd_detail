# 019 — 카드사 선택 실행의 미해결 개정 이력 보존 개선

작성: 2026-10-19 Planner. 사용자 요청은 신규 계획 작성이다. 이 문서는 Executor용이며 이번 세션에서 구현·배포·실제 수집은 하지 않는다. 상세 사실/미검증 구분은 같은 디렉터리 ANALYSIS.md 참조.

## 목표와 범위

기존 운영 baseline에 원본 메타데이터 미확인 이력이 있어도, 그 이력의 증거와 비선택 카드사 데이터를 보존하며 `--issuer` 및 round-robin 실행이 완료되게 한다. 단순 예외 무시·이력 삭제·출처 추정으로 성공 처리하지 않는다. 모든 카드사에 적용되는 일반 프로세스로 개선한다.

운영 실패는 우리 run `6266b015df8c496586fe6a55362edd09`의 scope/issuer_scope_revision_source_missing이다. 기존 baseline run `f1871a51eff549ea9aee95b73eaad99d`의 미해결 identity 22개 중 4개가 canonical snapshot/seed에 없다. PDF cache source 행의 카드사 표시는 하나 1개·현대 3개이며 모두 source_metadata_unresolved다. 조사 결과는 ANALYSIS에 기록했다. 018 준비 및 017 구현 위의 후속 수정이며 원본 계획·보고서·fix 이력은 덮어쓰지 않는다.

## 유지할 계약

- 비선택 7사의 문서·OCR·구조화 결과·embedding·개정/미지원 이력을 유지하며 해당 카드사 origin/OCR/provider 호출을 실행하지 않는다. 기존 실제 serving pointer에 연결된 검증된 baseline만 재사용한다.
- 기존 PDF 캐시·증분 수집·90일 분산 감사·선택 범위·freshness/not_scheduled·회전 cursor·실행 lock·백업/GC pin 정책을 유지한다. 선택되지 않았다는 이유로 단종/삭제/장애 처리하지 않는다.
- ZIP 자체 오류 격리, 압축 해제 금지, DRM/기타 문서 오류의 정상 문서 처리 지속, bounded reconfirm, 게시 전 오류 증거 고정 등 017/FIX_09 정책을 유지한다.
- 과거 상품명/효력일/정식 SourceRecord를 PDF cache URL·timestamp·현재 상품명으로 만들어내지 않는다. 4개 source_id/상품/카드사를 예외로 하드코딩하지 않는다.
- 진행 중인 다른 변경을 덮어쓰거나 운영 DB/seed/seal을 수작업 수정하지 않는다. 프록시 실제 주소·SSH 키·VPN 설정은 커밋하지 않는다.

## 착수 확인 및 관련 파일

Executor는 현재 Git status/diff, 적용 AGENTS/GEMINI, 017 PLAN/ANALYSIS 및 FIX_01–09와 보고서, 018 PLAN/REPORT/LIVE_FIX_01/LIVE_REVIEW_01, 본 ANALYSIS를 읽는다. 운영 데이터가 바뀌었으면 baseline/run/image를 다시 식별하고 차이를 REPORT에 남긴다.

중점 파일:

- `apps/cardrag-worker/src/cardrag_worker/issuer_scope.py`: preservation_source, preserve_inputs.
- `revision_history_v5.py`: 미해결 identity의 정규화/사유/해시, 증거 기반 이력 규칙.
- `pipeline.py`: _known_snapshot_sources, 보존 호출 전후, 미해결 집계·seal 검증/생성·재개.
- `state.py`, `state_seed_v122.py`, `pdf_cache.py`: snapshot/seed 복원, source identity 및 revision 조회/검증.
- `partial_execution.py`, `partial_cli.py`: ReuseSource, collect/process, 재처리/게시 재개.
- `tests/test_017_scope.py`, `tests/test_pipeline_v5.py` 및 기존 revision-history 테스트. 필요한 전용 테스트를 추가한다.
- 운영 설명이 달라질 경우 `docs/OPERATIONS.md`. 운영 helper/이미지는 별도 배포 단계 대상이다.

## 설계 결정 및 구현 지침

### 1. 카드사 귀속과 문서 메타데이터 복원을 분리

완전한 SourceRecord는 실제 문서 복원에 계속 요구한다. 미해결 identity를 이력으로 유지하고 selected/nonselected를 판별할 때 같은 수준의 메타데이터를 요구하지 않는다.

권장 순서: canonical snapshot/seed의 SourceRecord가 있으면 기존 증거로 귀속한다. 없으면 기존 PDF source identity/revision 저장 구조에서 source_id를 재계산·대조하고, 해당 pdf_sha256 revision 연결 및 issuer 정합성을 확인해 **카드사 귀속만** 얻는다. row 존재·문자열 issuer만 믿거나 source_id prefix로 추측하지 않는다. 검증된 SourceRecord와 cache identity가 충돌하면 명시적 차단한다. 기존 identity 검증 함수를 우선 재사용한다.

이번 4건은 이 경로의 정상 지원 대상이다. `source_metadata_unresolved`를 해결됐다고 바꾸지 않고 원래 identity/hash/사유를 유지한다. 같은 규칙을 다른 미해결 사유와 seed/구형 seal에도 적용하되 기존 계약을 약화시키지 않는다.

### 2. 선택/비선택 미해결 이력 병합 정책

- 검증된 비선택 issuer의 ledger entry는 그대로 계승한다. 새 run에서 정규화·중복 제거하고 기존 count/hash 계약을 재계산한다.
- 선택 issuer는 기존 revision-history planner로 재평가한다. metadata가 여전히 없으면 unresolved로 남기며, 원래 identity가 새 계획에 포함되지 않았다는 이유만으로 조용히 삭제하지 않는다. 검증된 해결/충돌 판정 등 기존 정책상 근거가 있을 때만 해소하고 그 근거를 테스트한다.
- source 귀속 자체를 검증할 수 없는 경우도 ledger를 버리면 안 된다. 보수적으로 미해결 entry를 계승하는 fallback을 우선 검토하고, 이 fallback이 필요한 실제 문서 복원이나 무결성 충돌까지 통과시키지 않도록 한다. 안전한 보존을 증명할 수 없으면 구체적 reason code로 조기 차단한다. 최종 선택과 근거를 REPORT에 명시한다.
- 새 issuer 보조 정보를 영속화할 필요가 있으면 기존 seal/ledger와 호환되는 선택적 evidence로 설계하고 hash/schema 검증을 포함한다. 기존 seal rewriting·일괄 DB migration을 복구의 전제로 삼지 않는다.

### 3. 조기 검사와 진단

현재는 origin 수집 후 보존 단계에서 실패한다. baseline 결합·기존 이력의 귀속/보존 가능성은 가능한 한 discovery/download 이전, 기존 lock 및 baseline fence 안에서 검사하거나 plan으로 고정한다. 검증 결과를 실제 보존에서도 재사용해 검사 시점과 실행 시점이 어긋나지 않게 한다. 수집 결과에 의존하는 검사는 해당 단계에 남긴다.

실패 시 source_id·해당 ledger identity·reason·증거 종류 등 제한된 진단을 남겨 단순 corpus 오류만으로 추적해야 하는 상황을 개선한다. 민감한 URL·설정·전체 원본 payload는 출력하지 않는다. 오류를 제외하고 성공으로 보고하는 catch-all은 금지한다.

### 4. 전체 배치/재개 호환

수동 선택, round-robin, 전체 배치, collect/process 및 기존 skip/reprocess/publication-only resume가 같은 ledger 의미를 사용하게 한다. 새 schema가 불필요하면 추가하지 않는다. 기존 sealed artifact 읽기, hash 검증, 중단 후 재개, GC 보존 대상과의 관계를 확인한다. CLI scope mode 표시와 selected 목록의 불일치는 부수 관찰 사항이며 이번 수정의 핵심 목표와 분리해 기록한다.

## 인수 기준과 검증

1. 운영 장애를 닮은 최소 fixture: 검증된 baseline에 `source_metadata_unresolved` entry가 있고 canonical snapshot/seed에는 SourceRecord가 없으며 PDF cache identity/revision만 있다. 수정 전 실패·수정 후 통과를 재현한다. 실제 전체 운영 DB나 비밀 설정을 저장소에 넣지 않는다.
2. 우리와 같이 해당 이력이 비선택인 실행에서 완료하고 원래 identity/pdf_sha256/reason_codes가 새 seal/ledger에 남아 count/hash 검증을 통과한다. 하나/현대처럼 해당 이력이 선택인 실행도 재평가·미해결 유지/정당한 해소를 검증한다. 나머지 미해결 사유도 보존한다.
3. 비선택 manifest 문서·OCR/구조/embedding 참조 및 개정 이력의 의미가 변경되지 않는다. 비선택 adapter discovery/PDF/OCR/parser/provider 호출에는 실패 spy를 사용해 실제 미호출을 증명한다. 전체 serving 8사가 보존된다.
4. source 귀속 정보 부재, source_id 재계산 불일치, revision PDF 링크 불일치, known/cache issuer 충돌, 손상 ledger/hash, 중복 entry, 구형 seal을 시험한다. 증거가 부족하면 보수적 유지 또는 설계된 조기 오류이며 데이터 삭제·허위 metadata 생성은 없다.
5. 조기 차단 가능한 baseline 오류는 origin 호출 없이 실패한다. 재개/다음 issuer 실행에서 이력 유실·중복·hash drift가 없고 선택 scope·동시 실행 방지·순회 상태가 유지된다.
6. 선택 실행의 freshness만 갱신하고 비선택은 not_scheduled/기존 freshness를 유지한다. 90일 감사 due 상태를 임의로 완료시키거나 리셋하지 않는다. PDF 캐시 재사용과 백업/GC 회귀를 확인한다.
7. 기존 전용/관련 테스트 뒤 Worker 전체 테스트 및 ruff를 실행한다. 권장 명령은 `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -q`, `uv run --no-sync pytest apps/cardrag-worker/tests/ -q`, `uv run --no-sync ruff check apps/cardrag-worker/`. 범위/실제 결과를 REPORT에 기록하고 기존 1370 통과를 새 검증 대신 사용하지 않는다.

## 운영 검증·인계 경계

구현 Executor는 코드/테스트와 REPORT를 작성해 Reviewer에게 넘긴다. 본 PLAN만으로 배포·timer 활성화·실제 카드사 호출을 실행하지 않는다. 앞선 운영 적용 의도는 유지하되 사용자가 직접 수동 실행한다는 018 절차를 존중한다. 검토 통과 후 식별 가능한 새 이미지/release를 준비하고 기존 볼륨·백업/rollback·helper의 물리 경로 처리를 유지한다. 이번 실패 이미지에서 자동 배치를 재개하지 않는다.

사용자가 우리카드를 다시 실행한 뒤 컨테이너 exit와 run 상태, 신규 PDF/상품/OCR, 비선택 처리 미호출, 8사 serving 유지, unresolved ledger 계승, 실제 증분백업 완료를 확인한다. `backup_pending=[]`만으로 성공 판정하지 말고 해당 run 백업 등록과 기존 백업 검증 기준을 함께 적용한다. `no_change`처럼 백업이 필요 없는 결과는 명시적 정책 근거와 실행 증거로 설명한다. unsupported가 남으면 degraded로 정확히 보고한다.

우리 성공 후 나머지 7사를 사용자가 하나씩 실행한다. 수동 `--issuer` 8회는 자동 날짜 cursor의 실시간 8일 검증과 구분하며 cursor는 회귀 테스트/상태 증거로 확인한다. 운영 미지원/신규 상품 등 남은 항목을 명시한 다음 사용자와 기존 절차에 따라 timer를 맞춘다. 성공하지 않은 상태에서 “운영 적용 전부 완료”로 판정하지 않는다.
