# 019 FIX_34 — 배포본 서비스 그룹 보존 및 활성화 전 실제 계정 검증

2026-10-22 Reviewer. LIVE_REVIEW_14의13시 예약 실패 재발 방지. FIX_33까지의 코드/정책과 원본 서비스 오류 처리와 별개인 배포 권한 문제다.

- release 복제/준비 시 operations의 제한된 mode와 cardrag 그룹을 보존한다. private overlay/env를 world-readable로 만들지 않는다. 현재 active operations 그룹은 이미 cardrag로 복구했다.
- root 적용/활성화 절차는 timer 중지/포인터 전환 이전에 실제 systemd User=cardrag와 필요한 supplementary groups로 active 후보 helper config --quiet 실행을 검증해야 한다. 단순 root/lee 계정 test -x나 compose 검증으로 대체하지 않는다. 후보 경로 전체 탐색·helper 실행·private overlay/env 읽기 실패는 활성화를 중단한다. 실제 배치/provider 호출 없이 검증한다.
- 운영 helper는 현재 /opt/cardrag 아래 준비본에 있으므로 repo install-systemd 절차 및 운영 배포 인수 문서에도 gate를 반영하여 다음 복제 때 재발하지 않게 한다. 불필요한 새 Worker 이미지는 만들지 않는다.
- 최소 권한 변경, 실제 서비스 계정 config-only 통과, 의도적 잘못된 그룹 fixture 거부, shellcheck 및 timer/실제 volumes·다음 예약 보존을 검증하고 FIX_34_REPORT를 작성한다. 현시점 배치 end-to-end 복구는 사용자 sudo start 후 별도 확인한다.
