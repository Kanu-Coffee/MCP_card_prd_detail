# 019 운영 반영 04 — FIX_16 배포 및 BC 수동 테스트 준비

2026-10-20 Codex. REVIEW_06 통과 후 사용자의 “운영반영하고 테스트를 위한 복붙용 명령어 셋팅” 요청으로 수행했다. **운영 전환 완료, BC 배치는 사용자가 실행하도록 미기동 상태다.** timer/service inactive, 전체 게시 상태는 기존 삼성 성공 세대를 유지한다.

## 배포 식별

- 새 current: `/opt/cardrag/019-fix16-4108e3dac5f5`
- 이전 current: `/opt/cardrag/019-e7fb8a0ba60c`
- 이미지: `cardrag-worker:019-fix16-4108e3dac5f5`
- 이미지 ID: `sha256:6f83f14501302bcc93fc1e76979515b8bb884788a5ddb0297a52e40935f1b990`
- worker/core Python 80파일 source digest: `d6685263a45526e28a4a3b1db2fc43ab2bd99cede49403f7f9a9b2bf9ad5b6a8`
- base ID+실제 overlay 입력/의존성/운영 script 등의 build input digest: `4108e3dac5f515c54336d9d38d1241425c0b0a82ae597dcee0dd5df640bcbd43`

기존 runtime 이미지 ID `sha256:c99752428ea1490d0e19f2267c6a7acd9d3d9b2aa4a07a11a0fb2bc1189c2d8f`를 확인하고 network-none source overlay로 빌드했다. 기존 release와 root/worker/core pyproject 및 uv.lock 바이트가 동일했다. 기존 release에 MCP pyproject가 없어 최초 비교가 중단됐으며, 실제 overlay 대상의 선언/lock 동일성을 확인한 뒤 진행했다. 새 base를 다운로드하거나 dependency를 변경하지 않았다. tracked/untracked 제품 소스와 py.typed를 frozen release로 복사했다.

설치 소스 81파일(py.typed 포함)을 frozen release와 바이트 대조하여 일치를 확인했다. host snapshot 일부 파일 mode600 때문에 UID10001의 비교가 PermissionError로 중단되어, **검증 컨테이너만 root+read-only+network-none으로 재실행**했다. 실제 runtime 테스트는 기존 UID10001로 통과했다. 운영 파일 전체에 읽기 권한을 넓히는 우회는 하지 않았다.

## 최신 복구점 및 활성화

timer/service inactive, 수동 Worker 컨테이너 없음과 이전 포인터/base ID를 확인했다. 새 snapshot-lock 도구로 worker.lock과 backup.lock을 모두 획득한 후 SQLite online backup을 수행하고 quick_check/hash/size를 검증했다. 두 lock은 snapshot 및 activate.sh의 원자적 current 교체가 끝날 때까지 유지했다. holder는 정상 종료·제거되어 lock을 해제했다.

복구점: `cardrag-worker-state:/state/deployment-backups/019-fix16-4108e3dac5f5/`

| 파일 | 크기 bytes | SHA256 |
|---|---:|---|
| worker-state.sqlite3 | 7305965568 | 4daa7d3542d1e38815b1574010b21cf8333d4a6afc48c290786ddfe593f247cf |
| backup-ledger.sqlite3 | 24416256 | ed6278f1244cd1499cfc042a148a2be5f8b16e5abfc4a34e717eaa58390246f9 |

이번에는 이전 snapshot과 Worker DB가 달라 새 복구점이 필요했다. 기존 복구본은 삭제하지 않았다. 여유 공간은 약76GiB→69GiB, 사용률80%→82%로 변했다. 주 증가 원인은 위 약6.8GiB 최신 DB snapshot이며 batch/run 누수로 해석하지 않는다. 복구 metadata와 evidence는 새 operations/deployment.json, backup-verified.json 및 volume snapshot.json에 기록했다.

## 실제 확인

- 새 이미지로 기존 BC PDF/prior를 RO 사용하여 native OCR 재사용 성공: SHA `c3a3556d7b80a20d69f1cd8ca4e329c73ef4e223e6cb1f509b978d55013eb3f8`, variant=None, provider_called=False. 새 카드사 수집/배치는 아니다.
- 새 이미지의 실제 기존 monitor-state.py 삼성 조회 exit0. required/bound2085, 비선택 문서 변경0, 기존 stable 유지.
- 활성화 후 정식 worker-compose.sh의 `backup status --run 44d55df9db8645c8b9060dc4631ff463 --issuer samsung`: pending0, remaining/lost/missing/conflicts/unbound 모두0, 정상 정책 제외2. RO 조회이므로 scope_complete=null이며 원격 index 완료를 새로 검증했다고 주장하지 않는다.
- current와 compose image 참조 일치, timer/service inactive. 자동 배치 및 BC 재실행 미기동.
- stable `g-44d55df9db8645c8b9060dc4-a7a46a5bb5e6`, 전체5543. 기존 실패 BC 컨테이너·실패 이력도 보존했다.

## 호스트 테스트 도구 보완

새 release의 operations에만 아래를 반영했다. 이전 release 도구는 보존했다.

- summary-issuers.sh: 실패한 BC의 backup_status=null을 `n/a`로 표시하여 문자열 포맷 오류를 방지. 실제 summary 실행 exit0 확인.
- status-issuer.sh: run snapshot에 `backup_excluded_shared_cache` 필드를 전달.
- check-issuer.sh 추가: 최신 해당 issuer 컨테이너의 종료·exit0·succeeded/no_change·run/컨테이너 일치·비선택 변경0을 확인한다. 성공 run ID를 자동 추출하여 정식 backup status를 호출한다. `--flush` 지정 때만 해당 run을 전송/검증하고 scope_complete=True가 아니면 실패로 반환한다. 실패 run은 전송 전에 차단한다. 실제 과거 실패 BC로 차단을 검증했으며 이 테스트에서는 backup flush를 실행하지 않았다.
- run/logs/status/summary/check shellcheck 통과. helper별 SHA는 deployment.json에 보존.

Compose의 orphan 경고는 보존된 과거 수동 실행 컨테이너에 대한 경고다. 자동 remove-orphans/prune은 하지 않았다. summary의 과거 BC failed는 새 테스트 실행 전의 보존된 실패 기록이다.

## 사용자 복붙 명령

```bash
OPS=/opt/cardrag/current/operations
"$OPS/run-issuer.sh" bc
"$OPS/logs-issuer.sh" bc
```

로그 관찰 후 배치 종료 시:

```bash
OPS=/opt/cardrag/current/operations
"$OPS/check-issuer.sh" bc --flush
"$OPS/summary-issuers.sh"
```

사용자는 RUN_ID를 수작업으로 옮길 필요가 없다. check 도구의 첫 status 결과에서 serving·비선택 변경·run 종료를 확인하고, 이어 출력되는 실제 flush에서 scope_complete=True를 확인한다. 단순 status의 null은 실패가 아니라 RO 진단 의미다. 로그 관찰을 Ctrl+C로 끝내도 detached Worker는 계속 실행된다. 아직 실행 중이면 check는 백업을 시작하지 않고 종료 후 재확인을 요청한다.

BC 사용자 실행 후 결과 리뷰와 실제 백업 완결 확인, 나머지 카드사 수동 검증이 남아 있다. 019는 닫지 않았고 timer도 재개하지 않았다.
