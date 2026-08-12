# ADR-0003: 모델, 구조 분석과 품질 gate

- 상태: Accepted (합성 fixture 기준선)
- 일자: 2026-08-12

## 결정

초기 BULK OCR은 인증된 Codex exec의 `gpt-5.4`만 사용한다. 일일 증분은 동일 backend를
우선하고 승인된 재시도 뒤 OpenRouter `google/gemini-2.5-pro`를 fallback 후보로 둔다.
provider/model을 바꾸면 페이지 조각을 섞지 않고 문서 전체를 새 attempt로 처리한다. 실제 argv,
prompt hash, render scale과 입출력 hash를 provenance에 기록한다.

구조 분석 v1은 canonical OCR을 변경하지 않는 결정론적 rule baseline을 게시 기준으로 한다.
Schema-guided LLM은 `StructureEnhancer` 경계 뒤의 선택 기능으로 두며 exact source span validator를
통과해도 실제 gold 비교에서 rule baseline을 개선하기 전에는 기본 활성화하지 않는다. 이 선택은
외부 모델 장애 시에도 연회비·혜택·조건·실적/혜택 제외·필수 안내 taxonomy를 재현 가능하게 한다.

Embedding v1은 OpenRouter `openai/text-embedding-3-small`, 1,536차원과 versioned query/document
prefix를 선택한다. 응답 개수·순서·dimension·finite 값이 모두 일치할 때만 저장하며 한 요청의
query vector는 한 번만 만든다. 모델 자동 혼합은 금지한다.

## 정량 gate

합성 calibration/regression 기준은 다음과 같다.

- OCR 문자 정확도 ≥ 99.5%, 페이지 coverage/order 100%
- 숫자·단위·경계·부정·제외 token recall 100%, 중대 오류 0건
- 구조 source span 정확도 100%, 필수 taxonomy recall ≥ 95%, 중대 오류 0건
- 검색 전체 Recall@10 ≥ 95%, critical query Recall@10 100%
- MRR ≥ 0.90, nDCG@10 ≥ 0.90, filter 정확도 100%, issuer collision 0건

평균 점수가 높아도 숫자 변경, 부정 반전, 제외조건 누락, 근거 없는 사실은 실패다. 합성 fixture는
세 카드사, KB v9/v10, text/image형 PDF 생성, 표·각주·다중 페이지·숫자·제외조건을 포함한다.

## 근거와 한계

`reports/quality/fixture-gate.json`은 개발 가능한 contract/gate를 검증한다. 외부 모델의 실제
품질·비용과 카드사 live layout 대표성은 secret·계정·이용조건이 필요한 운영 인계 항목이며,
같은 evaluator로 재측정해 후속 ADR에서 모델이나 threshold를 교체한다.

## 검증

- `tests/fixtures/gold/gold_set.v1.json`
- `tests/unit/test_quality.py`
- `tests/unit/test_ocr.py`
- `tests/unit/test_structure.py`
- `tests/unit/test_embeddings.py`
- `scripts/run_fixture_quality.py`
- `reports/quality/fixture-gate.json`
