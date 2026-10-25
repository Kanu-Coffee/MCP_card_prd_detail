# 019 REVIEW_17 — FIX_30 공통 재시도 검토 및 단일 PDF 등록 오류 여부

2026-10-22 Codex, Reviewer. FIX_30/REPORT/AMENDMENT_01, LIVE_REVIEW_12, 기존 계획·리뷰 이력과 현재 shared classifier/downloader/rate-limit/metrics/pipeline/CLI·테스트를 대조했다. 코드/배포/DB/cursor/예약은 변경하지 않았다.

## 이번 오류 범위와 사용자 pass 조건

사용자는 단일 URL의 카드사 등록 오류로 확인되면 DRM/ZIP처럼 개별 격리 후 계속 처리할 수 있다고 승인했다. 현재 증거는 그 조건에 해당하지 않는다.

10:47 현재 운영 image의 공식 adapter/downloader로 제한된2개 source를 다시 요청했다. 둘 다 landing 세션을 먼저 만들었으며 /state:ro SQL로 기존 SourceRecord만 읽고 다운로드는 임시 디렉터리에서 처리했다.

- 실패한 BCTOP source_c363707aef7be75165e225bf96369ac88cd7784f3393116411535400083a3c0f: PDFValidationError / response MIME type is not PDF-compatible.
- 별도 최신 상품500108(효력일2026-10-21): 동일한 PDFValidationError/MIME 오류.

이전10:08–10:09의 조건부 유무/다른 source/공식 direct URL 진단과도 일치한다. 최소 두 다른 PDF source가 공통 RAONK 경로에서 실패했으므로 **특정 단일 PDF URL만의 등록 오류라고 보고 pass할 근거는 없다.** 모든922개 PDF가 실패한다고 전수 검증한 것은 아니다. endpoint 장애/정책 변화의 정확한 원인은 미확정이며 프록시 해제와의 인과관계도 없다. 최신 진단은 /tmp/woori-scope-recheck-20261022T1047.jsonl에 보관했다.

개별 source에만 실패가 한정되고 다른 control PDF가 정상이라는 근거가 생기면 사용자 승인대로 격리/degraded 표기·정상본 보호·다른 문서 지속 처리하는 정책을 적용할 수 있다. 현 공통 서비스 오류를 같은 unsupported로 일괄 감추지는 않는다.

## FIX_30 판단

공통 transient 분류→기존 finite retry,503 Retry-After cooldown,typed collection/worker failure 및 PDF 성공/건너뜀 분리의 방향은 타당하다. 다만 **P2 오탐1건으로 최종 검증 통과를 보류하고 FIX_31을 발행한다.** 현재 active image에는 아직 FIX_30을 배포하지 않아 아래 신규 오탐이 운영 배치에 이미 반영된 것은 아니다.

classify_html_service_error는 HTML prefix 전체에서 문자열을 찾는다. 정상 카드 목록에 비활성 script 오류 사전 `const errors={temporary:"Service unavailable"}`가 있으면 classifier가 service_unavailable_phrase를 반환한다. 실제 RateLimitedClient issuer=kb GET의 임시 mock에서도 OriginServiceError가 나 정상 parser에 도달하지 못했다. 원래 오류 페이지와 정상 UI template의 구분이 없어 8사 공통 landing/목록/상세에 거짓 장애를 만들 수 있다. 실제 국민 사이트에 그런 template이 있다고 주장하지 않는다.

script/style/comment/hidden template 및 정상 상품 설명을 제외한 명확한 서비스 오류 근거를 요구하는 수정과 negative fixture를 FIX_31에 기록했다. 무조건 HTML 허용/PDF 검증 약화/무제한 retry를 요구하지 않는다.

## 직접 검사와 한계

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_30_origin_retry.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_017_scope.py -q
78 passed in9.19s

ruff check: issuer_collection/downloader/rate_limit/issuer_metrics/test_fix_30_origin_retry
All checks passed!
git diff --check
통과
```

78건 통과에도 신규 정상 template 반례가 빠져 있다. Executor 전체1844건 통과 보고를 이 반례 부재의 근거로 쓰지 않는다. Reviewer가 전체 suite를 반복 실행했다고 주장하지 않는다.

timer는 active/enabled/다음10/23 10:00KST 유지. 오늘 slot은 우리 실패이고 내일은 국민이라는 기존 rotation 정책은 변경하지 않았다. 새 image/원격 백업/배치 resume·실제 origin 복구는 미실행·미확정이다. LIVE_REVIEW_12에서 확인한 기존 신한 live/자동운영 인수 미완료 상태를 유지한다.
