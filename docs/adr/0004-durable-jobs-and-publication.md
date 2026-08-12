# ADR-0004: Durable job과 generation 게시

- 상태: Accepted
- 일자: 2026-08-12

## 결정

작업 상태는 `queued`, `running`, `retry_wait`, `succeeded`, `dead_letter`, `cancelled`로 구분한다.
Worker claim은 PostgreSQL `FOR UPDATE SKIP LOCKED` transaction으로 원자화하고 lease, heartbeat,
attempt history와 증가하는 fencing token을 사용한다. Lease를 잃은 worker는 진행 중 작업을
취소하며 오래된 fencing token으로 DB stage 완료나 최종 artifact를 commit할 수 없다. 재시도는
유형별 유한 budget, exponential backoff+jitter와 dead-letter/redrive 이력을 가진다.

OCR page/chunk checkpoint는 immutable content object와 durable DB state로 보존한다. 새 fenced
attempt는 동일 provider와 page input hash의 이전 checkpoint object를 hash 검증한 뒤 격리된
workspace로 재사용한다. provider/model 변경 시에는 이전 페이지를 섞지 않고 문서 전체를 새
attempt로 처리한다. Downstream idempotency key는 generation을 포함하며, content가 같은 문서도
새 generation snapshot에 materialize한다.

Generation은 정상 경로에서 `building → validating → ready → published/retired`로 진행하고,
검증 실패·issuer terminal 실패·run 취소는 이유를 남긴 `failed`로 종결한다. 최신 document의
OCR·structure·embedding·index stage coverage를 각각 측정하며 모두 100%가 아니면 seal/publish를
거부한다. 실패 candidate는 원인 보고와 함께 7일 보존한다. 게시와 rollback은 file pointer와
PostgreSQL active row의 일치가 복구 가능한 publication protocol로 수행되고 readiness가 두 권위를
대사한다. 성공 최근 3개와 active/pin을 보존하며 owner-only 04:00 one-shot이 DB와 file retention을
함께 실행한다. DB-backed retention에서는 PostgreSQL `created_at` 순위와 pin/active 판정을 권위로
삼고, 삭제가 commit된 정확한 generation ID의 published/candidate tree를 안전하게 제거한다. 이때
filesystem mtime 기반 최근 3개 판정을 중복 적용하지 않으며, 이전 실행이 DB commit 뒤 중단해 남긴
sealed orphan도 DB 보존 집합과 대사해 다음 실행에서 제거한다.

## 검증

- `tests/integration/test_postgres_jobs.py`
- `tests/unit/test_generation.py`
- `tests/unit/test_scheduler.py`
- `tests/integration/test_generation_retention.py`
- generation materialization·publish 오류주입 통합 시험
