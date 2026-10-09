# 013 초기 백업 완료 및2026-10-10 03시 배치 검토

Reviewer/Executor Codex. 확인05:31~05:34KST. commit50a0129.

## 판정

1. **초기 백업 관리대장 작업 전량 정상 완료.**
2. **03시 timer는 발화했지만 Worker는 시작되지 않았다. 배포 폴더 접근권한 누락으로 ExecStartPre가200/CHDIR 실패했다.** 두 실행 모두 정상 완료라고 판정할 수 없다.
3. 접근권한을 바로 수정하고 UID10001 검증 후 누락 batch를 동일 compose/image/env로 수동 재기동했다. startup 및 첫수집만 확인했으며 전체 완료와 해당 배치의 증분 백업은 후속 확인 대상이다.

## 초기 백업

- 컨테이너 `cardrag-backup-bootstrap`:23:14:50→00:01:21,46분31초,exit0.2시간 예산 안에서 단1회flush 완료.
- 처리12977개/1289084865bytes. pending_count0 / pending_bytes0 / lost_count0 / statusready / should_triggerfalse. 마지막 index commit00:01:21.214807.
- 실제 HTTP:HEAD12979/GET19861/MKCOL55034/PUT6881/MOVE6882/DELETE6882. processed/uploaded 명칭의12977개·1.29GB는 확인·commit한 전체 항목이며 실제 신규PUT량과 동일하지 않다.
- 이번 리뷰에서 원격 index만8,017,974bytes 다운로드하여 확인:remote items16783/local scoped receipts16783/hash·size mismatch0. PDF/OCR 전량 재다운로드 검증은 반복하지 않았다.
- 동일 설정으로 backup status 확인 후 empty queue flush 호출:unchanged / requests0 / uploaded_count0 / uploaded_bytes0 / remaining_pending0. **대기0이면 전체 재검증·업로드 없이 빠르게 생략하는 경로를 확인했다.** 이것은03시 전체 batch 검증을 대체하지 않는다.

## 03시 실패와 복구

- systemd journal03:00:00:WorkingDirectory Permission denied; ExecStartPre200/CHDIR. 실제OCR/수집/백업은 실행되지 않았다. service Resultexit-code / ActiveStatefailed.
- `/opt/cardrag/current` 대상 `/opt/cardrag/013-50a0129` 및 deploy/worker가lee소유0700이었다. 이전 수동 Docker 작업은lee로 실행되어 통과했지만 systemd Usercardrag(10001)은 접근할 수 없었다. 이번 배포의 운영권한 확인 누락 책임은Codex에 있다.
- 배포root/deploy/worker/mcp/operations/docs 디렉터리만0755로 수정. secrets파일/환경변수내용/개인로그0600/운영volume은 변경하지 않았다. UID10001에서 chdir 및 compose/worker.env 읽기 성공 검증.
- systemctl 직접 재시작은 대화형 인증 필요로 거절됨. sudo -n도 불가. 권한을 우회하지 않았다.
- 기존 timer는active, 다음발화2026-10-11 03:00. failed표시는 지난03시시도이며 다음 timer 실행을 비활성화하지 않는다. 실제systemd 재실행 성공은 아직 검증하지 않았다.
- 누락 작업을 동일 운영 compose/env/image로 `cardrag-worker-013-retry-20261010`에 수동기동05:33:13. startup capacity 및 embeddingpreflight 통과, startup05:33:33.198(19.225초), Samsung565 discovery05:33:46.225 확인. Worker재시작을 반복하거나 대기완료까지 모니터링하지 않았다.
- MCPhealthy/readytrue; 기존활성generation 정상서빙 유지. 배포 이미지/백업300초정기한도/즉시모드 유지.

## 후속 확인

사용자가 retry완료를 알려주면 PDF/OCR신규량,5519이상cache재사용 여부,queue 신규추가 여부,backup deferred/unchanged 및requests/전송량,publication·MCPgeneration을 확인한다. 신규자료가 있으면 필요한백업만 수행했는지 검토한다. 다음실제03시systemd기동 성공도 별도로 확인한다.

```sh
docker logs --tail 20 -f cardrag-worker-013-retry-20261010
```

수동복구 실행이 진행중이므로 systemd를 동시에 start하지 않는다. 증거: `evidence/operations-backup-and-scheduled-20261010.json`. 이전 handoff기록은 보존했다.
