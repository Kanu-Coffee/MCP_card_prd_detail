# 018 준비 REPORT — 관리자 타이머 중지 후 전환 대기

작성: 2026-10-19, Codex. 사용자 운영 적용/라이브 검증 요청에 따라 준비했다.

## 현재 판정

**운영 문서 정정·새 이미지·비파괴 사전 점검·백업·실행 도구 준비 완료. 운영 전환과 실제 카드사 배치는 미시작.**

`sudo -n true`와 `systemctl stop cardrag-worker.timer --no-ask-password`는 관리자 대화형 인증 필요로 거부됐다. 우회하지 않았다. 현재 timer는 active/waiting, Worker service는 inactive, /opt/cardrag/current는 기존 013 release다. 새 activation helper는 timer active 상태에서 전환을 거부하며 이 guard의 동작도 확인했다. 사용자 SSH 명령에서 sudo로 timer를 중지한 다음 전환·첫 카드사를 시작할 수 있다.

## 준비한 배포

- Release: `/opt/cardrag/017-fca5ef313a0b`
- Image: `cardrag-worker:017-fca5ef313a0b`
- Image ID: `sha256:e4d930848ec04ded6b699553a08d1ceaf9dfc1843b39d241deaf568b33f684c2`
- Source tree digest: `fca5ef313a0beb9173a25c2836f121d12712e0c2ce06bc8b715d64e4b7f8fcd0`
- 기존 운영 image 016-d8a9018의 고정 runtime/dependency를 재사용해 worker/core 패키지 소스를 교체하는 오프라인 Docker build. dependency 선언/uv.lock 변경 없음 확인. Git revision은 aaeb2c4+worktree digest로 기록했고 새 commit인 것처럼 표시하지 않았다.
- 원래 013 release 및 016 image를 보존했다. MCP/다른 프로젝트/볼륨/서비스는 교체·삭제하지 않았다.
- host-local runtime overlay는 기존 운영 secret mount와 external 볼륨 이름을 보존했다. 신규 image, round_robin 기본 모드, 2160시간 정책, 컨테이너에서 접근 가능한 비공개 issuer proxy만 설정했다. 실제 proxy 값은 Git에 기록하지 않았다.
- 실제 볼륨: cardrag-worker-state / cardrag-worker-auth / cardrag-worker-paddleocr-models / cardrag-serving. 기본 신규 설치 볼륨(v122)을 잘못 사용하지 않도록 렌더된 config를 대조했다.
- 사용자 UID 10001의 새 release/설정 접근, 운영 OCR/embedding 설정과 source profile 일치도 확인했다.

## 직접 검증과 준비

- docs/OPERATIONS.md: 이전 ZIP 전체 목록 실패/압축 해제 전제 설명을 공통 문서 오류 격리·보존·degraded·재확인 정책으로 정정. 수동 8사와 자동 cursor 관계를 설명했다.
- `docker build --network none ... operations/code-image.Dockerfile`: 성공. 이미지 안의 run --help에서 --issuer/--round-robin 확인.
- 새 Compose `config --quiet`: 통과. 컨테이너 WorkerSettings에서 stable/local/immediate 백업, 2160시간, proxy 설정 여부 확인.
- 컨테이너에서 기존 Docker network의 프록시 TCP 연결 확인. 실제 카드사 HTTP나 provider 추론은 호출하지 않았다.
- 현재 stable manifest 해시/8사 자료 확인: hana735, samsung766, lotte647, woori979, shinhan931, hyundai605, kb751, bc122, 합계5536 문서. 최신 run f1871a51eff549ea9aee95b73eaad99d succeeded.
- Retained ReuseSource 실물 검증: 5536문서/8사, sealed DB hash·profile 검증 및 embedding 설정 일치. 선택 보존의 실제 baseline을 읽을 수 있음 확인.
- Worker lock 아래 SQLite online backup: 실제 state 볼륨의 deployment-backups/017-fca5ef313a0b/worker-state.sqlite3(7,286,935,552 bytes), backup-ledger.sqlite3(24,416,256 bytes). PDF/OCR/cache/serving 자료는 삭제/초기화하지 않았다.
- host helpers: activation, per-issuer detached run, live logs, 상태/신규 문서·OCR·비선택 보존/백업 조회, 8사 summary. shellcheck 통과. monitor는 SQLite mode=ro로 조회하며 WAL shared-memory 접근을 위해 state mount는 읽기쓰기다. 데이터 SQL 쓰기는 하지 않는다.
- monitor baseline/status 읽기 및 manifest hash 검증 smoke 통과. 8사 summary는 모두 NOT_FINISHED이며 실제 배치가 수행된 것처럼 표시하지 않았다.
- helpers는 매 수동 실행 전에 baseline과 named container를 기록하고, 다음 실행 전 이전 완료 결과를 host에 저장해 native run retention 후에도 검증 기록을 보존한다. 동시 Worker 실행·활성 timer를 거부한다. 원본 Worker lock도 유지한다.
- `git diff --check`: 통과. 제품 코드 변경은 없으므로 직전 리뷰의 1370 Worker 테스트를 새 실행으로 재기재하지 않는다. 새 스크립트는 shellcheck, 컨테이너 읽기/구문 점검 및 무실행 사전 검사로 검증했다. 실제 실행 중 stage/new-document SQL 분기는 라이브 확인 대상이다.

## 사용자 실행 순서

운영 서버 SSH에서 아래를 실행한다. sudo는 timer 중지/재개에만 필요하고 나머지는 lee의 기존 Docker 권한을 사용한다.

```bash
sudo systemctl stop cardrag-worker.timer
/opt/cardrag/017-fca5ef313a0b/operations/activate.sh
/opt/cardrag/current/operations/run-issuer.sh woori
```

같은 helper로 다음 카드사를 완료 후 하나씩 실행한다: kb, shinhan, samsung, hyundai, hana, lotte, bc. run helper는 detached container를 시작하고 종료하므로 SSH 연결이 끊겨도 Worker는 계속된다. logs-issuer.sh CODE는 follow, status-issuer.sh CODE는 단발 결과, summary-issuers.sh는 전체표다.

검증 기준: container exited/exit0, run succeeded 또는 no_change, 선택 issuer의 실제 discovery/download 성공, 비선택 문서 변경0, 전체8사 보존, 신규 PDF/OCR·ZIP 오류 가시성 및 백업 등록/잔여/로그 상태 확인. ZIP에 의한 문서 degraded는 사용자 승인 정책이며 네트워크 실패로 카드사 전체가 carry된 것을 정상 수집과 혼동하지 않는다. 단순 exit0만으로 8사 정상 판정하지 않는다.

8사 확인 후 `sudo systemctl enable --now cardrag-worker.timer`. 현재 OnCalendar는 KST 매일03시, Persistent=true여서 놓친 예약이 있으면 즉시 한 번 실행할 수 있다. 자동 run은 round_robin이다. 수동8회는 각 issuer 처리 경로 검증이며 8일간 실제 날짜 rollover를 수행한 시험은 아니다. 자동 day/cursor는 기존 날짜 시뮬레이션 회귀와 실제 예약 slot 확인을 구분한다. 현재 SQLite에는 rotation table이 없으므로 처음 예약은 woori부터다.

## 남은 단계

사용자의 관리자 timer 중지 및 activation, 첫 실제 woori 실행, 나머지7사 실제 결과 대조, 예약 재개 및 첫 slot 확인이 남는다. 현재 상태를 운영 적용 완료/라이브 검증 완료로 보고하지 않는다.
