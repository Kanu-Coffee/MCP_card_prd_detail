# 019 LIVE_REVIEW_16 — 2026-10-23 국민 예약 배치 정상 완료

2026-10-23 16:17–16:21 KST Reviewer. systemd journal, 신규 run 보고서·seal, 전후 serving index, MCP 실제 조회, 백업 receipt/원격 index를 읽기 전용으로 검토했다. 코드/정책/예약/DB 변경, 재실행, backup flush는 하지 않았다.

## 결과

- 14:30 예약 기동→16:08 종료(서비스 wall 약98분27초, Worker5900.748초). Result=success/ExecMainStatus=0, CLI status=succeeded, collection_status=complete, quality_status=passed. 13시 때의 권한 문제나 어제 우리 origin 오류는 재발하지 않았다.
- run_id=745fbf41449541db9fa94f9b6aaaa27e, mode=round_robin, selected=[kb]. 오늘1회차 succeeded 이력 저장, 다음 queue 선두shinhan.
- 국민750건 discovery/acquired, PDF747건 성공·3건 skipped/unsupported. PDF cache hits746, 신규 다운로드1, 원본 재검증91건 모두304. 국민 외 원본 호출은 없고 다른7사는 not_scheduled.
- 국민 문서757건 OCR/구조 처리 성공, 기존OCR756건 재사용/새OCR provider1건, OCR실패0/구조실패0. 신규 상품0, source successor1, 단종확정0/단종후보1. 전체 기존 unsupported8건과 구조 fallback2건은 결과에 표시하며 전체 정상 문서처럼 숨기지 않는다.

## 운영/MCP와 비선택 보존

새 serving g-745fbf41449541db9fa94f9b-f666a8a06bd4로 게시됐고 MCP current.json도 같은 generation이다. 전후 contract_revisions+product lineage 전체 행 비교에서 변경은 국민3행만(집합 변경 포함)이며 다른7사 행은 동일하다. 카드사별 문서 수는 우리980/국민757/신한958/삼성766/현대607/하나735/롯데707/BC122, 총5632 유지다.

MCP get_product(kb,09570)는 KB국민 WE:SH Daily 카드의 새20261020 current 설명서 doc_1628df215b486105d2794ceb58e02aa4069005145123a7e0853d8e17e6576e33를 반환했다. get_source_page(page1) 실제 호출 성공, OCR text SHA e76d08b6a8e17929508c5cf986cb5cb914a631ec2112e1d67ff6e9ff6abef2b0. 이전20260922 설명서 doc_da5f02cd1982f3d038d6e853afb42fd30edcbbbc24963c6b147d4787fdc7b0a4는 superseded로 보존되며 해당 page1 조회도 성공했다.

## 백업 판정

최종 JSON backup_status=degraded이나 이번 국민 범위 실패는 아니다. 업로드2123777 bytes/요청7회, backup_scope_complete=true, in_scope remaining/lost_source/conflicts/missing/unbound 모두0. 전역에 남은3건30862bytes는 어제부터 있던 범위 밖 pending이며 이번run이 새로 누락한 것이 아니다. excluded_shared_cache12도 정책에 따른 제외다. 이 pending은 삭제/flush하지 않았다.

backup status --run 745fbf41449541db9fa94f9b6aaaa27e --issuer kb의 read_only 진단에서 동일한 범위별0/범위밖3건 확인. 이 명령의 scope_complete=null은 원격검증을 하지 않는 상태조회 계약이므로 최종 scope_complete=true를 반박하지 않는다.

더 나아가 BackupLedger readonly 연결과 WebDAV GET만 사용하는 별도 검증으로 국민 expected1553경로의 receipt와 committed remote index를 대조했다. backup_scope_complete_from_receipts_and_remote_index=true, missing/conflicts/remaining0 확인. degraded는 전역 pending이 남으면 index commit 성공 후에도 degraded로 표시하는 기존 backup.py 로직과 부합한다. 마지막백업16:08:12 KST.

## 처리 시간과 다음 실행

약98분으로 처리량 대비 시간은 여전히 길다. 실제 신규OCR은1건이며 export에2894.934초(약48분15초), 전체 게시본 캐시 조회/검증·재생성 부담이 크다. 이번 성공을 빠른 단독 배치 성능 달성으로 해석하지 않는다. 성능 개선은 별도 검토 대상으로 남기되 이번 검토에서 임의 변경하지 않는다.

timer active, 다음2026-10-24 14:30 KST. queue 선두신한 확인. 추가 수정/재실행이 필요한 새 기능 오류는 확인되지 않았다. 원본 우리카드는 사용자 지시에 따라 재호출하지 않았다.

## 증거

- journal 원본 /tmp/cardrag-kb-20261023-journal.log
- 최종JSON /tmp/cardrag-kb-20261023-result.json
- 원격백업검증 /tmp/cardrag-kb-20261023-backup-verification.json
- 검증 helper /tmp/cardrag-kb-20261023-backup-review.py (SQL readonly/WebDAV GET only)
- 운영 run reports/issuer-collection.json, performance.json, corpus-diff.json 및 sealed/publish.json

최종JSON과 원격검증은 active operations/live/kb-auto-20261023.*.json에도 보존했다. 과거 수동 status-issuer.sh kb의 결과와 이번자동run을 구별한다.
