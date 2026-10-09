# 012 운영 경과 검토 — 2026-10-09 오전03시

**판정: 예약 배치 정상 완료 및 MCP 반영 확인. 수집 상태는 신한 원천 장애를 격리한 degraded다. 현재 예약 운영을 유지할 수 있다.**

## 확인 근거

- systemd:03:00:00 시작→06:00:08 종료, exit0/Result success. 현재 Worker service inactive, 실행 컨테이너 없음, timer active/다음10-10 03:00 KST. 일회성 Worker 컨테이너가 종료 후 제거된 상태는 정상이다.
- terminal JSON: run025ce35739944d8facfdda4c99961f0c, status succeeded/published true. generation g-025ce35739944d8facfdda4c-19895e1a91da.
- Worker performance/issuer collection/corpus diff/native OCR manifest를 읽기 전용으로 확인했다. 비밀/원본 운영 로그를 공개 보고서에 복사하지 않았다. 로컬 worker-state.sqlite3 읽기 시도는 실패하여 DB 내용을 근거로 사용하지 않았으며, run JSON 산출물과 journal/실제 HTTP로 확인했다.
- MCP generation.activated06:24:55 KST 및 인증 HTTP 확인. 변경된 롯데5상품의 실제 반환 PDF SHA가 새 OCR 대상 PDF와 모두 일치했다. 우리500107도 같은 새 generation에서1.2% 혜택·15,000원 연회비를 반환한다. MCP/LibreChat proxy healthy다.

## 실행 경과

| KST | 경과 |
|---|---|
|03:00–03:27|시작 사전검사·카드사 discovery·PDF 확보. 7개 카드사 성공, 신한 network 실패 격리·기존931문서 이월|
|03:27–03:37|이전 run의 재사용 가능한 OCR 산출물 탐색·대응|
|03:37–04:25|5,519문서 OCR/cache 처리와 구조 가공, OCR 실패0|
|04:25–04:56|검색 view·임베딩·export 및 전체 view/revision 검증|
|04:56–06:00|WebDAV 게시·검증 및 정상 종료|
|06:24:55|MCP가 새 generation 활성화|

시간대는 journal의 관찰 경계이며 각 단계 내부 작업이 모두 독립적인 것은 아니다. performance의 accumulated_seconds는 중첩·병렬 합산이 있어 wall time으로 단순 합산하지 않는다.

## 처리 성과

- 기존 current상품5,060 유지. 신규 상품0, 변경된 롯데약관5건 반영, 이전판 history454→459 보존. 총문서5,514→5,519. corpus diff의 missing_unjustified0, 단종 처리0.
- OCR5,514건 재사용, 새 provider 처리5문서/총39페이지, 실패0. 새5문서 manifest 모두 opencode/alibaba-token-plan/qwen3.8-flash/medium/succeeded. 신규 PaddleOCR 실행으로 확인되는 결과는 없다. provider_called_count5는 문서수이며 chunk별 LLM 호출 총수를 의미하지 않는다.
- 새 대상: Driving Pass1186(6p), I'm ACTIVE1640(10p), I'm Powerful1644(9p), 포인트플러스1634(4p), I'm CHEERFUL1545(10p). 실제 MCP의 개정판/document/PDF hash 대응5/5 확인.
- 임베딩 view616,336 검증 완료. 새 unique입력329, 본처리 provider batch6회. 시작 preflight24샘플은 별도다. 기존 벡터를 재사용했고 전량 임베딩 API 재호출로 해석하지 않는다.
- 원격 OCR publication deferred0. 원격 GC는 status null/삭제0으로 이번 실행에서 수행 완료로 주장하지 않는다. PDF cache prune은 succeeded/삭제0.

## 경고 및 남은 항목

1. 신한 원천 네트워크 장애 지속: 최신 수집은 미완료지만931건을 보존했고 나머지7개 카드사의 수집/후속 게시가 완료됐다. 이번 정상 배치가 신한 접속 복구를 의미하지 않는다.
2. 롯데1678/1603 PDF404, 현대 날짜 미확인3건/첨부 없음1건, 보호 문서 등을 포함 unsupported8건. BC의 제외 대상 집계는 parser 안내성 warning이다. 이 로그만으로 새 시스템 장애로 판정하지 않는다.
3. 구조 fallback2건은 이전 generation과 같은 문서·artifact hash다. 이번 신규 회귀가 아니며 failed0/원문 보존 정책을 유지한다.
4. 과거 개정판 미해결26→27. 추가1건은 document_identity_collision(source457f49c…). 현재상품 missing_unjustified0과 구분해 과거판 정합성 후속 대상으로 기록한다. 새 증가를 기존 경고로 뭉뚱그려 정상화하지 않는다.

## 시간·자원 검토

3시간은 캐시 활용에도 전체 로컬 처리/export/전송·검증 비용이 남기 때문이다. 이번 Worker performance는 WebDAV PUT 약15.02GB, 검증 GET 약30.02GB, PUT 누적약18.4분/검증 GET 누적약49.2분을 기록한다. 전체 검색 view616,336와 revision5,519 검증도 완료했다. 이 값은 네트워크 왕복/검증의 성능 개선 여지가 있음을 보여주지만 임의로 검증을 끄지 않았다.

Worker peak RSS약9.68GiB. 현재 루트 여유 공간약173GiB, 사용률55%, 시작/수출 capacity gate/WAL 검사는 통과했다. 메모리·전체 재가공·대형 artifact 검증의 경량화는 후속 개선 대상으로 삼을 수 있다.

이번 검토는 재실행·타이머/설정/코드 변경을 하지 않았다. 신한 재연결, 새 history 충돌1건, 성능 경량화는 후속 관찰/개선 항목이며 정상 게시된 이번 run을 즉시 반복 실행할 필요가 확인되지는 않았다.

증거: evidence/scheduled-run-20261009.json. 이 운영 검토 문서는 로컬 추가이며 GitHub 재발행/버전 변경을 수행하지 않았다.
