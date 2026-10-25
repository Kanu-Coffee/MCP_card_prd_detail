# v1.0.37 CI補完 — installed helper 경로 fixture

2026-10-25 Executor. PR48 CI40192053214의보안·lint·strict type·전체테스트는통과했으나systemd-analyze verify가CI에없는운영전용/opt/cardrag/current/operations/worker-compose.sh경로를거부했다. 운영서버권한문제의재발이아니고빈CI호스트의unit검증fixture부족이다.

CI의Compose계약검증후unit parsing직전에/usr/bin/true를해당절대경로의실행가능fixture로설치한다. unit검사·실제운영계정검증gate를끄지않고런타임스크립트/비밀설정을CI에복사하지않는다. Worker는실행하지않는다. actionlint/shellcheck통과후수정commit으로원격CI를다시실행한다. 실행결과는최종릴리스보고에기록한다. 운영이미지/설정/timer는변경하지않았다.
