# v1.0.20 — 8사 수집과 검증

v1.0.20의 기본 수집 대상은 우리·KB국민·신한·삼성·현대·하나·롯데·BC카드입니다.
명시적으로 설정한 `CARDRAG_ENABLED_ISSUERS` 값이 기본값보다 우선합니다. 기존 배포의
환경변수에 4사가 고정되어 있으면 새 버전 설치만으로 해당 설정이 8사로 바뀌지는 않습니다.

```dotenv
CARDRAG_ENABLED_ISSUERS=woori,kb,shinhan,samsung,hyundai,hana,lotte,bc
```

## 수집 계약

신규 4사는 공식 공시 페이지의 상품 안내장을 수집하며 발급 종료 상품도 포함합니다.
현대·하나·롯데의 공식 목록은 개인·법인 상품이 섞여 있으므로 원안의 전체 목록을
유지하되 `shared`로 분류합니다. BC는 개인 안내장 열의 `personal` 범위입니다.
`classification_basis=official_shared_listing`을 보존하며 상품명 추정으로 법인 상품을
제외하거나 모든 상품을 개인 상품으로 표시하지 않습니다.
상품별 최신 문서 선택은 어댑터에서 완료하고, 같은 상품의 최신 문서가 서로 충돌하면
generation 생성을 중단합니다. 최초 실행은 카드사별 최소 25개 유효 레코드를 요구하며
이후 실행에는 기존 성공 기준 수집량 감소 감지도 적용됩니다.

| 카드사 | 공식 진입점 | 안정적인 상품 식별 기준 |
|---|---|---|
| 현대 | `www.hyundaicard.com/cpu/ug/CPUUG2001_04.hc` 및 상세 API | 목록의 `sqno` |
| 하나 | `www.hanacard.co.kr/OSA95000000D.web` 및 `.ajax` | `ADD_VAR3` |
| 롯데 | `www.lottecard.co.kr/app/LPCMNPD_V100.lc` 및 검색 API | `DOCID`; 공식 상품코드는 메타데이터 |
| BC | `www.bccard.com/app/card/ContentsLinkActn.do?pgm_id=ind0836` | 상품명·출시일과 안내장 구분에 따른 안정적인 해시 |

현대의 개정 이력은 날짜를 검증한 뒤 최신 날짜의 첨부파일만 선택합니다. 이전 이력의
빈 파일명은 현재 파일 선택에 영향을 주지 않으며, 최신 파일명이 비어 있으면 실패합니다.
공식 UI가 `-`로 표시하는 날짜 sentinel `99999999`가 있으면 최신성을 증명할 수 없으므로
해당 상품을 제외하고 경고를 기록합니다. 정상적인 빈 첨부 목록도 별도 경고로 남깁니다.
그 밖의 잘못된 날짜·응답 구조·최신 문서 충돌은 수집 실패로 처리합니다.

LG U+-현대카드M(`12396`, 버전 `20180822`)은 공식 원본이 SCDSA004 DRM 컨테이너입니다.
확인된 source ID·URL·버전·317,916바이트·SHA-256이 모두 일치할 때만 기존
`unsupported_drm` 계약으로 기록합니다. PDF로 간주하지 않으며 원본이나 바이트가 바뀌면
다시 실패하여 검토 대상이 됩니다.

하나의 `hasMore`는 첫 응답에서 문자열로 제공되고 후속 응답에는 생략될 수 있습니다.
`AMM_NEXT_KEY`를 따라 조회하며 반복 커서·진행 없는 페이지·조기 종료를 오류로 처리합니다.
후속 응답의 `listCount=0`은 최초 총건수를 덮어쓰는 값으로 사용하지 않습니다.
롯데는 이중 JSON을 파싱하고 서버 총건수에 도달할 때까지 조회합니다.

BC는 상품 안내장 열만 사용합니다. 같은 상품의 은행별·브랜드별 안내장이 여러 개이면
안정적인 구분 키로 보존하고 신청서 및 명시적인 법인 안내장은 제외합니다.
BC의 `effective_date`는 출시일이며 `date_basis=product_launch`로 이를 명시합니다.
출시일을 문서 개정일로 해석하지 않습니다. 파일 경로가 source version이며 같은 URL의
파일 교체는 기존 PDF 해시·원본 재검증 정책으로 감지합니다.

신규 4사의 OCR 구조화는 기존 무손실 generic issuer profile을 사용합니다.
별도의 카드사별 레이아웃 최적화나 검색 품질 통과를 수집 성공만으로 보장하지 않습니다.

## TLS 및 처리 호환성

현대카드의 레거시 TLS 예외는 `https://www.hyundaicard.com:443` 연결에만 적용합니다.
인증서 체인·호스트 검증과 기본 프로토콜·암호군 정책은 유지합니다. 다른 카드사,
WebDAV 및 모델 제공자 통신에는 예외를 적용하지 않습니다.
Discovery와 PDF 수집은 같은 클라이언트 생성 함수를 사용합니다.

v1.0.18의 PDF 수집 장벽·슬롯별 쿠키 격리·동시성 제한·병렬 후처리를 유지합니다.
v1.0.19의 WebDAV 14회·7일·신규 CAS 10GiB 주기 검증과 기존 PDF/OCR 캐시 계약도 유지합니다.
신규 카드사를 추가한 실행은 새 run ID로 시작합니다. 중단된 4사 run을 8사 설정으로
재개하여 기존 봉인 증빙과 섞지 않습니다.

## Candidate 및 릴리스

candidate의 정렬된 issuer 계약은 `bc,hana,hyundai,kb,lotte,samsung,shinhan,woori`입니다.
8사가 모두 존재해야 하며 과거 4사 증빙은 v1.0.20 합격 증빙으로 사용할 수 없습니다.
MCP gold draft도 8사를 대상으로 만들고 issuer별 평가 slices를 필수로 요구합니다.
기본 300개 질의와 24개 no-answer 예산은 유지하며 8사에 결정적으로 배분합니다.

새 증빙의 경로는 `release-evidence/v1.0.20/`입니다. 릴리스 workflow는 v1.0.20 태그,
같은 버전의 이미지·receipt, 봉인된 8사 검증 증빙을 요구합니다. 과거 증빙은 수정하지
않으며 candidate 채널 `candidate-v1.0.11`과 기존 v114 볼륨·프로젝트 이름은 유지합니다.
서빙 DB 스키마 및 MCP 도구의 요청·응답 구조에는 변경이 없습니다.

이미지 생산은 [기존 고정 빌드·검증 절차](V1_0_14_MIGRATION.md)의 builder/base image/
SBOM scanner digest 및 provenance 인자를 유지하고 아래 활성 버전 값만 사용합니다.
`CANDIDATE_SOURCE_COMMIT`은 변경 사항을 포함한 커밋이어야 합니다.

```text
--build-arg APP_VERSION=1.0.20
candidate-v1.0.20-worker-$CANDIDATE_SOURCE_COMMIT
candidate-v1.0.20-mcp-$CANDIDATE_SOURCE_COMMIT
```

과거 사고 복구 문서의 same-run resume 명령은 신규 8사 실행 지침이 아닙니다.
새 run에서 얻은 이미지 digest, 8사 Worker 지표·MCP smoke 및 평가 증빙으로
v1.0.20 receipt를 생성합니다. 과거 실행의 지표를 복사하여 버전 문자열만 바꾸지 않습니다.

소스·단위 테스트 성공은 실제 candidate acceptance를 대체하지 않습니다. 실제 8사
수집→OCR→임베딩→Export→MCP 검색·원문 조회와 기존 품질 게이트를 통과한 뒤
해당 실행에서 생성한 증빙을 봉인해야 합니다. 문서에 적힌 관측 건수는 고정 합격 기준이 아닙니다.

## 로컬 회귀 검사

```bash
uv lock --check
uv run --all-packages pytest
uv run --all-packages ruff check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
uv run --all-packages ruff format --check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1 tools
uv run --all-packages mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
actionlint .github/workflows/release.yml
```

## 2026-09-06 실사 결과와 원본 누락 처리

구현 검토 시 자동 테스트 1,935개가 통과했고, 운영 배포를 위한 원본 누락 처리 테스트를 추가했습니다. 8사 fixture의 Worker→WebDAV→MCP 검색·PDF
조회와 캐시 재사용을 포함합니다. Ruff 검사·포맷 확인, Mypy(81개 source 파일),
release workflow의 actionlint, lockfile 검증이 통과했고 세 패키지의 v1.0.20 wheel/sdist를
빌드했습니다. 실제 모델 제공자와 운영 WebDAV를 사용하는 candidate 평가는 수행하지 않았습니다.

기존 4사는 전체 Discovery와 각 대표 PDF를 검증했습니다. 신규 4사는 전체 Discovery
응답을 확인하고 선택된 안내장 URL 1,942개 전체에 production downloader 검증을 수행했습니다.
현대·하나의 최종 파서 검증에는 같은 날 수집한 전체 응답의 로컬 재생도 사용했습니다.

| 카드사 | Discovery 결과 | PDF 검증 |
|---|---|---|
| 우리 | 911개 | 대표 1개 성공 |
| KB | 750개 | 대표 1개 성공 |
| 신한 | 869개 | 대표 1개 성공 |
| 삼성 | 562개 | 대표 1개 성공 |
| 현대 | 577개 목록 → 573개 문서 | 572개 정상 PDF, 정확한 DRM 원본 1개 |
| 하나 | 73페이지·721행 → 720개 상품 | 720개 전체 성공 |
| 롯데 | 528개 상품 | 526개 성공, 공식 원본 404 2개 |
| BC | 112개 상품 → 121개 안내장 | 121개 전체 성공 |

현대의 제외 경고는 날짜 불명 `13843`, `12662`, `12518`과 첨부 없음 `12942`입니다.
BC는 신청서 1개와 명시적인 법인 안내장 1개를 제외합니다.

운영 배포 요청에 따라 다음 두 문서에 한해 검토된 `source_unavailable` 처리를 적용합니다.
전체 528행의 목록 검증을 먼저 마친 뒤 source ID·URL·version이 코드에 고정한 값과 모두
일치하고 해당 실행의 GET이 404인 경우에만 제외합니다. 200으로 복구되면 자동 포함하며,
상품 정보·날짜·URL 변경이나 그 밖의 HTTP 오류에는 예외를 적용하지 않습니다.
조회 결과는 526개와 경고 2개이며 이전 이력 PDF로 대체하지 않습니다.
Worker는 경고를 journal 및 `runs/<run_id>/discovery/lotte.warnings.json`에 기록합니다.

- `1603` 이엠이코리아 롯데카드 아임원더풀: 검색 목록이 오래된 404 파일을 가리킵니다.
  공식 이력에는 2024-12-02의 정상 PDF가 있으나, Discovery의 현재 문서 선택 계약을
  변경하지 않고 다운로드만 바꾸면 식별자가 불일치하므로 자동 대체하지 않았습니다.
- `1678` 캐시노트 롯데카드: 공식 첫 이력도 같은 404를 가리킵니다. 정상 다운로드되는
  다른 이력의 현재성을 입증할 수 없어 대체하지 않았습니다.

진단 요약은 [issuer-smoke.json](../release-evidence/v1.0.20/issuer-smoke.json)에 보관합니다.
이는 PDF 수집 진단이며 실제 OCR·임베딩·검색 품질 평가·candidate acceptance receipt가 아닙니다.
