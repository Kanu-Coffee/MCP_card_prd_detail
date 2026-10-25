# 019 FIX_21 — 빈 index와 restore 읽기 실패의 판정 보완

2026-10-21 Reviewer: Codex. REVIEW_09의 로컬 재현 두 건을 수정한다. PLAN/PLAN_AMENDMENT_01, FIX_20/REPORT, REVIEW_09와 현재 Git 상태를 읽고 이 가장 높은 미보고 FIX를 수행한다. 작은 백업 경계 보완이므로 019를 유지하며 이전 문서를 덮어쓰지 않는다. 결과는 FIX_21_REPORT.md로 신규 작성한다.

## 1. [P2] HTTP200 빈 body는 부재가 아니라 읽을 수 없는 index

backup.py `_read_backup_index`의 None/빈 bytes 공통 absent 판정을 분리한다. 실제 WebDAVClient는 HTTP404→None, HTTP200 빈 body→b''이므로 빈 bytes/bytearray는 malformed/invalid_json 등 명시적 unreadable로 처리한다. commit merge는 빈 body에서도 기존 index와 batch manifest를 게시하지 않고 오류와 pending 보존을 유지한다. no-pending completion은 true로 바꾸지 않는다.

부재는 명시적 404/None 계약으로 한정한다. fake가 부재를 b''로 반환한다면 fake를 None/404 계약에 맞게 보완하고, 이를 이유로 실제 HTTP200 빈 index를 새 index로 오인하게 하지 않는다. 정상 404의 최초 index 생성은 유지한다. 일반 유효 빈 index(items=[])와 손상된 zero-byte body는 구별한다.

검증: 실제 Worker WebDAVClient facade/로컬 core stub을 통해 HTTP200 zero-byte 및 HTTP404를 구별하고 64MiB max_bytes 전달도 확인한다. zero-byte reader, no-pending completion, pending 있는 commit 금지·index/batch PUT0·pending/기존 원격 bytes 보존, 404 최초 생성, 유효 items=[] 처리를 최소 fixture로 검사한다. 운영 파일을 손상시키는 재현은 금지한다.

## 2. [P2] 새 호스트 restore의 index 조회 장애를 empty로 숨기지 않음

receipt 없는 restore discovery에서 `_read_backup_index`가 unreadable이면 `status=empty` 분기로 흘리지 않는다. typed index_read_error와 구체적인 실패 상태/제한된 error를 외부 반환에 보존한다. HTTP403/인증, 연결, 새 한도 초과, invalid/빈 JSON을 실제 부재·정상 빈 index와 구별한다. 해당 실패는 CLI에서도 비정상 종료로 전달하여 복구 자동화가 정상 empty로 오인하지 않게 한다. 기존 실제 empty/정상 복원 결과는 유지한다.

실패/부재/정상 모든 조기 반환에서 recovery lock과 소유 client를 확실히 정리한다. injected client의 소유권을 침범하지 않는다. 불필요한 전역 restore/CLI 재설계는 하지 않는다.

검증: receipt0 + 403/transport/size_limit/invalid_json(빈 body 포함)에서 실패 상태·typed error·CLI nonzero, 대상 파일/receipt 쓰기0, lock 해제/소유 client close를 확인한다. 404와 유효 items=[]는 정상 empty, >8MiB 정상 index discovery/복원은 유지한다. 후속 정상 재시도가 lock에 막히지 않아야 한다.

## 검증 및 후속 경계

기존 FIX_20 64MiB bounded read/공통8MiB 유지/remote-only merge 보존/정상 index completion/일반 읽기 오류 시 index 보존과 FIX_18–19 cache 경계·metadata pair 회귀를 유지한다. 관련 테스트 뒤 요구 Worker/Core 검사와 ruff, diff check를 실행하고 명령·결과·변경 범위·미검증 운영을 보고서에 기록한다.

이번은 fixture/코드 보완이다. 기존 승인된 운영 작업은 후속 리뷰 통과한 실제 소스로 이어가며, 부정 경계 확인을 위해 원격 index/receipt/seal/운영 DB를 조작하지 않는다. 현대/BC completion=true는 실제 배포 후 정식 CLI와 committed index로 확인하고, 롯데/신한 완주나 timer/8사 인수를 앞당겨 선언하지 않는다.
