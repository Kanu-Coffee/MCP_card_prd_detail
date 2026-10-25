# FIX_05 — 첨부 문서 오류 격리와 ZIP·DRM 일반 수집 절차

작성: 2026-10-19, Reviewer Codex. 이전 REVIEW.md 이후 사용자의 추가 정책 지시를 반영한 새 수정 과제다. 기존 코드 리뷰 승인은 당시 범위의 이력으로 보존한다. 이번에는 조사·과제 정리만 했으며 제품 구현·배포는 하지 않았다.

## 1. 사용자 방침과 범위

사용자는 잘못 업로드된 것으로 보이는 첨부를 DRM과 마찬가지로 오류 기록하되 나머지 수집을 정상 진행하고, 이후 업데이트 때 다시 확인하도록 요청했다. 이어 **특정 건 하드코딩을 금지하고 유사 사례를 처리하는 일반 프로세스**로 검토할 것을 명시했다.

따라서 DOCID·상품명·파일명·특정 hash allowlist를 추가해 우회하지 않는다. source/content fingerprint는 모든 문서에 공통으로 쓰는 오류 이력·캐시 키이며 특정 문서 예외 허가 목록이 아니다. 카드사 업로드 실수라는 해석은 추정으로 남긴다.

새 요구는 정상 ZIP 안의 PDF 수집, 읽을 수 없는 첨부의 문서 단위 격리, 기존 자료 보존과 재확인을 포함한다. ZIP 암호 해독이나 DRM 해제는 구현하지 않는다. 기존 증분/90일 분산 감사/선택 issuer/전체 serving 보존/독립 collect-process/백업 조건을 유지한다.

## 2. 조사에서 확인한 재현 자료

공식 공시 목록 531개 고유 DOCID를 6페이지 끝까지 조회했다. PDF 이외 첨부는 ZIP 1건이었다. landing+목록+해당 ZIP 총 8회, 수신 body 합계 450,523바이트. 이번 조사는 운영 Worker 실행이 아닌 별도 임시 프로세스였다.

- 상품 `toss place 카드`, DOCID 1893, 목록 BULT_SDT=20261012.
- 공식 ZIP `2023-A00026_20261012_10004.zip`: HTTP 200, application/zip, 87,750바이트, CRC 검사 정상. SHA256 `cd000a7644382650b344c4e75d39af332e3ac009149ee072ec6addb2e6d067f3`.
- 내부 파일 하나: `토스플레이스카드_신구조문대비표_2608.pdf`, 90,368바이트. ZIP 자체 암호 없음.
- 내부 바이트 시작은 `\x9b DRMONE`, 일반 PDF header 없음. 기존 validate_pdf가 ProtectedDocumentError/FASOO_DRMONE으로 판정한다. SHA256 `de885452f8fec09285d037fd492b65b747a1deb8febb8c6299d2c1cdf151f762`.
- 따라서 내부 조항은 읽지 못했다. 이름상 변경 대비표이며 프리미엄 부속 안내장 묶음은 확인되지 않았다. 이전 첨부 교체 이력과 게시 사유는 미확인이다.

이 값은 재현 fixture 근거다. 운영 코드의 조건문/예외 허용 목록으로 옮기지 않는다. 공식 상품공시실: https://www.lottecard.co.kr/app/LPCMNPD_V100.lc

## 3. 현재 코드에서 필요한 구조 변경

- `issuers/lotte.py:_pdf_url/parse_listing_page`는 .pdf 이외 파일명 하나로 목록 전체를 거부한다. 목록/상품 identity 검증과 첨부 처리 가능 여부 판정을 분리해야 한다.
- `pipeline.py`에서 IssuerCollectionError는 해당 issuer 다운로드를 quarantine하며 sibling 작업을 취소한다. 첨부 하나의 DRM·손상·지원 불가를 이 오류로 통합하면 사용자 요구를 충족하지 못한다.
- `downloader.py:validate_pdf/ProtectedDocumentError`, `origin_policy.py`, PDF CAS/cache, 기존 unsupported/실패 이력·carry·retirement/manifest·partial 실행 구조를 조사해 재사용한다. 단순히 모든 예외를 catch하고 성공 처리하지 않는다.

## 4. 공통 처리 절차

### A. 목록과 첨부 취득을 분리

목록 전체의 페이지 수·총건수·중복·상품 identity는 기존대로 검증한다. 첨부 확장자가 ZIP/지원 불가라는 이유만으로 다른 정상 행을 파싱 중단하지 않는다. 실패 첨부도 inventory에 관측된 존재로 남기며 단종/삭제로 해석하지 않는다.

상품과 첨부의 관계를 명시적으로 표현한다. 허용된 원본 origin/redirect 정책과 source identity 보호는 유지한다. 이 변경을 이용해 임의 host/path 파일을 받아서는 안 된다.

### B. 일반 첨부 분류 및 ZIP 검사

취득 시 MIME/확장자만 믿지 않고 실제 signature를 검사한다. 정상 PDF는 기존 strict hash/페이지 검증을 유지한다. ZIP은 제한된 임시 영역에서 멤버를 검사하고, 정상 PDF 멤버를 기존 PDF/OCR 경로에 전달한다.

파일 수·압축/해제 총크기·압축비 상한, path traversal/절대 경로/symlink, 중첩 archive와 암호화 ZIP 정책을 명시한다. 예산에 outer 다운로드와 실제 처리량을 계측한다. 지원하지 않는 구성은 문서 오류로 가시화한다. 정상 ZIP인지 여부를 특정 파일명으로 판정하지 않는다.

한 상품의 복수 안내장에는 안정적인 attachment/child identity와 원본 archive→member lineage를 유지한다. 순서 변경으로 child identity가 전량 바뀌거나 서로 덮어쓰지 않아야 한다. ZIP에 여러 상품이 들어 있는 경우 관계를 확인할 근거 없이 전부 같은 상품으로 게시하지 않고 해당 관계 불명확 항목을 보류한다.

상품안내장·바우처·브랜드 서비스·변경 대비표를 구별할 수 있는 metadata/분류 근거를 검토한다. 변경 대비표만 받은 경우 기존 전체 상품안내장을 대체하거나 완전한 최신 설명서로 판정하지 않는다. 근거 없는 파일명 추정은 분류 신뢰도와 미확정 상태를 남긴다.

### C. 문서 오류와 시스템/issuer 오류의 경계

DRM, 깨진 PDF/ZIP, 암호화·지원 불가 첨부 등 재현 가능한 문서 내용 문제는 source 또는 ZIP child 단위의 typed 오류로 기록한다. 정상 sibling 문서·다른 상품을 취소하지 않는다. 혼합 ZIP은 정상 멤버를 처리하고 실패 멤버를 추적하되 완전성 상태를 과장하지 않는다.

목록 pagination/schema/identity 불완전, 403/429·일반 연결 실패는 기존 issuer-level 보호·cooldown/예산 정책을 따른다. 로컬 디스크/SQLite/게시/공유 저장소/예상하지 못한 코드 오류는 문서 오류로 삼키지 않는다.

### D. 보존·게시·가시성

실패한 기존 문서의 이전 검증 결과와 current/history/lineage/vector 참조를 보존한다. 신규 문서에 이전 정상 결과가 없으면 미수집 오류/unsupported로 명시하며 내용을 만들어 채우지 않는다. 보존된 과거 내용을 오늘 취득한 최신 원본으로 표시하지 않는다.

run이 게시까지 진행될 수 있다는 사실과 첨부 전체가 정상이라는 사실을 분리한다. 문서별 reason/attempt/first_seen/last_checked/next_check와 오류·보존·신규 정상 처리 수를 보고한다. 일부 문서 오류가 남으면 실행 결과에 degraded/partial 지표를 명확히 노출하되 기존 CLI/스케줄러의 완료·재시도 계약과 호환되도록 설계한다. 정상 처리 증거 없이 100% complete라고 기록하지 않는다.

### E. 이후 업데이트와 재확인

목록 metadata/첨부 identity 변경은 기존 negative cache와 독립된 새 후보로 즉시 취득한다. 동일 URL/이름의 무표시 교체도 발견할 수 있도록 만료되는 오류 캐시와 원본 재확인 due를 영속화한다.

같은 오류를 매 배치마다 다운로드하거나 negative hit마다 만료를 뒤로 미루지 않는다. 기존 OriginPolicy의 짧은 관측/확인된 오류 재확인 및 90일 분산 감사 정책을 재사용하되 오류 종류별 TTL 근거를 보고서에 명시한다. 확인된 동일 오류도 영구 제외하지 않고 최대 90일 안의 분산 재확인 대상으로 유지한다. 최초 미확정 오류는 기존 짧은 관측 주기를 유지할 수 있다. 순회/예산 부족은 deferred/overdue로 표시하며 실제 확인 없이 due나 freshness를 갱신하지 않는다.

교체 후 정상 PDF/ZIP member를 확인하면 오류를 해소하고 해당 신규/변경 문서만 처리한다. 자동으로 이전 정상 자료를 먼저 삭제하지 않는다.

## 5. 필수 회귀와 제출

1. 상품명/DOCID/파일명을 바꾼 여러 fixture로 동일 경로가 동작해 하드코딩이 없음을 확인한다.
2. 정상 PDF, 정상 PDF 하나/여러 개 ZIP, PDF+DRM 혼합 ZIP, 전부 DRM, 손상 archive, 암호 ZIP, 지원 불가 파일을 포함한다. 잘못된 첨부 한 건으로 정상 sibling 수집이 중단되지 않는다.
3. 기존 정상 자료 있는 실패 source/처음 등장한 실패 source를 구분하고 current/history/vector·상품 수·실패 이력·retirement와 전체 serving을 대조한다. 오류 source가 inventory에서 사라지지 않는다.
4. 동일 오류 다음 배치 재다운로드 0, metadata 변경 즉시 재확인, 동일 URL 교체의 만료 재확인 및 복구, 캐시 만료 비연장, 90일/순회/예산 overdue를 날짜 시뮬레이션으로 확인한다.
5. collect→process에서 archive/member 오류·정상 PDF·출처와 보존 결과가 동일하게 이어진다. selected 밖 HTTP/OCR/후속 처리 0, 전체 serving 보존, 기존 skip/resume/seal 복구·선택 백업·GC pin이 유지된다.
6. issuer 네트워크·목록 오류와 SQLite/디스크/게시 오류가 문서 오류로 은폐되지 않는지 검증한다. 실제 origin을 대량 호출해 시험하지 않는다.

Executor는 실제 구현과 모델 변경을 조사한 후 이 요구를 충족하는 상세 결정을 내리고 `FIX_05_REPORT.md`에 명시한다. 관련 회귀/Worker 전체·ruff/diff 검증, 상태/manifest 소비 계약 변경 시 관련 전체 suite를 수행한다. 기존 handoff는 덮어쓰지 않는다. 이번 지시 정리를 구현·배포 완료로 해석하지 않는다.
