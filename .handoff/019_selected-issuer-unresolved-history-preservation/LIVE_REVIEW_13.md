# 019 LIVE_REVIEW_13 — 12시 실행은 당일 저장 실패 반환, 내일12시 국민 배정 예정

2026-10-22 Codex. 사용자 요청에 따라12시 실행의 journal/systemd/실제 rotation DB와 MCP를 확인했다. 배치 재개·DB/cursor/설정/타이머 변경은 하지 않았다.

## 12시 결과

- active release `/opt/cardrag/019-fix32-55c579b986cb`, image `cardrag-worker:019-fix32-55c579b986cb`. FIX_30–32 수정은 배포돼 있다.
- service12:00:00 시작/12:00:22 exit1. startup capacity/OCR resolver/tokenizer/Qwen/embedding preflight 통과 후 pipeline entry에서 즉시 종료했다.
- 첫 반환은 기존 run `e81c17e69b5f428f8d6314ab2b8762d4`의 status failed/reason rotation_execution_failed다. 이는 오늘10시 slot의 저장 결과다. 새12시 run이나 새 discovery/PDF 다운로드 시도는 없으며, 새 bounded origin retry가 실동작한 증거도 아니다. startup의 provider preflight는 실제 수행됐으므로 모든 외부 호출이0이었다는 뜻은 아니다.
- 실제 DB latest run은 여전히10:00 시작/10:03 종료의 우리 failed run이다. 새 run을 만들거나 기록을 덮지 않았다. OPERATIONS_REPORT_11의 "12시 새 bounded retry 가능" 예측은 당일 terminal result 반환 경계를 반영하지 못했으며 실제 관측으로 정정한다.

## 내일 순회와 live

timer는 daily12:00 Asia/Seoul, active/enabled/Persistent=no, next **2026-10-23 12:00 KST**다. actual mode=round_robin/channel=stable/pool8사 유지.

issuer_rotation_day의2026-10-22는 woori/동일 run/failed이고 remaining queue는 kb→shinhan→samsung→hyundai→hana→lotte→bc다. 따라서 현재 설정과 운영 상태를 유지하면 **내일12시 다음 배정은 국민카드**다. 오늘 우리 실패 때문에 다음날도 우리부터 반복하는 정책은 아니다. 국민의 실행 성공 자체를 미리 보장하지 않으며, 우리 복구는 원인 대응 뒤 별도 명시 resume 대상이다. failed service의 과거 결과와 계속 active인 timer를 구분한다.

운영 state WAL0을 확인한 뒤 snapshot이 아닌 live DB를 이번에는 Worker 미실행/오프라인 조건에서 read-only immutable로 읽었다. 최초 일반 mode=ro는 sidecar 열기 오류로 실패했고 journal이나 WAL을 삭제/절단하지 않았다. MCP source page2건 성공, current=Worker stable `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`/기존5632건 유지.

## 별도 P2 — CLI의 의도된 exit를 unexpected runtime으로 재분류

journal에는 저장 실패 JSON 직후 두 번째 worker_unexpected_failure JSON도 출력된다. cli.py run_command에서 `_echo(payload)` 후 `raise typer.Exit(code=1)`가 같은 try문의 `except Exception`에 잡히기 때문이다. 외부 호출 없는 CliRunner+_run_with_signal_shutdown mock으로 실제 동일2개 JSON/exit1을 재현했다. 이는 새로운 pipeline 오류가 아니라 의도한 terminal 종료의 잘못된 이중 진단이다. exit nonzero와 다음날 queue는 정상 유지된다.

FIX_33으로 control-flow 예외 전파 및 출력 회귀 보완을 발행한다. 오늘/내일 slot을 지우거나 우리를 자동 재실행시키는 변경은 아니다. 이번 점검에서 코드·운영 설정은 수정하지 않았다.
