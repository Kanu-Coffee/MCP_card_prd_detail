# 019 FIX_16 — pending 독립 소유 백업 판정 및 RO 운영 경로 호환

2026-10-20 Reviewer: Codex. FIX_15 리뷰는 보완 필요다. REVIEW_05.md의 재현과 이전 PLAN/PLAN_AMENDMENT_01·REPORT·FIX_01–15/REPORT 및 리뷰 이력을 읽고 현재 Git/소스를 확인한다. 기존 HEAD `aaeb2c4` 위 변경과 과거 문서를 보존한다. 본 FIX를 수행하고 FIX_16_REPORT.md를 새로 작성한다. 019의 국소 보완을 유지한다.

## 1. [P1] transport 상태와 필수 metadata ownership을 분리

현재 scope는 content/adopted 문서의 현재 소유 metadata라도 pending이 아니면 정상 shared cache로 제외한다. 기존 신규 fixture의 metadata pending 2건을 lost_source로 바꾸면 required scope가 CAS 2개로 축소되고 실제 flush가 complete=True가 된다. REVIEW_05와 `/tmp/review_fix15_loss.py`에 재현이 있다.

- 현재 문서가 소유한 필수 metadata는 pending/lost_source/기전송·receipt-only/행 소실 여부에 의존하지 않게 판정한다. 기존 verified registration 또는 일관된 ownership 근거를 사용하고, 근거가 부족하면 안전하게 미확인으로 보류한다. transport 상태 하나를 ownership 증거로 대신하지 않는다.
- 정식으로 복구 가능한 spool/cache 증거는 sealed PDF/OCR 및 현재 root/manifest/READY 바이트에 연결해 재바인딩할 수 있다. doc_map/doc_info/receipt 문자열만으로 바이트 검증을 생략하지 않는다. 올바른 과거 shared-cache 제외와 현재 소유 항목의 소실을 구분한다. 최소 변경을 택하고 필요 이상 범용 resolver를 만들지 않는다.
- 정상 삼성 전환의 과거 native 2건 제외와 현재 native/adopted/content의 진짜 필수 항목 보호를 함께 유지한다. 단순 pending SQL 조건 확대만으로 완료로 보고하지 않는다.
- 같은 fixture에서 pending→lost_source 전환, source/manifest 손상, 행 정리·receipt-only, 정식 검증 복구, 재시도, 다른 issuer/root를 검사한다. 동일 ownership의 정상 완료 판정이 transport 상태 때문에 거짓으로 바뀌지 않아야 한다. 실제 `backup flush --run --issuer` CLI/factory 경로 회귀도 추가한다. 기존 새 테스트는 ledger.flush 직접 호출에 치우쳐 있다.

## 2. [P2] RO helper와 scope 진단 호환

`scoped_backup_scope`가 `_last_excluded_shared_cache_by_run` 생성자 초기화를 필수로 가정하여 기존 운영 monitor의 RO subclass가 AttributeError로 실패한다.

- 기존 RO 접근 방식을 유지하면서 scope/diagnostics를 조회할 수 있게 한다. 순수 결과/명시적 진단 전달 또는 안전한 RO 초기화 호환 등 최소 방식을 택한다. 쓰기 가능한 BackupLedger 초기화로 우회하지 않는다.
- 없는 run/연속된 다른 run/다른 scope에서도 이전 제외 카운트가 섞이지 않게 검증한다.
- 새 소스와 실제 operations monitor/status/summary의 호출 경로를 RO state로 검증한다. helper 수정이 필요하면 새 배포 준비에 반영하며 과거 release를 임의로 활성화/기동하지 않는다.

## 3. [P2] 실제 준비 가능한 배포·복귀 절차와 canonical 운영 문서

- REPORT의 명령은 실행에 필요한 operations helper/deployment metadata/private overlay 준비, 이미지 참조 갱신, source 식별과 설치 소스 확인까지 연결한다. repo에 operations가 있다는 가정을 제거한다. 새 release가 복사한 old helper·old image를 그대로 쓰지 않는지 확인한다.
- digest는 현재 배포 대상의 tracked/untracked 제품 소스와 필요한 설정/lockfile 등 실제 입력을 포함한다. 기존 운영의 파일 경로+바이트 digest 방법도 활용 가능하다. `git diff HEAD`만을 source digest로 사용하지 않는다. overlay Dockerfile을 사용하면 고정된 과거 label/base 이미지 근거도 갱신·검증한다.
- rollback 전 활성 timer/service, 수동 compose run을 포함한 Worker 실행, lock을 확인하는 기존 검증된 경계를 사용한다. 기본 run 자동 기동 금지는 유지한다. 공유 lock의 경쟁을 피하는 절차와 실행 중 차단을 설명한다.
- `docs/OPERATIONS.md`의 volume 조회 명령을 RO mount와 컨테이너 내부 glob 확장으로 수정한다. 복구본 재사용은 일관된 SQLite snapshot의 내용 동일성 증명을 조건으로 명시한다. 현재와 다른 DB는 새 snapshot을 보호하며, worker/backup ledger 시점과 WAL/lock 처리를 설명한다.
- 삭제는 요구하지 않는다. 이전 보고서의 후보/예상량은 실제 보호 집합·의존성·물리 공유 검증 전 자동 삭제 목록으로 취급하지 않는다. 전체 GC/출력 형식 재설계는 범위 밖이다.

## 검증 및 인계

BC 수정과 기존 정상 삼성 정책을 유지하고 관련 CLI/백업/RO helper 회귀, Worker/Core 요구 검사와 ruff check/format check 결과를 기록한다. 실제 운영 자료 RO 대조와 fixture/테스트/정적 명령 검증을 분리한다. 과거 보고서의 부정확한 절차는 새 보고서에서 대체하며 이전 문서를 덮어쓰지 않는다. Reviewer 통과 전 새 배포·BC 배치·timer 기동을 하지 않는다. 기존 사용자 승인 범위의 후속 운영 적용과 수동 테스트 의도는 유지한다.
