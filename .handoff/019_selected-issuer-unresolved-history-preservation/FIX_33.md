# 019 FIX_33 — 저장 terminal 결과 반환 시 CLI 이중 오류 출력 제거

2026-10-22 Codex, Reviewer. LIVE_REVIEW_13의12시 예약 결과에서 확인한 P2다. run_command는 stored failed/interrupted/blocked payload를 먼저 출력하고 typer.Exit(1)을 raise하지만, 같은 try의 generic except Exception이 그 정상 종료 예외를 잡아 추가 worker_unexpected_failure JSON을 출력한다. 오늘12시 journal 및 외부 호출 없는 mock CliRunner에서 동일 재현됐다.

## 필요한 수정

- cli.py의 의도된 typer/click 종료 예외가 generic runtime failure 처리로 들어가지 않게 최소 수정한다. terminal payload는1회만 출력하고 nonzero exit를 유지한다. 성공/no_change와 실제 unexpected Exception 진단은 유지한다.
- 실패를 exit0으로 바꾸거나 이유를 숨기지 않는다. partial run/차단·취소·signal 정책 등 인접 경로에 영향이 없는지 확인한다. 오늘 rotation terminal result 재사용 정책이나 DB/cursor·timer/명시 resume 계약은 변경하지 않는다.
- persisted result 반환이 새 HTTP retry/새 run 실행으로 오인되지 않도록 운영 설명을 보완한다. OPERATIONS_REPORT_11은 역사 기록으로 보존하고 최신 인수 문서에 당일 재호출의 실제 의미를 반영한다.

## 검증

_run_with_signal_shutdown이 failed/interrupted/blocked 결과를 반환하는 fixture에서 stdout이 각각 단일 JSON으로 decode되고 원래 reason/run_id와 exit1을 유지하는지 검증한다. succeeded/no_change exit0, 실제 예외 시 단일 generic/typed 오류, 기존 CLI signal/error 회귀를 확인한다. provider·원본·실제 Worker 배치를 호출하지 않는다.

FIX_33_REPORT.md에 구현/관련 검사/ruff/diff 및 배포 여부를 기록한다. 이 문제는 새 source 배치 실패가 아니라 종료 출력의 P2이므로 내일12시 국민 배정이나 기존 live를 변경할 필요가 없다. actual image에 반영할 경우 식별 가능한 운영 배포 절차를 따르며 무단 순회 초기화/전체8사 실행·재개는 하지 않는다.
