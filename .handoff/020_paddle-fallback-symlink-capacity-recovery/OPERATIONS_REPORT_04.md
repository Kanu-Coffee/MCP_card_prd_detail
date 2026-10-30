# 020 하나카드 수동 지정 실행 시작

2026-10-28 06:21 KST. 사용자는 전일 실패한 하나카드를 지정 실행하고, 모니터링은 직접 한 뒤 종료 시 알려주겠다고 요청했다. 10:00 timer와 겹칠 수 있어 중지를 안내했으나 사용자가 명시적으로 **예약을 켠 상태로 진행**하도록 지시했다.

- active release `/opt/cardrag/020-paddle-20261028`, image `cardrag-worker:020-paddle-2bfed5c195e3`, image ID `sha256:943becf993a6cc8348e7a25526b5b5d6cce7a54385542bf8f6be4970965df93a`.
- `worker run --issuer hana`를 detached container `cardrag-manual-hana-20261027T212144Z`로 시작했다. 이 지정 실행은 자동 round-robin cursor를 소비하지 않는다.
- container start 2026-10-28 06:21:44 KST, 초기 확인 `running`, 기동 capacity preflight 통과. 배치 완주·게시·backup은 아직 확인되지 않았다.
- 사전 여유 84,276,424,704 bytes, 마지막 성공 관측 요구량과 4 GiB 버퍼 합계 79,507,329,416 bytes 이상을 확인했다. 별도 실행 전 확인일 뿐 전체 배치 성공을 보장하지 않는다.
- timer는 enabled/active, 다음 `2026-10-28 10:00 KST`. 그때 수동 Worker가 계속 실행 중이면 중복 Worker 잠금이 차단되며 롯데 예약은 실패 또는 기동 전 오류가 될 수 있다. `Persistent=false`이므로 놓친 회차를 자동 보충하지 않는다.
- 원래 일자별 `hana.json` 및 이전 실행 결과는 덮어쓰지 않았다. 이번 시작 증거는 `operations/live/020-cardrag-manual-hana-20261027T212144Z.json`.

사용자 모니터링: `docker logs -f cardrag-manual-hana-20261027T212144Z`. 로그 추적 종료의 Ctrl+C는 로그 보기만 종료한다. 사용자가 종료 사실을 알리면 실제 exit code, terminal JSON·run ID, 하나 범위 collection/quality, 게시 generation·MCP 및 backup 증거와 10:00 timer 결과를 확인한다.
