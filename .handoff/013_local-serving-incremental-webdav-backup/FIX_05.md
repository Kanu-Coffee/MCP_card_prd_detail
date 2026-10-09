# 013 FIX_05 — 실제 백업 트래픽 및 부분 배치 commit 예산 개선

작성: 2026-10-09, Reviewer / Codex. 기준: 3eaa35d.

## 운영 관측

사용자가16:12 WebDAV 대량 트래픽을 지적했다.16:10~16:15 KST Docker timestamp 로그 집계:

- `cardrag-worker-013`: HTTP0건, 해당 구간 로그0건. OCR에 WebDAV 호출이 들어갔다는 근거 없음.
- `cardrag-backup`: HEAD585 / GET588 / MKCOL17 / PUT2 / MOVE2 / DELETE2.
- 객체 HEAD/GET는 기존 CAS 검증. PUT은 backup batch/index 임시 JSON 두 건이며 OCR/PDF를 전량 다시 업로드한 증거가 아니다.
-16:12:28 결과 pending_commit, flushed_count0, processed/uploaded_count1189, reported uploaded_bytes1,028,914,709, remaining_pending9856. 이 bytes 값은 코드가 처리 객체의 expected_size를 누적한 값이므로 실제 업로드 바이트로 해석하지 않는다.
-15:52 첫 batch는1569건 index commit 완료/degraded(시간 제한), 이후 새 OCR metadata inventory도 늘어 pending이 증가했다. 기존 receipts가 남아 다음 실행에서 이미 검증한 동일 root/hash/size 자료는 전송/GET를 생략하므로 모든 기존 자료를 무한 재전송한다고 단정하지 않는다.

## 즉시 조치

16:2x에 별도 `cardrag-backup`을 stop했다. 이때 이전 flush 결과 후900초 sleep 구간이었다. Worker/MCP/03시 timer/원격 객체/backup pending/spool/receipts를 변경하거나 삭제하지 않았다. 현재 Worker는 계속 실행한다. inline backup은 Worker 본처리 이후의 기존 옵션으로 남으며 본처리 성공과 격리되어 있다.

초기 기존 inventory를 전부 검증하는 백업을 OCR와 동시에 시작한 운영 배치에는 개선이 필요하다. 이 관측이 코드 인수 당시 실 WebDAV를 검증했다고 소급 해석되지 않도록 기록한다. OPERATIONS_REPORT/OPERATIONS_FIX_01_REPORT의 기동 기록은 보존한다.

## 필요한 수정

1. `apps/cardrag-worker/src/cardrag_worker/backup.py` flush: 객체 처리 budget과 batch/index commit budget을 구분한다. 지금은 객체 처리에300초를 모두 사용한 후 index commit에 `max(1, remaining)`만 주어 실제 원격 원자 교체가 끝나기 전에 TimeoutError가 발생한다. 작은 batch 또는 명시적 commit reserve로 부분 batch를 확정할 시간을 보장한다. 전체 실행의 유한 budget은 유지하며 이를 위해 예산을 무제한 늘리지 않는다.
2. 기존 verified receipts는 같은 canonical remote root/hash/size면 GET/PUT 없이 index 확정에 재사용한다. commit 실패→다음 retry에서 기존 verified 객체 네트워크 재요청0건을 검증한다. commit 성공 전 pending/spool을 지우지 않는다.
3. `uploaded_bytes/count`는 현재 이미 존재하는 객체를 `put_bytes`에서 HEAD/GET 검증만 해도 업로드로 세는 문제가 있다. API 출력에서 processed/verified와 actual upload를 구분하거나, 실제 전송량을 측정하지 못하면 그 한계를 명확히 표시한다. 실제 다운로드까지 업로드로 보고하지 않는다. 기존 결과 consumer와 호환성을 검토한다.
4. 백업은 운영 중 OCR/embedding/publish와 자원 경합을 피하도록 실행 조건/시간대를 조정한다. Worker 실행 중 background flush는 보류하거나 작은 bounded batch로 처리한다. Worker가 없으면 재시도할 수 있어야 하므로 잠금이 없는 단순 sleep만 영구 해결책으로 사용하지 않는다. 기존 `backup.lock`으로 inline/background single writer를 유지한다.
5. 최초 inventory 검증은 migration 한 번의 작업으로 구분하고, 이후에는 신규/미등록 OCR/PDF만 다룬다. 기존 remote generation/adopted/Paddle 백업을 삭제하지 않고 OCR 재실행 없이 보존한다. OCR 원격 cache 접근은 local mode에서 None으로 전달되는 현재 경계를 유지한다.

## 검증과 운영 반영 제약

느린 mock WebDAV로 부분 batch의 data budget 소진 후 index commit 성공/pending 감소를 재현한다. 다음 retry에서 verified 객체 재GET/PUT 없음, commit failure에서 pending/spool 보존, remote root 변경 시 재검증을 확인한다. 실제 기존 백업 사례의 HEAD/GET/PUT를 구분해 소량 운영 검증한다. 전체 Worker 재실행/유료OCR/임베딩 재생성은 요구하지 않는다.

**현재 장시간 Worker를 재시작하거나 중단하지 않는다.** 수정/검증은 독립 진행하고, 사용자의 Worker 완료 통보 후 새 백업 runtime만 교체·재개한다. `docker start cardrag-backup`으로 기존 정책을 무조건 재개하지 않는다. 새 승인된 retry 설정/이미지로 재생성한다. 다음 Worker image 갱신은 운영 배치가 없을 때 진행한다.
