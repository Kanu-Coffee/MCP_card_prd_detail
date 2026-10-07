# 008 Executor 최종 운영 결과 — 핵심 기능 인수 가능

2026-10-07 22:38 KST. 사용자 Worker 완료 알림에 따라 실제 종료·보고서·게시·MCP와 기존 자료 보존을 검증했다. **008 카드사 병렬 수집/장애 격리 및 007 content OCR 계약의 운영 전환은 완료했고, 핵심 기능 인수가 가능하다.** 006 OpenCode 운영 활성화는 PLAN §10.2의 별도 승인 항목으로 미실행이다. 모든 카드사 최신화 완료를 선언하는 것은 아니다.

## 실제 운영 결과

| 항목 | 확인 결과 |
|---|---|
| 컨테이너 | `cardrag-prod-008-resume01`, ID `9e92eea6cbca7787424bd223aace03f3094547284855ae969a572113b87bc973` |
| 실행 시각 | 2026-10-07 19:23:04 ~ 21:40:32 KST |
| 종료 | exited / exit 0 / OOMKilled false; AutoRemove false로 결과 보존 |
| run | `5f60efbc4529434cb15b7cd43128b705` |
| pipeline 상태 | `succeeded` / collection `degraded` / failed issuers `[shinhan]` |
| 게시 generation | `g-5f60efbc4529434cb15b7cd4-e05b9e8d2031` |
| 최종 문서 | 5,513 current+historical, current 5,059 / superseded 454, unsupported 6 별도 |
| OCR | 완료 5,513 / 실패 0 / 재사용 5,513 / 재개 실행 provider 호출 0 |
| embedding | derived views 615,840, unique cache misses 177, provider call count 3 |
| DB/vector | 4,904,849,408 / 10,089,922,560 bytes |
| corpus | missing_unjustified 0, retired 0, 단종 후보 1 |
| 게시/정리 | OCR cache publication deferred 0, PDF prune succeeded / 삭제 0, 원격 GC 삭제 0 |

첫 실행은 용량 gate에서 실패했지만 OCR 캐시 재사용 5,511 / 실제 provider documents 2를 남겼다. 재개가 이를 재사용하여 추가 OCR 호출 없이 완료했다. 합계 추론을 5,513건 전량 처리로 해석하면 안 된다. 새 롯데 PDF 개정 2건(1863, 1737)은 이전 source의 superseded 문서도 보존됐다. 구조 parser fallback 2건은 기존 안전 UNCLASSIFIED fallback 정책이며 structure_failed 0 / source coverage 100%다. 이를 신규 배포 차단 조건으로 추가하지 않는다.

## 008 장애 격리 실증

- 정상 수집 issuer 7개: 우리 915, KB 751, 삼성 565, 현대 577, 하나 724, 롯데 528, BC 122개 채택. 수집 원천 목록 수이며 최종 historical 포함 검색 문서 수와 다르다.
- 신한은 ReadError 4회 후 discovery failed / download skipped, 신한 기존 **931개**를 이전 stable에서 carry했다. 최신 원천 관측을 성공으로 위장하지 않았으며 origin_freshness_at null, 기존 정상 관측 시각 유지.
- collection report terminal=true 후 OCR→embedding→export→게시 완료. 실제 신한 실패가 전체 run을 막지 않았다. 매일 03시의 다음 신규 run은 신한을 다시 시도한다. 이번 인수를 위해 별도 2일 대기/전량 재OCR/추가 장기 배치를 요구하지 않는다.

## 기존 자료·007 계약 보존 검증

1. 이전/새 sealed manifest를 비교했다. 이전 5,512개 중 공유 문서 **5,511개 전부 PDF·OCR SHA/크기·page_count·availability 동일**. 신한 931개 누락 없음, 동일 바이트 보존.
2. cache binding은 예상된 `native → content` 전환이다. 모든 5,513개 manifest 문서가 content kind와 ocr_variant_id를 갖는다. 이전과 다른 reuse_key/variant 메타데이터는 007 계약 전환이며 OCR 바이트 변화가 아니다. 동일 PDF 여러 OCR pin도 공유 문서의 OCR SHA를 기준으로 보존했다.
3. 실행 중 MCP의 실제 local serving DB를 immutable/read-only로 확인했다. 이전/새 **신한 contract_revisions 전체 931행 동일**(current/superseded 관계·source identity·PDF 포함), 문서별 page/view 건수도 모두 동일했다. manifest만 남기고 검색 DB에서 누락시키는 구현이 아니다.
4. 새 DB 전체 contract_revisions 5,513, document_pages 27,153, embedding_views 615,840으로 manifest/export 수치와 일치한다. Worker export 자체 검증에서 views 615,840/615,840, revision coverage 5,513/5,513 통과했다. 대형 DB·vector의 동일 SHA 전수 읽기를 다시 반복하지 않았다.
5. 이전 문서 중 1개 미포함은 **롯데 1733 정상 목록의 부재**에 따른 기존 corpus/retirement 정책의 최초 후보다: `doc_663087c32417e58594e732bc896e5e780547df1e4a8cf3d70acd620381604fde`, consecutive_absences=1. 신한 장애와 무관하며 단종 확정/원본 삭제 없음. 후보 자료는 이전 generation에서 복원 가능하다. 신규 generation 목록에는 이 후보 문서가 포함되지 않는다는 점을 명시한다. `missing_unjustified=0`을 이전 모든 문서가 신규 목록에 있다는 뜻으로 쓰지 않는다.

## 실제 게시·서비스 검증

- Worker 운영 env로 WebDAV stable pointer를 직접 GET하여 새 generation `g-5f60efbc4529434cb15b7cd4-e05b9e8d2031` 확인했다. read-only 짧은 script이며 Worker 배치를 추가 실행하지 않았다.
- 현재 MCP local pointer도 동일 generation이다. 기존 007 MCP 이미지가 새 007/008 계약 generation을 정상 소비하므로 재시작/재빌드하지 않았다.
- `/health/ready` 실제 HTTP 200 / ready=true, Docker healthy.
- 인증된 `/resources/products/shinhan/00003`, 해당 current document 및 page 1을 실제 호출하여 모두 HTTP 200. 이 조회는 embedding/LLM 호출이 없다. 해당 endpoint 응답에는 generation_id 필드가 없으므로 loaded generation은 local pointer/serving DB로 별도 확인했다.
- 운영 OCR 설정 codex-exec / qwen3.8-flash, read-write / epoch 0, GC false, pending reprocess 없음 재확인. 006 코드 통합은 유지하지만 provider 전환은 하지 않았다.
- 종료 후 filesystem 여유 약 **91GiB**, timer active / 다음 2026-10-08 03:00 KST. 추가 수동 배치를 시작하지 않았다.

## 증거·검증 범위와 남은 항목

구현 commit `6b42a1a55899899be8fdab5d3b3555ef7b5390da` 동일. Worker image `cardrag-worker:008-6b42a1a`, ID `sha256:2eed6edbeb4f1d57d69481bd0d576dfbfcc957d29cb3bd89a5591619f5e9d121`. 이후 Git commit은 handoff 문서만 변경했다. CI run 37594483364 success / 전체 테스트 2,339 passed / Ruff·mypy는 기존 동일 코드 검증을 재사용한다. 이번에는 실제 완료 보고서와 필요 범위의 운영 데이터·HTTP 조회를 검증했다.

운영 증거는 `/opt/cardrag/008-6b42a1a/operations/completion-20261007/`의 collection/performance/corpus 보고서, `baseline-compare.json`, `cardrag008-mcp-review.json`, `publication-check.json` 및 검증 script에 보존했다. 실패 실행 증거는 `recovery-20261007/`에 별도 보존한다. 비밀값은 보고서나 Git에 기록하지 않았다.

신한 최신화는 접속 장애가 계속되어 미완료다. 기존 자료 서비스와 다른 카드사 처리에 지장이 없고, 장애를 명시한 채 다음 run에서 재시도하므로 008 핵심 기능 인수를 막지 않는다. 신한 IP 차단 여부는 확정하지 않는다. OpenCode 운영 활성화·Git main 병합·정식 공개 릴리스는 별도 범위이며, 이번 결과로 자동 실행하지 않았다.

**추가 FIX 없이 008 핵심 기능 인수 권고.** OpenCode 승인 게이트를 포함한 전체 범위의 무조건 완료 선언은 하지 않는다. 현재 정상 운영을 유지한다.
