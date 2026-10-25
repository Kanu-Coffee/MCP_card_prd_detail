# 019 LIVE_REVIEW_10 — 롯데 정상 완주·라이브 게시 및 범위 백업 확인

2026-10-21 Codex, Reviewer. 사용자 종료 확인 요청에 따라 OPERATIONS_REPORT_10과 최신 container 로그, 운영 status helper, 현재 read-only backup status, sealed publication 및 실제 MCP 응답을 대조했다. 배치 재시작·백업 flush·DB 수정·배포·삭제는 수행하지 않았다.

## 판정

**롯데 배치와 라이브 게시는 정상 완료다. 해당 run의 백업 범위도 종료 시 검증 완료됐다.** 미지원 ZIP 및 정책상 범위 밖 pending이 있어 collection/backup 집계의 degraded 표시까지 전부 clean이라고 표현하지 않는다. 추가 코드 FIX가 필요한 새 장애는 확인하지 못했다.

- release `/opt/cardrag/019-fix24-11d1321cc9e6`, container `cardrag-live-lotte-resume-20261021T104220Z`.
- 동일 run `49845932e05849d3a0110a2bc82d6c1c`, status=succeeded, quality_status=passed, published=true, container exit0.
- 2026-10-21 19:42:21–21:48:17 KST, 약2시간6분. OCR707/707 완료는20:30:13, failed=0/structure_failed=0/cache_publication_deferred=0. Worker elapsed7549.512秒이며 container 전체 시간과 구분한다.
- 영속 stage OCR/structure/views 각각707 succeeded, embedding-v5 succeeded. 마지막 재개 시도 지표는 OCR cache693/provider-called documents14, PDF cache529/download0. 14를 최초 시도부터의 누적 OCR 호출 수나 page/chunk 호출 수로 해석하지 않는다.
- 이전 unknown 실패 지점을 통과했다. reports/ocr-systemic-failure.json의09:00:23 UTC 기록은 같은 run의 이전 실패 이력이며 최신 실행 실패로 오인하지 않는다.

## 게시·범위 보존·MCP

stable generation `g-49845932e05849d3a0110a2b-87e491cd55a1`.

롯데647→707(+60), 전체5545→5605. 비선택 문서 변경0, 비선택 stage records=[]다. 다른7사는 하나735/삼성766/우리980/신한931/현대607/국민757/BC122 유지.

운영 MCP는 실제 `cardrag-serving` 볼륨을 read-only로 공유한다. 기존 운영 test-live-mcp.py의 두 문서 ID만 /tmp 복사본에서 이번 신규 문서로 바꿔, 실행 중 cardrag-mcp의 인증된 MCP API에 initialize/tools-list/get_source_page를 호출했다. 인증정보는 출력하지 않았다.

- 필웨이 명품엔로카 `doc_0a961513d2062940d3cf93ccd1897a0aff098e48b46898573f09b1625ee6e2f0`: page1/6, text43문자 반환.
- 하나은행 밀리언달러 카드(issuer=lotte) `doc_5da34711132e1f245ff8985b403f78eec5a30cd374e0c9c4d0ecee043fc2352e`: page1/9, text3512문자 반환.
- MCP current generation과 Worker stable generation 일치. 두 호출 모두 성공. 두 번째는 이전 시도에서 Paddle fallback을 처리했던 문서지만 이번 seal은 아래와 같이 별도 OCR body를 참조한다. 이번 MCP 응답을 이전 Paddle body 자체의 게시 증거로 주장하지 않는다.

## 백업과 정상 pending 구분

종료 snapshot: backup_scope_complete=true, in_scope_remaining/lost_source/conflicts/missing/unbound 모두0. backup_run_recorded=true. helper의 현재 ledger 검사는 expected2606/bound2606/matching receipts2606, conflicts0/missing0을 확인했다. 종료 업로드43,926,739bytes/227requests.

현재 `backup status --run ... --issuer lotte`는 read_only 모드이므로 backup_scope_complete=null을 반환하지만 위 범위 결손은 모두0이다. 이번 조사에서 원격 객체를 다시 GET 검증한 것은 아니며, 종료 시 완료 판정과 현재 영수증 정합성 증거를 구분한다.

전체 pending3개/30,862bytes는 모두 out_of_scope이며, 이전 시도의 해당9페이지 fallback 문서에서 등록한 OCR CAS(28,631bytes), native manifest(1,946bytes), READY(285bytes)다. pending CAS SHA는 `bf265e2672c2001c7679464f03111fa5497ff73aa11dffb8458169fdeb34fb03`; 실제 현재 seal OCR SHA는 `051a255837a3e6e8766419908e85cce33be1d8a8619d8af3d53acff10e71ab46`/24,071bytes이고 cache kind/reuse key=null이다. **현재 게시의 필수 OCR body가 pending인 상황이 아니다.** 기존 공유 cache/과거 body 제외 정책과 일치하며 shared cache excluded12다. pending0을 만들려고 전체 flush·삭제·강제 업로드하지 않았다. backup_status=degraded는 이번 run 필수 범위 백업 실패와 구분한다.

## 남은 알려진 제한

- collection_status=degraded: 롯데 product1893의 explicit_zip_archive 첨부1개가 기존 정책대로 미지원 처리됐다. 압축 해제를 시도하지 않는다. discovery cached404 경고1개도 있으나 수집은 complete다.
- unsupported_documents8은 기존 비선택 DRM(현대1/국민3/우리4)이며 롯데 OCR707건의 실패8건이 아니다.
- 구조 parser fallback2개, historical revision unresolved6개가 명시적으로 남는다. structure_failed_document_count=0, source_coverage_percent=100.0, quality passed와 함께 해석한다.
- 파일시스템 가용84GiB/사용78%. FIX_24 배포 보고서의 임시 복구점2개(fix23/fix24)는 아직 보존돼 있다. 기존 단일 독립 복구본 정책에 맞추는 정리는 별도 운영 후속이며 이번 확인에서 삭제하지 않았다.
- timer/service inactive 유지. 신한 배치와019 전체8사 최종 인수는 아직 별도다. 롯데를 다시 실행할 필요는 확인되지 않았다.
