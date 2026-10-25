# 019 LIVE_REVIEW_11 — 신한 및 수동8사 완료, WebDAV 재검증과 예약 경로 결함

2026-10-22 Codex, Reviewer. IP 재배정에 따른 백업 이상 가능성을 포함하여 최신 신한/수동8사 실행을 점검했다. status/summary, container 종료, 운영 seal·DB, 현재 WebDAV와 실제 MCP를 대조했다. 신규 배치·배포·systemd 변경은 하지 않았다. 신한의 공식 scoped backup flush 및 GET audit는 수행했으며 실제 업로드0이다.

## 신한과 수동8사

신한 run `666c2d2ee9e24d04acf76eed58123a15`, container `cardrag-live-shinhan-20261021T134616Z`, 10/21 22:46:17–10/22 01:15:06 KST, exit0/status succeeded/quality passed/collection complete다. OCR/structure/views958건 모두 succeeded, OCR failed0/structure failed0/cache publication deferred0. PDF887건(cache859/download28), OCR 재사용930/provider-called documents28이다. 문서 단위 지표와 provider page/chunk 호출 수를 구분한다.

신한 live931→958, 전체5605→5632. 신규 document ID28건, corpus diff new_products5/replaced_predecessors23/missing_unjustified0/retirement_candidates1이다. 신규 ID 수와 순증27을 동일하게 표현하지 않는다. 비선택 변경0, stable은 `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`다.

| 카드사 | 최신 수동 실행 | container exit | 비선택 변경 |
|---|---|---|---|
| 우리 | succeeded | 0 | 0 |
| 국민 | succeeded | 0 | 0 |
| 삼성 | succeeded | 0 | 0 |
| BC | succeeded | 0 | 0 |
| 현대 | succeeded | 0 | 0 |
| 하나 | no_change | 0 | 0 |
| 롯데 | succeeded | 0 | 0 |
| 신한 | succeeded | 0 | 0 |

**수동8사 배치 검증은 완료됐다.** 기존 7사 결과 snapshot 및 이전 scoped 검증을 근거로 한 것으로, retention으로 제거된 옛 run 모두를 이번에 새로 원격 검증했다고 주장하지 않는다. 삼성 과거 unbound2는 FIX_16의 정책 제외 후0으로 정리된 증거, 현대/BC는 보존된 last_check complete=true를 대조했다. 하나 no_change 및 deferred를 실패로 보지 않는다.

현재 serving: 우리980/국민757/삼성766/BC122/현대607/하나735/롯데707/신한958. 실제 MCP에서 신규 신한법인카드 아시아나클럽 page1/2(3144문자), 신한카드 The BEST-XO page1/13(218문자)를 읽었고 MCP current와 Worker stable generation이 일치했다.

## IP 변경 이후 WebDAV 현재 검증

종료 시 신한 backup_scope_complete=true, 37,130,761bytes/115requests, ledger 범위3740개 모두 matching receipts, 결손/충돌0이었다. 06:39 KST 현재 다음을 추가 실행했다.

```text
worker-compose.sh run --rm --no-deps worker backup flush --run 666c2d2ee9e24d04acf76eed58123a15 --issuer shinhan
status=unchanged backup_scope_complete=true index_read_error=null
in_scope_remaining/lost_source/conflicts/missing/unbound=0
uploaded_count=0 uploaded_bytes=0

worker-compose.sh run --rm --no-deps worker backup audit
status=succeeded sample_size=20 verified_count=20 failed_count=0
total_receipts=17224
```

scoped completion은 현재 원격 index를 읽어3740개 기대 경로/digest를 영수증과 대조한다. audit는 최근20개 객체를 실제 GET하여 크기·SHA를 검증한다. 전17224개 본문 전체 다운로드 검증은 아니다. flush 결과의 requests0은 반환 계수이며 원격 index GET이 없었다는 뜻은 아니다. `/tmp/shinhan-20261022-backup-check.json`, `/tmp/cardrag-20261022-backup-audit.json`에 결과를 보관했다.

현재 설정으로 원격 연결/인덱스/샘플 무결성이 정상이며 IP 재배정에 의한 백업 이상은 발견하지 못했다. IP 변경 시각이나 실제 WAN 주소 변화 자체를 외부 관측으로 입증한 것은 아니다. 남은 pending3개/30862bytes는 LIVE_REVIEW_10에서 확인한 이전 롯데 fallback cache이며 신한 범위 밖이다. 없애려고 unscoped flush·삭제하지 않았다.

## 별도 예약 실행 실패 — 전체 운영 인수는 보류

수동8사와 달리 **재부팅 후 오늘06:32:20 timer가 다시 활성화됐고 Persistent catch-up 실행이06:32:36–06:33:48 exit1로 실패했다.** journal의 결과는 worker_unexpected_failure다. 정확한 내부 예외는 현재 journal로 확정하지 않는다.

service는 `operations/worker-compose.sh`의 검증된 runtime overlay를 사용하지 않는다. base compose/secrets만 사용하여 `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:285326379739b802b33acd9e6136e473426762b66607f755c847418cdaf6c82b`를 pull했다. 수동 active release의 `cardrag-worker:019-fix24-11d1321cc9e6`와 다르다. compose project/volume의 차이도 검토해야 한다. 검증된 state volume의 최신 run은 여전히 신한 succeeded이며 MCP 게시도 신한 generation이다. 이 실패를 간밤 신한 수동 배치/WebDAV 실패로 오인하지 않는다.

현재 timer=active, service=failed, 다음 예정은10/23 03:00 KST다. 이번 확인에서 설정을 변경하지 않았다. 기존 문서의 timer inactive는 당시 관찰이며 현재 상태에 적용되지 않는다. 예약 실행 경로 일치를 같은019의 FIX_25로 발행한다. 수동8사 완료와 별개로 **019 종료·예약 자동운영 정상 인수는 아직 선언하지 않는다.** 가용80GiB/사용79%, 기존 미지원 DRM8건/역사 미해결1건은 별도 명시 상태다.
