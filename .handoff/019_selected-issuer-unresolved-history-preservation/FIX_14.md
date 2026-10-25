# 019 FIX_14 — BC sealed OCR 재사용, 삼성 백업 범위, 복구본 보존 기준

2026-10-20 Reviewer: Codex. **019를 유지한다.** 새 기능/전면 설계 변경보다 기존 공통 재사용 경로와 백업 제외 정책의 국소 보완이며 저장 공간은 운영 보존 기준으로 다룰 수 있다. 운영 사실과 재현은 LIVE_REVIEW_04.md 참조. 기존 PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–13/REPORT, 운영 리뷰와 현재 Git diff/status를 읽고 가장 높은 미보고 FIX인 본 문서를 수행한다. HEAD `aaeb2c4` 위 기존 미커밋 변경과 과거 문서를 보존한다.

## 1. [P1] native/adopted sealed OCR의 잘못된 variant ID

위치: `apps/cardrag-worker/src/cardrag_worker/ocr.py:OCRResolver._lookup_prior_local_sealed_ocr` 및 관련 테스트.

실제 운영 BC run `c975974d00404b0ba74779f9547021ee`는 정상 수집/다운로드 후 동일 PDF의 이전 sealed OCR 재사용에서 `ValueError: OCR variant ID requires content cache kind`로 실패했다. 이 함수는 cache_kind와 관계없이 variant ID를 전달한다. 이전 native manifest가 없어서 strict native materialization 대신 sealed OCR fallback을 쓰는 조건이 필요하다. LIVE_REVIEW_04의 실제 PDF/prior 정보로 network-none/RO 재현 완료.

요구:

- `OCRResult`의 형식 계약에 맞게 content에만 content variant ID를 부여한다. native/adopted의 key/kind와 기존 sealed 바이트 정체성을 보존한다. 정상 OCR을 content로 무조건 재분류하거나 검증을 약화시켜 우회하지 않는다.
- manifest 없는 native/adopted prior, 정상 content prior(variant 유지/정당한 fallback), document_id는 바뀌고 PDF/OCR identity는 같은 경우를 회귀 검증한다. source/retained hash 불일치·손상·symlink 등의 기존 거부 계약도 유지한다.
- 생성자 단위 테스트만 두지 말고 resolver/fallback의 실제 호출 경로에서 성공 및 provider 미호출을 검증한다. 원본 오류를 잡고 provider에 재요청하는 방식으로 덮지 않는다.
- 실제 BC 실패 자료의 읽기 전용 대조로 수정된 경로가 동일 OCR을 반환하는지 확인한다. 보고서에는 제품 코드 테스트와 실제 운영 RO 검증을 구분한다.

## 2. [P2] content 백업 제외 정책과 과거 native doc_map 진단의 충돌

위치: `backup.py:BackupLedger.scoped_backup_scope`의 tagged ledger Phase 1/2, 백업 CLI/관련 회귀.

삼성 run `44d55df9db8645c8b9060dc4631ff463`: pending0, bound receipt2085, unbound2. 현재 문서는 content인데 과거 native key `a7745ec9…55d206`의 doc_map 두 경로가 required/unbound로 들어온다. current content key `8cf30f1b…7e3501`, PDF/OCR identity는 옛 doc_info와 같다. 로컬 native manifest는 없고 원격 receipt는 있다. 이는 새 업로드 대기 2건이 아니다.

요구:

- 현재 검증된 seal/산출물과 기존 문서 소유 백업 정책에 따라 현재 필수 metadata와 과거 shared cache 태그를 일관되게 분류한다. content/adopted라면 무조건 모든 태그를 무시하는 방식도 금지한다. 현재 run의 검증된 native bundle, generation-only metadata 및 여전히 실제 소유/필수인 항목을 보호한다.
- 정상 정책상 제외라면 scope complete를 차단하지 않게 하고 제외 근거/카운트를 관측할 수 있게 한다. shared cache 복제를 새로 강제하거나 과거 receipt/doc_map을 삭제해서 숫자만 없애지 않는다. 현 정책의 정상 pending/out-of-scope도 보존한다.
- 같은 PDF/OCR의 native→content 전환과 오래된 doc_map/receipt만 있는 경우, content run 안에 검증된 native bundle이 있는 경우, 현재 native 필수 증거 소실, 변조 doc_info, stale revision, 다른 issuer/root, retry를 검사한다. metadata 문자열/receipt 존재만으로 바이트 검증 권위를 부여하지 않는다.
- 실제 삼성 run에서 status/flush의 정식 scope 계산이 같은 분류를 사용함을 확인한다. 수정 후 읽기 전용 진단을 먼저 제시하고, 운영 쓰기 없이 완료를 증명할 수 없는 부분은 명확히 남긴다.

## 3. [P2] 배포 복구본 누적을 막는 작은 운영 보완

현재 free 약76GiB, Worker/serving/MCP native retention은 동작 중이다. MCP의 3세대는 명시 설정값이다. 약20.5GiB의 `deployment-backups`에는 별도 상한/정리 기준이 없고 최신 rollback 자료는 이전 디렉터리 DB를 참조한다.

요구:

- 기존 운영 문서/배포 절차 안에 용도별 용량 조회, 복구 단위 목록, 외부 디렉터리 의존성, 현재 및 직전 검증된 rollback 보호, 불변 DB 복구본 재사용 기준, 정리 후보의 dry-run/실제 확보 예상량을 정리한다. 가장 작은 방법을 택하고 범용 GC 서비스를 새로 만들지 않는다.
- 삭제 후보가 보호된 복구 단위의 의존성이면 제외한다. 최신 디렉터리 두 개만 남기는 정책은 안 된다. 실제 삭제가 필요하면 대상·근거·보호 집합을 구체화한 후 사용자 요청 범위에서 수행하고 결과를 기록한다. 이번 필수 인수는 안전한 정책/후보 검증까지이며 용량 확보를 위해 삭제를 강제하지 않는다.
- 전역 `docker system prune`/volume prune, 다른 프로젝트 자원 삭제, 보존된 seal/DB 수동 수정 금지. 이미지 표시 용량을 합산해 회수량을 과장하지 않는다.
- 다음 배포가 같은 worker DB를 약7GB씩 또 복사하지 않도록 한다. 다음 실행 전/후 free·주요 volume·복구본·최대 임시 사용량을 기록한다. 최소2GiB gate를 충분한 실행 공간의 증명으로 보고하지 않는다. 전체 export/DB/벡터 형식 재설계는 별도 필요성이 확인될 때만 새 계획 대상으로 제안한다.

## 검증과 운영 인계

1. BC 재사용 회귀, 삼성 백업 정책/CLI 회귀, 기존 FIX_06–13 백업 무결성/variant/복구 테스트를 실행한다. 이후 PLAN의 Worker 전체 및 관련 Core 테스트, ruff check/format check를 수행하고 실제 명령·결과를 FIX_14_REPORT.md에 기록한다.
2. 운영 RO 비교에서 BC 같은 OCR SHA 유지·provider 미호출, 삼성의 정상 제외/진짜 필수 항목 구분, 비선택 7사와 8사 serving5543 유지 근거를 제시한다. 테스트 fixture에 대형 운영 DB/비밀 값을 넣지 않는다.
3. Reviewer 통과 후 기존 사용자 승인 범위에서 식별 가능한 새 release/image와 rollback을 준비한다. BC는 현재 실패 이미지를 그대로 재시도하지 않는다. 사용자가 복붙하여 BC를 재실행하는 절차와 status/backup 검증을 제공한다. 기존 실패 run과 성공 serving의 증거를 보호한다. OCR cache/receipt/DB를 ad-hoc 조작해 통과시키지 않는다.
4. BC 성공과 삼성 scope 진단을 확인하기 전 019/전체 8사 검증 완료 또는 timer 재개로 판정하지 않는다. 이번 수정 후에도 나머지 카드사 수동 검증은 별도 남는다.
