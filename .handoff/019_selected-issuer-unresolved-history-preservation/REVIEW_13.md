# 019 REVIEW_13 — FIX_25 및 자동 순회·재부팅·이미지 운영 검토

2026-10-22 Codex, Reviewer. FIX_25/REPORT, LIVE_REVIEW_11, 관련 계획/이전 검토와 현재 source·Git·설치 unit·resolved compose·운영 DB를 대조했다. 배치/이미지 빌드·설정 변경·timer 활성화·삭제는 하지 않았다. 검증용 SQLite는 임시 디렉터리에서만 생성했다.

## FIX_25 판단

**설정 수정과 현재 설치 상태 검토 통과. 추가 필수 FIX 없음.** 실제 새 service를 통한 예약 배치 성공은 아직 미검증이며 그 인수까지 완료라고 표현하지 않는다.

- repo service는 active release의 operations/worker-compose.sh를 호출한다. resolved compose image는 `cardrag-worker:019-fix24-11d1321cc9e6`, UID10001:10001, project cardrag-worker이며 실제 state/auth/models/serving volume은 각각 cardrag-worker-state/cardrag-worker-auth/cardrag-worker-paddleocr-models/cardrag-serving이다. 과거 candidate 경로 불일치를 제거했다.
- timer Persistent=false 및 설치 script의 disable --now는 미실행 슬롯의 재부팅 직후 catch-up과 수동 검증 기간 자동 재시작을 막는다. script는 service를 새로 실행하지 않는다. 기존 실행 lock·취소/stop semantics는 유지한다.
- FIX_25_REPORT 작성 당시의 미적용 상태와 달리, 현재 설치 파일은06:58:15 KST 갱신됐으며 repo service/timer와 cmp 일치한다. systemctl ExecStart도 helper로 변경됐다. 현재 timer disabled/inactive/Persistent=no/next elapse 없음이다. service의 failed/exit-code 표시는06:32의 이전 실행 이력이며 새 unit을 실행해 실패한 증거가 아니다.
- systemd-analyze verify, shellcheck, sh -n, git diff --check 모두 통과. 실제 예약 배치를 검증 목적으로 시작하지 않았다.

## 정확한 순회 계약

현재 private runtime의 CARDRAG_ISSUER_EXECUTION_MODE=round_robin, channel=stable. pool의 실제 registry 순서는 우리→국민→신한→삼성→현대→하나→롯데→BC다. 예약 시각은 KST03:00이나 timer는 현재 비활성이다.

**한 번에8사를 모두 처리하지 않고, 서로 다른 실제 실행 날짜8회에 걸쳐 한 바퀴다.** 하루1회 정상 실행이면 카드사별 약8일 주기다. 하루 안에 round-robin을8번 호출해도8사로 넘어가지 않는다. 모든 카드사를 매일 갱신해야 한다면 이 모드는 요구에 맞지 않으며 별도 all 실행 정책 변경이 필요하다. 이번 질문만으로 기존 하루1사 정책을 변경하지 않았다.

- Rotation.claim은 Worker writer lock 안의 transaction으로 KST날짜/schedule/channel별 배정을 고정하고, remaining queue에서 한 카드사를 꺼낼 때 cursor를 전진시킨다.
- issuer_rotation에는 남은 queue/roster, issuer_rotation_day에는 날짜별 issuer/run/result가 들어간다. 이는 cardrag-worker-state의 worker-state.sqlite3에 영속 저장되므로 같은 볼륨을 사용하는 재부팅·container 교체·image 교체로 초기화되지 않는다. 텍스트 로그를 읽어 다음 카드를 추측하는 구조가 아니다.
- 같은 날 저장 결과가 있으면 성공/실패 결과를 재사용한다. 완료된 결과를 재호출해 다음사로 넘기지 않는다. 같은 날 미완료 배정은 기존 run을 이어가는 경로가 있으나 무결성/게시 reconciliation 조건에 따라 안전 차단할 수 있다. 모든 중단이 무조건 자동 복구된다는 뜻은 아니다.
- 실패해도 다음 실제 실행 날짜는 다음 카드사다. 이전 실패 카드사는 운영상 별도 점검/명시 resume가 필요하다. 며칠 쉬었다고 그 날짜 수만큼 queue를 건너뛰거나 한꺼번에 처리하지 않는다.
- timer Persistent=false는 missed slot을 즉시 catch-up하지 않는다. enable 상태이면 재부팅 후 다음 예정03:00에 실행하지만, 현재처럼 disabled면 재부팅해도 예약 자체가 돌아오지 않는다.
- `--issuer CODE` 수동 실행은 자동 cursor를 소비하지 않는다. **현재 실제 운영 DB에는 issuer_rotation 관련3개 table이 아직 없다.** 수동8사 완료 이력은 있으나 자동 순회 이력은 없으므로, 동일 설정의 첫 자동 배정은 우리부터다. 직전 수동 신한 다음이라 BC부터 시작하는 것이 아니다. 수작업 cursor seed는 하지 않았다.

## 검증 증거

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py -k rotation -q
5 passed, 52 deselected in1.92s
```

별도 임시 DB에서 우리 배정/run/result(failed)를 저장하고 WorkerState를 닫았다가 다시 열었다. 같은 날 동일 run/result 유지, 다음날 국민,10일 뒤 실제 다음 실행은 신한인 것을 assert했다. DB reopen persistence 검증이며 호스트 재부팅을 실제로 수행한 테스트가 아니다. 외부 호출0, 운영 DB 쓰기0이다. Python 구현 변경이 없어 전체1819건을 다시 돌리지 않았다.

## 이미지와 디스크

정상 배치의 compose run은 현재 검증된 이미지를 재사용해 일회성 container를 만든다. build/pull을 매 실행마다 강제하는 명령은 없다. current release의 private overlay가 image를 고정한다. 새 이미지들은019 장애 수정 코드 배포 때문이며, 성공 배치마다 생기는 데이터 generation과 container는 실행 이미지와 다르다.

FIX_25는 unit/helper 연결만 바꾸므로 새 Worker image 없이 적용할 수 있고 실제 현재도 FIX_24 image를 사용한다. 코드/의존성 변경 때 새 immutable image를 만들고 같은 운영 volume을 쓰는 것은 적절하다. 장기 운영에서 모든 과거 수정 image/release를 무기한 보관하는 것은 별도 정리 문제다.

docker system df 현재 호스트 전체 이미지57개/46.59GB/회수가능22.26GB, build cache8.02GB, volume168.8GB다. 이 수치를 전부 cardrag 또는 안전한 삭제 대상으로 해석하지 않는다. 활성/rollback/기타 서비스 참조와 snapshot 보존을 확인한 표적 정리가 필요하며 docker system prune -a/volume prune을 실행하지 않았다. 이전 독립 복구점2개 정리 과제도 별도다.

## 남은 운영 단계

수동8사 검증 통과와 현재 수정된 service 설치는 확인됐다. 예약 실제 실행·실패 모니터링 및 사용자 자동운영 전환은 별도다. 현재 disabled 상태를 임의로 enable하지 않았다. 하루1사 약8일 주기의 기존 정책과 매일8사 갱신 요구는 다른 운영 선택이며, 원하는 최신성 기준이 바뀌면 명시적으로 조정한다. 기존 PLAN의 자동 순회/운영 인수까지 모두 완료했다고 선언하거나019를 닫지 않는다.
