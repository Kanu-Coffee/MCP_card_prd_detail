# 013 재실행 배치 완료 및 WebDAV 비백업 구간 검토

2026-10-10 Reviewer/Codex. commit50a0129. run `d995591fee8c47ffb7bff73e27a8c44a`.

## 판정

**05:33 수동 복구 배치는 정상 완료했고, 초기 관리대장 구축 이후 변경 없는 batch의 백업 생략 동작을 확인했다.** 이번 기록은03시 systemd기동 성공 증거가 아니다.03시 실패 및 권한수정 기록은 이전 OPERATIONS_BACKUP_AND_SCHEDULED_20261010_REVIEW.md를 유지한다. 다음 실제03시systemd기동은 별도 확인 대상이다.

- `cardrag-worker-013-retry-20261010`:05:33:13→07:10:59, **1시간37분46초**, exit0/OOMfalse. startup05:33:33.198.
- status succeeded/publishedtrue/local_generation_publishedtrue, final5520문서. OCR5520재사용/provider0/failed0/structure_failed0. PDFdownloads0/revisions0, embeddingcachemiss0.
- 기존 신한 originnetwork실패만collectiondegraded;7카드사성공,931건carry. 기존 unsupported8개 보호문서 건너뜀. 정상처리 범위에서 자료누락을 의미하지 않는다.
- backup_statusdeferred, backup_requests0, backup_bytes_uploaded0, pending_bytes0/pending_ocr_count0, oldest_pendingnull. 마지막 원격backup시각은 초기작업00:01:21 그대로이고 runs_since_last_backup1이다. pending없는상태에서record_run_success는로컬기록만수행하며flush/client생성이생략됐다.
- OCR/localprocessing24분09초, export37분12초. pipeline5835.475초는약1시간37분15초이며컨테이너전체시간과차이가있다. cachehit이어도구조화/views/token/export/정리가실행되어 전체시간이OCR호출0만으로즉시단축되지는않는다.
- peakRSS약8.90GiB, 디스크여유110GiB. MCPhealthy/readytrue.
- MCPgeneration `g-d995591fee8c47ffb7bff73e-035c4632e085` 활성화07:13:18.109. current.json 및 하나15911/우리500107 실제get_product_summary에서동일generation성공확인.

## WebDAV 호출 확인

1. 실제컨테이너설정publication_transportlocal. CLI는LocalServingTransport를생성하고OCRResolver에는webdavNone을전달한다. pipeline수집후OCR/embedding/export/publication경로에WebDAVClient가전달되지않는다. localtransport는로컬filesystem동작이다.
2. performance의webdav_verificationnull이며webdavHTTP측정block이없다. 컨테이너전체로그에서WebDAVhost/HTTPRequest/backuptimeout등원격요청흔적0. backup결과requests0와대기0이일치한다.
3. `Remote OCR cache access mode=read-write`는설정명출력로그다. 코드상localmode에서도동일로그를찍으며remoteclient접속을뜻하지않는다. 실제5519이상cache는localpriorseal재사용이다(이번5520전량).
4. **별도리뷰용요청은있었다.**05:33~05:34에Codex가초기백업완료를검증하려고별도임시컨테이너에서원격`v1/backup/index.json`을1회GET(8,017,974bytes)했다. Worker자동네트워크요청이아니며PDF/OCR전량검증도아니다. 당시NAS트래픽을완전히0이었다고설명하지않는다. 이번완료검토에서는추가WebDAV검증호출을하지않았다.
5. 당시전체구간패킷캡처나NAS접근로그는없어독립패킷계수로WorkerHTTP0을증명한것은아니다. 결론은실제설정·생성코드경로·로그·성능기록·백업결과를대조한것이며자동WebDAV호출흔적이나경로가없다는것이다.

이번검토에서는컨테이너재기동/원격전수감사/추가backupflush/설정변경을수행하지않았다. 증거: `evidence/operations-retry-20261010-completed.json`. 전체자료백업의정기무결성감사를수행했다는뜻은아니며초기완료와이번변경없음생략동작을확인한것이다.
