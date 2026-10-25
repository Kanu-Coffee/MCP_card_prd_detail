# FIX_06 — 사용자 정책 정정: ZIP 자체를 문서 오류로 격리

작성: 2026-10-19, Reviewer Codex. FIX_05 작성 직후 사용자의 명시적 정정 지시를 반영한다.

## 우선순위와 철회

**본 문서가 FIX_05보다 우선하며, Executor는 FIX_05를 별도로 구현하지 않는다.** FIX_05의 ZIP 해제, 내부 PDF 수집·선택, 복수 member/child identity, archive 내용 기반 문서 분류와 관련 회귀 요구는 철회한다. 기존 문서는 이력으로만 보존한다. 아직 구현·배포하지 않았다.

사용자 판단: ZIP 안의 자료도 잘못됐을 가능성이 있고 복수 PDF 중 정식 상품문서를 자동으로 결정할 수 없으므로, **ZIP 자체를 수집 오류로 분류**한다. 파일이 실제 잘못 업로드됐는지는 확정하지 않는다. 정책상 처리할 수 없는 첨부라는 판정과 원인 추정은 구분한다.

## 확정 처리 정책

1. 특정 카드/DOCID/파일명/hash 하드코딩 없이 모든 수집 대상 ZIP에 동일한 절차를 적용한다. 카드사 공통 첨부 정책으로 설계하고 현재 ZIP을 만나는 롯데 목록에도 적용한다.
2. 목록에서 ZIP 첨부가 확인되면 지원 불가 첨부 오류로 기록한다. 목록 자체는 관측한 상품·source identity를 유지하고 정상 행 처리를 계속한다. ZIP이라는 이유만으로 issuer 전체 목록을 실패시키거나 다른 상품을 취소하지 않는다.
3. ZIP을 풀지 않고 내부 PDF를 수집·OCR·파싱·게시하지 않는다. 내부에 PDF 하나만 있거나 일반 PDF처럼 보여도 예외를 두지 않는다. 복수 PDF 중 대표 문서를 추정하지 않는다. ZIP 전용 라이브러리/압축 해제 기능을 추가하지 않는다.
4. metadata에서 ZIP임이 명확하면 본문 다운로드도 생략한다. 원본 유형이 불명확하여 취득한 응답이 ZIP signature인 경우도 같은 문서 오류로 처리한다. 확장자/헤더의 불일치를 통해 ZIP이 일반 PDF 경로에 들어오지 않도록 한다. 기존 HTTPS origin·redirect·PDF strict validation은 유지한다.
5. 해당 source의 이전 검증된 상품문서·현재/과거 개정·OCR/vector 참조가 있으면 보존한다. ZIP을 새 정상 revision으로 등록하거나 기존 자료를 대체하지 않는다. 최초 등장해 보존 자료가 없으면 미수집 오류로 노출한다. 다른 상품 자료를 대신 연결하지 않는다.
6. 실패 첨부는 inventory에서 사라지지 않으며 단종·삭제 후보로 집계하지 않는다. 다른 정상 문서의 처리는 계속하되 전체 첨부가 정상인 것처럼 complete 100%를 보고하지 않는다. run 완료와 문서 오류 잔존을 구분해 관측 가능하게 한다.
7. 이후 선택 카드사 목록 조회에서 첨부 metadata/identity를 다시 확인한다. 계속 ZIP이면 본문을 반복 다운로드하지 않는다. 정상 PDF 경로로 교체되면 그 source만 즉시 기존 strict 취득·검증 경로로 처리한다. 기존 정상 자료는 교체 검증 전까지 유지한다.
8. 같은 URL이 PDF로 바뀌는 경우 등 metadata만으로 식별하기 어려운 변경은 기존 90일 분산 원본 감사·제한된 재확인 정책으로 탐지한다. 이때도 ZIP 내부는 열지 않고 원본 유형까지만 확인한다. 실제 원본 미확인은 freshness/감사 완료로 기록하지 않는다. 비선택 issuer를 추가 호출하거나 오류 캐시를 영구 제외로 바꾸지 않는다.
9. 문서 유형 오류와 목록 schema/pagination 불완전, 카드사 네트워크·차단, SQLite/디스크/게시 장애를 구분한다. 일반 예외를 모두 문서 오류로 삼키지 않는다. 기존 worker lock·선택 격리·collect/process·seal resume·보존·백업·GC 계약을 유지한다.

## 구현 검토 대상

- `issuers/lotte.py:_pdf_url/parse_listing_page`의 PDF 외 첨부에 대한 목록 전체 예외를 문서 단위 관측·오류로 분리한다.
- `pipeline.py`의 issuer-level quarantine과 문서 단위 unsupported/실패·보존을 구별한다. ZIP 하나가 issuer 전체 다운로드 실패로 전파되지 않게 한다.
- `downloader.py`의 실제 파일 형식 검증, `origin_policy.py`의 오류 캐시/재확인 및 기존 unsupported/retirement/manifest 경로를 조사해 재사용한다.
- 데이터 모델은 ZIP 원본을 관측했으나 PDF 취득/검증은 하지 않았다는 사실을 표현해야 한다. ZIP을 PDF CAS나 검증된 DRM PDF로 위장하지 않는다. metadata 관측만으로 바이트 hash·원본 확인 시각을 만들어내지 않는다.

## 필수 검증

- 상품/DOCID/파일명을 바꾼 복수 fixture에서 ZIP을 동일하게 문서 오류로 분류한다. 내부가 단일 PDF/복수 PDF/DRM이어도 실행 시 archive를 열지 않는 spy를 둔다.
- 정상 PDF와 ZIP 혼합 목록에서 정상 문서 수집·처리·게시가 진행되고 ZIP은 오류로 남는다. 명시 ZIP은 본문 GET·압축 해제·OCR·embedding 0건이다.
- 확장자는 PDF이나 실제 응답이 ZIP인 경우에도 오류 격리·바이트 계측이 정상이며 sibling 작업이 취소되지 않는다.
- 이전 정상 자료 있는 상품/없는 상품을 구분해 보존·미수집 보고·retirement 동결을 검증한다. ZIP 관측을 정상 PDF 취득·최신 serving 반영으로 기록하지 않는다.
- 다음 배치 동일 ZIP은 본문 다운로드 0, 정상 PDF로 metadata 변경 시 해당 문서만 재취득, 같은 URL 교체는 bounded 재확인에서 탐지한다. 재확인·90일 due·overdue를 정확히 유지한다.
- 선택 issuer 외 처리 0, 전체 serving 보존, collect/process 및 resume/seal·백업·보존 경계를 회귀 검증한다. 목록·네트워크·공유 저장소 실패 보호는 유지한다.

Executor는 본 문서와 FIX_05의 공통 오류 격리·보존 요구 중 본 문서에 부합하는 내용만 구현하고, **FIX_06_REPORT.md**를 새로 작성한다. FIX_05_REPORT는 작성하거나 완료로 가장할 필요가 없다. 관련/Worker 전체 테스트와 ruff/diff 검사를 수행하고 변경·검증·미수행을 구분한다. 운영 배포나 ZIP 해제 구현이 이번 정리 과정에서 수행된 것은 아니다.
