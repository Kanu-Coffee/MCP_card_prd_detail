# 019 FIX_15 — content 문서 소유 백업 보호와 실행 가능한 운영 인계

2026-10-20 Reviewer: Codex. FIX_14 리뷰는 보완 필요다. 상세 사실/재현은 REVIEW_04.md를 참조한다. 가장 높은 미보고 FIX인 본 문서를 수행하고 FIX_15_REPORT.md를 새로 작성한다. 기존 PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–14/REPORT, 리뷰 이력과 현재 Git을 확인하고 HEAD `aaeb2c4` 위 기존 작업과 문서를 보존한다. BC의 variant 수정은 유지한다. 019 범위에서 국소 보완하며 새 PLAN을 만들지 않는다.

## 1. [P1] shared-cache 제외와 현재 소유 metadata 소실을 구분

위치: `backup.py:scoped_backup_scope` tagged Phase 1/2와 diagnostics, `test_fix_14_bc_samsung_backup.py` 및 기존 백업 회귀.

현재 content/adopted의 태그 rebind를 건너뛰고 현재 검증된 bundle이 없으면 모든 doc_map을 제외한다. 그 결과 현재 소유 native-manifest가 소실되면 전송 대기 metadata를 out-of-scope로 밀어내 완료=True가 된다. 반대로 정상 bundle이 있으면 과거 다른 key의 태그가 unbound로 남는다. 기존 신규 테스트로 두 결과를 재현했다(REVIEW_04).

요구:

- 현재 문서의 cache_kind만으로 모든 태그를 제외하지 않는다. 현재 문서 소유 bundle과 과거 공용 cache를 기존 암호학적 증거/정식 등록 근거로 구별한다. 정상 shared-cache 정책상 제외는 유지한다.
- 현재 소유 bundle의 증거를 잃었다면 검증 가능한 정식 근거로 복구하거나 required/unbound/missing 등 미확인 상태로 남겨 완료를 막는다. spool/cache/receipt가 있다는 문자열만으로 권위를 부여하지 않는다. 손상 metadata와 실제 과거 cache 제외를 구분할 수 없으면 안전하게 보류하고 구체적으로 보고한다.
- content/adopted 문서에 현재 검증된 native bundle이 있어도 과거 shared cache 태그가 불필요하게 완료를 막지 않게 한다. key별·현재 ownership별로 판정하고 현재 native 필수 경로 보호를 유지한다.
- `excluded_shared_cache`는 실제 범위에서 제외한 고유 경로만 세며 현재 PDF/OCR CAS 등 allowed/expected 경로와 겹치지 않는다. scope가 바뀌거나 없는 run 조회 후 이전 호출의 instance 진단 카운트가 잘못 재사용되지 않도록 확인한다.
- 위 기존 fixture를 확장하여 (a) 정상 bundle+옛 태그에서 완료 가능, (b) 등록 후 manifest 소실/손상에서 거짓 완료 금지, (c) 복구 가능한 정식 증거의 검증 재바인딩, (d) 정상 삼성 전환 유지, (e) native/adopted/content·다른 issuer/root·조작 doc_info·retry를 검사한다. scope뿐 아니라 실제 `backup flush --run --issuer` CLI/factory 경로에서 검증한다.
- 실제 삼성의 과거 cache 2건이 새로운 업로드 요구로 돌아가지 않게 하고, RO 운영 자료 대조에서 필수/제외/미확인 구분을 설명한다. source/provider 재실행·과거 seal 수정·운영 SQL/receipt 삭제는 금지한다.

## 2. [P2] 실제 배포 구성에 맞는 준비·조회·rollback 절차

FIX_14_REPORT의 기존 4절은 이력으로 남기고 새 보고서에 이를 대체하는 절차를 작성한다.

- 저장소 root Dockerfile의 worker target 또는 이전의 검증된 runtime overlay 절차를 사용한다. 같은 HEAD 위 미커밋 변경을 구별하는 source digest와 이미지 ID/release 식별을 제시한다. operations/private overlay·볼륨·그룹 권한을 유지하고 current가 새 이미지를 쓰는지 확인하는 절차를 포함한다. 비밀 값은 출력/커밋하지 않는다.
- 현재 `operations/worker-compose.sh`로 정확한 state/env를 전달하는 status/flush 명령을 제시한다. status의 RO completion=null과 flush 후 실제 remote-index completion=True를 구분한다. seal/종료 결과 없는 실패 run을 정상 백업 완료로 주장하지 않는다.
- rollback은 실행 lock/진행 여부를 확인하고 release/image를 복귀하되 timer 및 배치를 자동 기동하지 않는다. `up -d`는 기본 run을 실행하므로 단순 복귀 명령에서 제거한다. BC 재실행은 새 배포 검토 후 사용자의 명시 단독 실행 단계로 유지한다.
- 문서의 용량 조회 명령은 glob이 실제 컨테이너에서 확장되게 하고 RO mount를 사용한다. 기존 설치 이미지/도구를 우선 사용하며 환경에 없는 파일 경로·필수 옵션 누락은 실행 없이 정적 검증으로 걸러낸다.

## 3. [P2] DB 내용 동일성에 근거한 복구본 재사용과 실제 dry-run 목록

- 현재 worker DB 7305965568 bytes와 보고서가 지목한 과거 DB 7291777024 bytes는 이미 다르다. 코드 패치 배포라는 이유만으로 이전 DB를 현재 복구점으로 참조하지 않는다.
- Worker 비실행/lock 및 SQLite WAL 일관성을 포함하는 정식 snapshot 경계에서 내용 동일성을 검증한 경우에만 불변 복구본을 재사용한다. 다르면 현재 상태의 새 일관된 snapshot을 보존한다. worker/backup ledger snapshot 시점과 복구 목적을 설명하고 임의 시점 조합을 현재 rollback point로 표시하지 않는다. 약7GB 절약 때문에 최신 배치 결과의 복구 가능성을 희생하지 않는다.
- 실행 없는 dry-run으로 실제 release/복구본/의존성 인벤토리, 보호 집합, 정리 후보 또는 후보 없음, 예상 물리 확보량과 근거를 제시한다. 최신 두 디렉터리만 남기는 삭제나 공유층 합산으로 회수량을 과장하지 않는다. 삭제·대형 DB 복사는 이번 수정 검증의 필수 조건이 아니다.
- 다음 배포/수동 실행의 free 및 peak 관측 절차를 제시한다. 현재 native Worker/serving/MCP retention은 정상이라는 기존 결과를 보존한다. 전역 prune/다른 프로젝트 삭제는 금지한다.

## 검증·인계

관련 백업·CLI·BC 회귀 후 Worker/Core 요구 테스트와 ruff check/format check를 실행하고 정확한 명령·결과를 새 보고서에 기록한다. 현재 신규 테스트 파일은 format check 실패이므로 정리한다. 실제 코드와 다른 FIX_14_REPORT의 OCR 예제(prior.reuse_key를 variant ID로 사용)도 새 보고서에서 정정하며 과거 문서는 덮어쓰지 않는다. 운영 RO 대조와 로컬 fixture/전체 테스트를 구분한다. Reviewer 통과 전 새 운영 배포·BC 배치·timer 기동을 실행하지 않는다. 기존에 승인된 후속 배포/수동 검증 의도는 유지한다.
