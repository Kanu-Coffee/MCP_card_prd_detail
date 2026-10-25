# 019 FIX_30 — 우리 PDF origin HTML 오류 대응 및 자동 순회 실패 진단·복구

2026-10-22 Codex, Reviewer. LIVE_REVIEW_12의 첫10시 자동 우리 배치 실패에 따라 발행한다. 현재 live는 기존 신한 generation/5632건으로 정상이며 오늘 자동 slot은 우리 실패로 저장됐다. 이번 지시는 검토 후 수정/검증을 위한 인계이며 무조건 배치를 반복 실행하라는 승인이 아니다.

## 목표 및 근거

예약 서비스는 정상 기동했고 우리 목록922건까지 통과했다. 실제 원인 경로는 WooriAdapter의 RAONK PDF POST가 HTTP200/text/html의 서비스 오류 페이지를 반환하여 PDFValidationError→issuer_source_invalid→선택사 전체 quarantine→generic worker_unexpected_failure로 바뀐 것이다. 특정 문서/최신 다른 상품, 조건부 요청 유무/landing 세션 보완에서도 재현됐다. source URL 직접 GET의 안전한 redirect 추적도 같은 MIME 오류다.

공식 파일 origin의 일시 장애인지 request 프로토콜/정책 변화인지는 아직 확정하지 못했다. 원인에 맞는 최소 transport 대응과 진단을 보완하고, 복구 가능한 시점에 우리만 명시 재개하여 live/자동순회를 보존한다. 시스템 전체 OCR/백업/이미지 재설계를 하지 않는다.

## 착수·관련 파일

현재 Git·적용 지침, PLAN/AMENDMENT, FIX_25–29/REPORT/REVIEW_13–16, LIVE_REVIEW_12를 읽는다. run `e81c17e69b5f428f8d6314ab2b8762d4`, failed source `source_c363707aef7be75165e225bf96369ac88cd7784f3393116411535400083a3c0f` 및 정상보존 자료를 재확인한다. source/product ID는 관측 증거이지 예외 조건으로 하드코딩하지 않는다.

주요 파일: issuers/woori.py, downloader.py, issuer_collection.py, pipeline.py, cli.py, rotation.py(재개 결과 정합성 확인에 한정), 관련 issuer/downloader/scope/pipeline 테스트 및 docs/OPERATIONS.md. 현재 Worker state에는 nonempty WAL이 있으므로 production SQL은 정상 mode=ro로 읽고 immutable main-only로 최신 상태를 추측하지 않는다.

## 구현 지침

1. **공식 다운로드 경로를 최소한으로 재확인한다.** 공식 페이지가 현재 사용하는 RAONK 경로·payload encoding/헤더·세션 요구를 기존 구현과 비교한다. 제한된 대표 문서로 재현하고 origin의 transient outage이면 이를 명시한다. 직접URL/다른 transport가 정상 PDF를 반환한다고 검증하기 전 우회 fallback을 추가하지 않는다. 임의 User-Agent/proxy/브라우저 인프라를 무차별 시도하거나 전체922건을 진단 목적으로 재호출하지 않는다. 신규 browser/VPN 기반 우회가 필요할 정도면 현재 소규모 FIX에서 확대하지 말고 별도 계획 필요성을 보고한다.
2. **전송 오류와 문서 손상을 구별한다.** HTTP200 HTML 서비스 오류를 PDF로 인정하지 않는다. 안정적으로 확인 가능한 origin service-error 신호는 안전한 typed reason과 제한된 retry/backoff 정책으로 구별할 수 있으나 HTML의 임의 문구를 무조건 transient로 보거나 모든 validation 실패를 재시도하지 않는다. 인증/보안/DNS/redirect/DRM/ZIP/파일무결성 검증을 약화하지 않는다. 실제 origin이 아직 실패하면 failed/degraded를 정직하게 유지한다.
3. **운영 진단을 보강한다.** 문제의 source_id/issuer·stage·allowlisted validation reason/HTTP status/MIME 등 원문 없는 제한 진단이 collection/worker 보고서까지 이어지게 한다. all selected issuer collection failed를 generic unexpected runtime만으로 숨기지 않도록 기존 typed 실패 계약을 활용한다. request form/encrypted k01·cookie·인증·임의 response body/예외 text는 저장하지 않는다. PDF completed922에는 skipped897이 포함된다는 점도 운영 summary에서 구분 가능하게 한다.
4. **이전 정상본을 보호한다.** origin 재검증 실패를 새 PDF/단종/삭제로 처리하지 않는다. 성공24건과 기존 캐시·serving·미선택7사의 자료를 유지하며 freshness/audit를 정상 검증 없이 갱신하지 않는다. quarantine/현재 범위 보존을 무력화해 빈/부분 게시를 강제하지 않는다. 기존90일/방문8일 사전 감사창을 우회하려고 변경하지 않는다.
5. **명시 재개의 의미를 고정한다.** 현재 failed run의 우리 범위를 보존한 공식 resume를 준비한다. 같은 날짜 기본 round-robin 재호출은 저장 실패를 반환하므로 단순 systemctl start 반복을 해결책으로 안내하지 않는다. cursor/day/result를 SQL로 지우거나 성공으로 고치지 않는다. 명시 resume 후 동일 run 상태와 원래 day result의 정합성을 확인하고, 필요한 갱신은 기존 검증된 애플리케이션 경로로만 수행한다. 현재 큐의 다음사 국민 및 KST10시 예약을 임의로 되돌리거나 전체8사 실행으로 바꾸지 않는다.

## 검증과 완료 기준

- 재현 fixture: HTTP200 HTML service error/정상 PDF/잘못된 PDF signature/손상 구조/304 조건부·비조건부/보안 redirect/DRM/ZIP을 구분한다. bounded retry와 종료, 원문 비노출, 모든 선택사 실패 시 구체적인 typed 보고, 기존 live 보존을 검증한다.
- 실제 origin이 회복됐거나 교정된 protocol이 작동하면 대표 실패 문서와 다른 최신 정상 문서의 완전한 PDF/구조를 검증한다. prefix 표본이나 홈페이지200만으로 해결 완료를 선언하지 않는다. 복구 불가한 외부 장애를 숨기는 코드 변경은 하지 않는다.
- 같은 날 실패 slot 재호출/명시 resume/다음 날짜 다음사 배정, 재부팅에 준하는 DB reopen, 비선택 미호출/보존을 검사한다. 수동8사 검증 완료와 자동8일 실제 순회 완료를 구분한다.
- 관련 테스트 후 변경 범위에 맞는 Worker/Core 회귀·ruff/diff 검사를 수행한다. 새 Worker Python을 배포해야 한다면 식별 가능한 image가 필요하나 예약 설정만의 문제로 오인해 이미지를 계속 바꾸지 않는다. 실제 원인이 외부 서비스면 먼저 기다림/재검증의 근거를 남긴다.
- FIX_30_REPORT.md에 원인 확정/미확정, 변경, fixture/실제 HTTP 결과, 배포·재개 시행 여부, 현재 serving/backup/rotation/timer를 정확히 기록한다. 구현 검증과 실제 배치 완주는 별도다. 운영 재개를 수행할 때는 최신 복구점·lock·용량·원인 해결을 확인하고 기존 사용자 실행 권한 범위 안에서 진행한다.
