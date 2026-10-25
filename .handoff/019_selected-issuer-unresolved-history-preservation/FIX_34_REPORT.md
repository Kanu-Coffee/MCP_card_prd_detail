# 019 FIX_34 REPORT — 실제 서비스 계정 배포 gate 및 매일14:30 운영 반영

2026-10-22 Executor. FIX_34와 사용자의 매일14:30 KST 변경 요청을 완료했다. 사용자 sudo 적용 후 실제 오늘 예약까지 확인했다. 14:30 배치는 아직 실행 전이며 배치 성공으로 선언하지 않는다.

## 변경 내용

- active operations의 그룹을 lee→cardrag로 복구했다. 디렉터리2750/helper0750/private overlay0640을 유지했고 공개 접근 권한을 확대하지 않았다.
- deploy/worker/verify-service-access.sh 추가. root에서 /usr/sbin/runuser -u cardrag로 실제 서비스 계정 및 docker supplementary group을 사용해 후보 helper의 config --quiet를 실행한다. 경로 탐색·실행, private overlay/env/compose 읽기를 검증하고 실패하면 nonzero다. 배치/provider/DB를 호출하지 않는다. root/lee 접근 성공으로 대체하지 않는다.
- install-systemd.sh는 어떤 unit/timer 변경보다 먼저 위 gate를 실행한다. active release operations/activate.sh도 timer/포인터 전환 전에 동일 gate를 실행한다. docs/OPERATIONS.md 및 deploy/README.md에 후보 복제 시 cardrag 그룹·제한 mode 보존과 실제 계정 검증을 필수 단계로 기록했다. 그룹을 잘못 복사하면 자동 활성화가 차단된다.
- repo와 active release timer를 매일14:30 Asia/Seoul/Persistent=false로 변경했다. docs의 현재 예약도14:30으로 갱신했다. 과거 handoff 문서와 기존13시 실패 증거는 보존했다.
- 운영 apply-fix34-1430.sh를 제공했다. root/현재release/image/오늘14:30전 조건·manual lock·실행 중 Worker 부재 확인 후 실제 서비스 계정 gate를 먼저 실행한다. 이후 timer disable, 설치, daemon-reload, 실패 상태 reset, enable --now 및 배포 metadata 갱신을 수행한다. 지금 배치는 시작하지 않는다.
- sudo 인증이 없어 사용자에게 복붙 한 줄을 제공했고 사용자가 실행 완료를 통보했다. 실제 installed timer/current/helper 상태를 재확인했다.

## 검증과 명령

- 실제 /usr/sbin/runuser와 host /etc/group, 실제 Docker CLI/Compose plugin 및 실제 active release/private overlay/env를 readonly mount한 격리 컨테이너에서 root gate 실행: Service account config check passed. 실제 UID10001/cardrag와 docker 그룹의 compose config-only 성공이며 원본/provider 호출은 없다.
- 별도 제한 권한 fixture의 operations를 lee:lee/0750으로 설정: 실제 gate exit1로 거부. 동일 fixture의 그룹만 cardrag로 변경: gate exit0. private overlay0640 유지. 재현 fixture /tmp/cardrag-fix34-access-viryo6_2, 경로기록 /tmp/fix34-access-fixture-path. 실제 비밀 overlay가 있어 그룹 제한을 유지한다.
- shellcheck deploy/worker/verify-service-access.sh deploy/worker/install-systemd.sh /opt/cardrag/current/operations/activate.sh /opt/cardrag/current/operations/apply-fix34-1430.sh 통과. SC2016은 child shell에서 의도적으로 변수 평가하는 블록에 한해 사유를 적고 제한적으로 제외했다.
- systemd-analyze verify deploy/worker/cardrag-worker.service deploy/worker/cardrag-worker.timer 통과.
- systemd-analyze calendar --iterations=2 '*-*-* 14:30:00 Asia/Seoul': 오늘10/22 및 내일10/23 14:30 확인.
- git diff --check 통과. Worker/Core 코드는 변경하지 않아 이미지 재생성·전체 Python 테스트 반복은 하지 않았다. FIX_33의1866 통과 증거는 그 코드 검증으로 유지하고 이번 배포 권한 검증과 구분한다.
- 검증 결과와 helper/timer SHA는 active operations/fix34-verified.json에 보존했다.

## 운영 적용 확인

2026-10-22 14:15 KST 확인:

- /opt/cardrag/current → /opt/cardrag/019-fix33-7d2e39edbf59 유지. Worker image ID sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450 유지. 새 이미지를 만들지 않았다.
- installed /etc/systemd/system/cardrag-worker.timer == active release timer(byte 일치).
- timer ActiveState=active, UnitFileState=enabled, Persistent=no, NextElapseUSecRealtime=Thu 2026-10-22 14:30:00 KST.
- service ActiveState=inactive, SubState=dead, Result=success는 실패 상태 초기화 후 대기 상태이며 실제 배치 성공을 뜻하지 않는다. Worker container 없음.
- deployment fix34_schedule_applied_at=2026-10-22T05:15:09.389214+00:00, service_account_config_check=passed.
- 오늘13시203/EXEC는 배치 전 실패라 새 회차를 소비하지 않았다. 오늘14:30은 우리2회차, 내일14:30은 국민 배정이다. 3회 허용/4회 차단 정책 유지. 실제 state/serving/auth/OCR 모델 볼륨, cursor/DB/원장/보호 snapshot은 수정하지 않았다.

14:30 이후 journalctl -u cardrag-worker.service --since '2026-10-22 14:30:00' -n 100 -f --no-pager로 시작·진행·최종 결과와 백업을 확인한다. 원본 카드사 서비스 오류가 지속되면 권한 수정과 별개로 Worker가 실패할 수 있다.
