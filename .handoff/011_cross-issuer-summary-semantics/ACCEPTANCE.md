# 011 ACCEPTANCE — FIX_01 보완 후 개발 인수

작성: 2026-10-08 Reviewer. 승인 runtime commit **72c28ac** (`codex/011-summary-semantics`).

**판정: 개발 인수 가능. FIX_01 필수 보완은 완료됐으며 추가 FIX를 발행하지 않는다. 운영 반영은 아직 실행하지 않았다.** 기존 REVIEW.md는 수정 전의 판정 이력으로 보존하고 이 문서가 현재 판정을 기록한다.

## 1. 검토한 자료와 독립 확인

PLAN.md/PLAN_REVISION_01.md, REPORT.md/REVIEW.md/FIX_01.md/FIX_01_REPORT.md, c504b38→72c28ac 실제 runtime diff, 신규 의미 테스트, 고정34개+층화 무작위30개 source 및 before/after를 확인했다. Executor의 '통과' 문구만으로 인수하지 않았다.

- 독립 집중 테스트: summary_fields/summary_semantics/catalog_v122 **118 passed /1.24초**. Executor 보고의 MCP 전체883건과 구분한다. 확인된 필수 위험을 해결한 뒤 전체 테스트를 불필요하게 반복하지 않았다.
- Reviewer 재현5건을 실제 코드로 재실행했다. 연회비3건과 부정 후보2건 모두 해결됐다.
- 저장된64개 source를 같은 revision으로 직접 CatalogRepository._summary에 적용하고 연회비·혜택제목·혜택상세·조건을 비교했다. **64건 모두 Executor의 FIX_01 산출값과 일치**했다.
- 연회비61개는 v3 원문 기준치와 문자열 동일, 나머지3개도 `<br>`/공백 정규화 후 동일. 금액이나 브랜드/등급/본인 대상 정보가 바뀐 회귀는 확인되지 않았다.
- 현재 선택 evidence756개를 해당 source node와 대응 검사해 불일치0건. launch-date 검사는 이 source fragment 검사에서 제외했다. source 대응이 모든 의미상의 품질을 보장한다는 뜻은 아니다.
- 혜택 문장 전체가 조건에 반복되는 현상0건 유지. 신고4건 테스트도 통과했다.

증거: `evidence/reviewer-fix01-repro.json`, `reviewer-fix01-panel.json`, `reviewer-fix01-panel.py`, `reviewer-fix01-tests.txt`.

## 2. 필수 보완의 해결

### P1 연회비

연회비 문맥 TABLE_ROW를 혜택용 셀 분리에서 제외하고 기본료 면제 설명의 우선순위를 낮췄다. 상품별 분기 없이 source 행과 적용 대상을 보존한다.

- KB00917: 국내전용/실버 기본3,000+제휴10,000=합계13,000원 source 행 복원.
- 신한00549: MASTER 총15,000원(기본5,000+서비스10,000) source 행 복원.
- 신한00157: URS115,000/아멕스120,000 본인 source 행 복원. 기본료 면제 안내가 총연회비를 대신하지 않음.

### P2 부정 표 혜택

미적립/미제공 등 부정 판정에 최종 guard를 적용하고 label 제목도 실제 제공되는 offer cell이 있을 때만 생성한다.

- 온라인/5% 포인트 미적립: benefit이 아니라 condition으로 반환.
- 마일리지 적립/×: 혜택 제목·상세를 반환하지 않음.
- 국내5% 할인(무이자할부 제외): 긍정 혜택과 부분 예외 모두 유지하는 테스트 통과.

## 3. 범용성 및 실무 영향

runtime에 issuer/상품코드/특정 revision 또는 node ID 분기가 추가되지 않았다. 동일 문맥·표 역할로 처리하며 새 LLM/Worker parser/DB schema 변경은 없다.

연회비 외 출력이 달라진10개 상품도 변경된 항목을 확인했다. 일부 generic/table label 및 예시 총액 제목이 빠지고 실질 제목이 남는다. KB09058의 횟수·승인건당 금액은 조건으로 이동하고 actual 할인율은 혜택에 남는다. 렌탈/할인 표의 source row 표현이 길어지거나5개 제한 안에서 제목이 바뀌는 것은 이 범위에서 중대한 실무 회귀로 판단하지 않았다.

거래 예시 일부, 단독 표 금액의 해석, 제목의 빈 값·범위,5개 슬롯 누락 등 기존 보고된 한계는 남을 수 있다. 이를 숨기거나 완벽한 신규 상품 정확도를 주장하지 않는다. 추가 상품별 예외 규칙이나 오류0건을 강제하는 새 gate를 만들지 않는다. 이전 REVIEW에서 기록한 극단적인 HTML 줄바꿈 evidence 길이 방어는 후속 개선 대상으로 유지하며 이번 인수를 차단하지 않는다.

원래 사용자30개 목록은 여전히 미제공이다. 검증 대상은 기존34개와 새 층화 무작위30개이며 '사용자 원래30개 재검증'이라고 보고하지 않는다.

## 4. 운영 반영 지침

다음은 인수된 코드의 운영 반영 단계이며 **이 리뷰에서 실행한 작업이 아니다**.

1. 승인 commit72c28ac 기준 변경분으로 PR/통상 CI를 확인하고 main에 병합한다. 기존 공개 Release는 이번 인수만으로 변경하지 않는다.
2. 같은 serving generation을 사용하는 새 MCP 이미지만 준비한다. Worker code/config/image와 Opencode 설정은 현행대로 유지할 수 있다. 새 snapshot을 사용하는 경우 systemd가 읽는 Worker overlay와 절대 경로 wrapper도 현행 이미지/설정을 유지하도록 확인한다.
3. 운영 이름 cardrag-mcp, cardrag-mcp-state 및 LibreChat upstream/network를 유지한다. 이전 버전 이름으로 되돌리거나 새 volume을 복제하지 않는다. Worker 실행 중이라면 종료 후 전환한다.
4. root 작업은 이미 승인된 운영 방식에 맞춰 구체적인 cutover script를 준비한다. 대화형 sudo가 필요한 경우 사용자가 SSH에서 실행할 명령을 제공한다. MCP 교체 후 DB 초기 준비 최대15분을 허용하고 실패 시 현재 정상 배포를 복원한다.
5. 인증된 실제 HTTP로 신고4건의 혜택/조건·KB00917/신한00549/신한00157 연회비 및 기존500107을 확인한다. 같은 revision의 원문/contract bundle 및 LibreChat 중계 readiness도 점검한다. 완료 후 타이머 상태 확인과 배포 보고서를 작성한다.
6. 운영 rollback은 전환 직전 정상 배포1세트만 보존한다. 과거 source/image 정리는 활성 참조와 실제 rollback 의존성을 확인한 뒤 수행한다.

**재PDF/재OCR/재임베딩/전량 Worker/새벽03시2회/2일 대기는 인수나 이번 배포의 조건이 아니다.** 현행 데이터로 새 MCP를 구동하면 이번 요약 수정이 적용된다.
