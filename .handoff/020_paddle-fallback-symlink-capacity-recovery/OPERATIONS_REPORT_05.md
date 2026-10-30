# 운영 기록 05: 하나카드 실패 회차 재개

- 사용자 요청에 따라 새 지정 실행 `cardrag-manual-hana-20261027T212144Z`에 SIGTERM을 전달했다. 종료 정리와 terminal-state reconciliation 완료 로그, 상태 exited 및 종료 코드 143을 확인했다. Paddle 자식도 종료됐다.
- 어제 실패한 run `9fa3b10def034fa8b65029e6df2d29ae`의 frozen issuer scope는 round_robin / hana이다. 해당 회차 산출물은 남아 있다. 사용자 요청에 따라 이 run ID를 `run --resume`으로 재개했다. 명시 resume은 discovery를 새로 수행하고 일치하는 완료 OCR 및 체크포인트를 검증해 재사용할 수 있다. 재사용 성공 건수는 실행 결과로 확인해야 한다.
- 새 컨테이너 `cardrag-resume-hana-20261027T221729Z`, 이미지 `sha256:943becf993a6cc8348e7a25526b5b5d6cce7a54385542bf8f6be4970965df93a`; 시작 메타데이터는 운영 디렉터리 `live/020-cardrag-resume-hana-20261027T221729Z.json`. 런처 `operations/resume-020-hana.sh`는 기존 시작 전 점검을 사용하며 shellcheck와 bash -n을 통과했다.
- 10:00 Asia/Seoul 예약은 사용자의 이전 명시적 요청에 따라 활성 유지했다. 재개 작업이 겹치면 잠금으로 예약 실행이 거부될 수 있으며 Persistent=no이다.
