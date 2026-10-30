# 운영 기록 06: 하나카드 재개 완료 확인

- 2026-10-28 08:54:07 KST `cardrag-resume-hana-20261027T221729Z` 종료, exit 0, OOM false. 결과 run `9fa3b10def034fa8b65029e6df2d29ae` status=succeeded, collection_status=complete, failed_issuers=[], quality_status=passed.
- OCR progress 745/745 succeeded, failed=0; v5 metrics `ocr_cache_reused_count=742`, `ocr_provider_called_count=3`, `structure_failed_document_count=0`, `structure_fallback_document_count=2`. PDF 729건 성공, 캐시 hits 729, 다운로드 0.
- Published local generation `g-9fa3b10def034fa8b65029e6-0da956ec4e05`. `operations/test-live-mcp.sh` 확인에서 MCP current generation과 stable pointer 일치, get_source_page 두 건 성공.
- 최종 실행의 백업 status=degraded지만 scope_complete=true, 범위 내 remaining/missing/lost/conflicts/unbound 모두 0; 범위 밖 미처리 4건 300444 bytes가 남는다. `backup status --run ... --issuer hana` 읽기 전용 재조회는 동일한 범위 내 0/범위 밖 4건을 나타내며, 재검증을 하지 않는 read_only 특성상 scope_complete=null이다. 최종 실행의 scope_complete=true와 구분한다.
- 08:56 KST timer active, 다음 10:00 KST. 실제 예약 실행 결과는 아직 미확인이다.
