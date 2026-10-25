# 019 LIVE_REVIEW_17 — 10/24 신한 완료와10/25 삼성 착수 확인

2026-10-25 14:32–14:34 KST Reviewer. 사용자 요청은 어제 배치 정상 여부와 오늘 착수 확인이다. 운영 설정/코드/DB 변경, 재시작, backup flush 없이 journal/run artifacts/serving 전후 비교 및 readonly backup 검증을 수행했다.

## 어제10/24 신한

- run7fb1aa5d3f9244e1b291c14e4ee533d4. 14:30기동→16:09:52 Worker 종료(5990.127초, 약100분). systemd 정상 종료, 최종status=succeeded/collection complete/published=true.
- 신한887건 discovery/획득 완료. OCR959건 성공(기존958재사용, provider1), OCR/구조실패0. 원본HTTP는신한만호출. 새 설명서1건, 기존 source retirement1건 기록; 이력은 보존했다.
- 전후 contract row 비교에서 변화는신한2행뿐, 다른7사 동일. 문서수신한958→959, 전체5632→5633. 우리980/국민757/삼성766/현대607/하나735/롯데707/BC122 유지.
- stable/MCP current 모두 g-7fb1aa5d3f9244e1b291c14e-9348e12d1043. 새 신한카드 JYP Fans EDM Check(00898), doc_826ae083115cc848c16499edc8aba23e297af9b81d3af804fae920db1a02aee4의 get_product/get_source_page(page1) 실제 MCP 호출 성공.
- 백업2091139bytes/7요청, 최종scope_complete=true, 범위내 missing/conflicts/lost/unbound/remaining0. SQL readonly+WebDAV GET으로 expected3688경로의 receipt/committed remote index를 추가 대조하여 complete=true 확인. 전역degraded/pending은 기존 범위밖3건30862bytes 때문이다. 새누락아님; 제외sharedcache56건. 마지막백업10/24 16:09:46 KST.
- quality_status=warning은 embedding 반복입력24샘플 중2건이 warning기준0.999미달이었기 때문. 최저0.9972087은 hard reject0.990보다 높아 현행정책상 통과다. 평균0.9997831. 실행실패/문서처리오류가 아니며 기준을 낮추거나 경고를 지우지 않았다.

## 오늘10/25 삼성

14:30 timer 발화, 현재container cardrag-worker-worker-run-2a488784f6fc 가 실제실행중이며 systemd activating/start다. Type=oneshot이므로 정상 실행중상태다. provider/tokenizer/기동검증 통과후 pipeline진입, samsung discovery566건 완료. 14:31:17 PDF566/566 성공/실패0, cachehits563/misses3, revalidation88/downloads91이다. 재검증다운로드가 포함되므로 downloads91을신규PDF91로보지않는다. 확인시점전체배치종료전으로 최종성공/백업완료는 아직판정하지않는다.

오늘도 embedding최저0.997209/24샘플중1건 warning이지만 동일hard gate정책상 기동통과다. 새오류/권한오류/원본수집실패는현재없다. timer active 및 LastTrigger10/25 14:30 확인. 실행중NextElapse가일시빈값이라는이유로예약설정을변경하지않았다.

실행중state DB는 readonly 조회시database locked였으므로 추가조회/이행/SQL보정/복구를시도하지않고 현재컨테이너로그·scope파일로착수를판정했다. 새run을실행하지않았다.

## 증거

/tmp/cardrag-shinhan-20261024-journal.log, result.json, backup-verification.json, backup-review.py. 최종결과와백업검증을 active operations/live/shinhan-auto-20261024.*.json에도보존했다. 후속삼성완주확인은별도시점에서수행한다. 추가FIX는필요하지않다.
