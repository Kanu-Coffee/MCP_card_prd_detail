# 019 FIX_29 — 복구점 검증기의 필수 DB·offline WAL 조건 검증

2026-10-22 Codex, Reviewer. FIX_28/REPORT와 신규 verify-recovery-point.py를 검토하고 운영 데이터를 쓰지 않는 임시 fixture로 아래 P2 두 건을 재현했다. 현재 정상 snapshot이나10시 배치에 결손이 발견된 것은 아니다.

## P2-1 — 필수 backup-ledger DB가 없어도 verified=true

현재 verify()는 evidence.databases에 실린 항목만 순회하고 별도로 worker-state만 읽는다. worker-state만 기재한 일관된 evidence/guidance hash fixture에서는 backup-ledger 파일이 실제 없어도 verified=true다. 따라서 문서가 약속하는 두 DB 완전성을 검증하지 못한다.

evidence.databases가 정확한 필수 파일 worker-state.sqlite3와 backup-ledger.sqlite3를 포함하는지 검증한다. 적절한 고정 집합을 사용해 임의 상대경로/절대경로 항목을 읽지 않는다. 각 항목 size/hash 자료형·범위와 파일 정규성/비symlink/경로를 검사하고, 누락·잘못된 형식은 검증 실패와 nonzero 종료로 보고한다. 파일을 새로 만들거나 누락 항목을 보정해 성공시키지 않는다.

## P2-2 — immutable 검사 전에 nonempty WAL을 거부하지 않음

현재 verify()는 mode=ro&immutable=1로 main DB만 검사하며 snapshot의 WAL 존재/크기를 검사하지 않는다. 두 DB hash가 유효한 임시 fixture에 nonempty worker-state.sqlite3-wal 29bytes를 추가해도 verified=true였다. 실제 미checkpoint WAL이 있으면 main DB SHA/quick_check가 통과해도 snapshot의 최종 상태 검증이 아니다.

각 snapshot DB의 -wal이 없거나0bytes인지 immutable 검사를 시작하기 전에 확인한다. nonempty WAL·비정상 sidecar는 fail closed한다. snapshot DB/sidecar를 열어 checkpoint하거나 삭제/절단하지 않는다. 정상0bytes WAL과 SHM의 기존 읽기 검사 호환성은 유지하고, 검증 중 상태 변경이 감지되면 중단하는 방법을 최소 범위로 적용한다. live Worker DB 자체를 immutable로 검사하는 도구로 확대하지 않는다.

## 범위와 검증

- deploy/worker/verify-recovery-point.py 및 필요한 테스트/운영 복사본만 보완한다. active/rollback 양쪽 파일의 바이트 일치를 유지한다. 새 Worker image·실제 rollback/restore·snapshot 재생성·timer 변경은 필요 없다.
- 임시 fixture에서 정상 두DB, 원장 누락/worker 누락/metadata 필수 항목 누락, 경로 이탈/symlink, 크기/hash 손상, nonempty WAL(각DB),0bytes WAL, 정상 latest-run 증거를 검증한다. 잘못된 입력이 traceback만 출력하지 않고 verified=false/nonzero로 끝나는지도 확인한다.
- 실제 protected snapshot은 읽기 전용으로 재검증한다. 현재 운영 DB·단일 복구본·예약10시·프록시 해제를 유지하고 외부 provider/배치를 시작하지 않는다.
- FIX_29_REPORT.md에 변경/검사 결과/운영 복사본 일치와 미실행 범위를 기록한다. FIX_28의 안내 경로 보완 자체는 유지하고 원래 역사 metadata를 덮어쓰지 않는다.
