# 019 FIX_25 — 예약 Worker와 검증된 수동 운영 실행 경로 일치

2026-10-22 Codex, Reviewer. LIVE_REVIEW_11에 따라 발행한다. 수동8사는 완료됐으나 재부팅 후 enabled/Persistent timer가 재활성화되어06:32 예약 실행이 다른 candidate image로 실패했다. 정확한 내부 오류는 미확정이지만 service와 검증된 수동 경로의 불일치는 확인됐다. 현재019 안의 운영 연결 보완이며 provider/수집/백업 설계 변경은 아니다.

## 착수 확인

현재 Git/적용 지침, PLAN/AMENDMENT, FIX_24/REPORT/REVIEW_12, OPERATIONS_REPORT_10, LIVE_REVIEW_10–11을 읽는다. `/etc/systemd/system/cardrag-worker.service`/timer, repo deploy/worker의 원본, 활성 `/opt/cardrag/current`, operations/worker-compose.sh 및 private runtime overlay의 실제 resolved compose를 비교한다. 민감한 env/proxy/credential 값은 출력하거나 handoff에 넣지 않는다.

## 필요한 수정

1. 원래 예약 경로의 image/project/state/auth/models/serving/secret mounts·UID·환경·scope schedule을 식별하고 수동 검증 경로와 차이를 설명한다. journal generic failure만으로 IP·인증·OCR 원인을 단정하지 않는다.
2. 예약 service가 활성 release의 검증된 image·project·실제 운영 volume·필수 runtime 설정을 사용하도록 기존 helper/배포 절차를 최소 수정한다. private overlay/비밀 경로를 저장소에 복사하지 않는다. cwd/symlink 물리경로와 release 교체를 지원하고 수동 issuer 고정값을 예약 명령에 넣지 않는다. 기존 예약 순회 정책/lock·취소·stop semantics를 유지한다.
3. 수동 검증 기간의 timer 중지가 재부팅 후 자동 재개되는 문제를 해결한다. 영속 enable/disable 상태를 명시하고 검증/설정 교정 중 중복 또는 Persistent catch-up이 발생하지 않게 한다. 이번 FIX만으로 전체 실배치를 자동 시작하거나 원격 업로드·DB 교정·롤백하지 않는다. 현재 정상 신한 run과 serving을 보존한다.
4. repo service template/문서와 실제 적용 절차를 일치시킨다. 예약 자동운영의 재활성화는 코드/설정 검증과 사용자 운영 전환 범위를 확인한 뒤 진행하며, 재활성화 시 즉시 catch-up 여부·첫 예정 시각을 정확히 보고한다.

## 검증

systemd-analyze verify 등 사용 가능한 정적 검사, shell 수정 시 shellcheck 또는 적절한 문법 검사, resolved compose의 image/project/volume/필수 flags 비교를 수행한다. 실제 secret은 boolean/identity hash 등으로만 확인한다. 설정 변경 전후 동일 신한 succeeded run/stable generation 및 Worker 미실행을 확인한다. 예약 명령 검증을 빌미로 전체 worker run을 실행하지 않는다.

현재 구현 상태에 맞는 필요한 회귀만 실행하고 FIX_25_REPORT에 명령·결과·systemd 적용 여부·실제 image/volume 식별·timer 지속 상태·미검증 사항을 남긴다. 원래 실패 재현/정확한 원인/자동배치 성공을 확인하지 않았다면 각각 미확정/미실행으로 적는다. 기존 handoff/배포 복구본을 덮어쓰거나 운영 DB를 수작업 수정하지 않는다.
