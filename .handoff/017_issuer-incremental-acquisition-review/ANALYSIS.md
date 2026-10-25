# 카드사별 수집 부하·검증 구조 실사 — 2026-10-19

작성: Codex. 최초 조사 2026-10-19, 같은 날 08:20 KST 이후 재검토·개정. 기준 HEAD `aaeb2c4`, 구현 `d8a9018`, 운영 `cardrag-worker:016-d8a9018`.

사용자의 명시적인 문서 수정 요청에 따라 이 문서와 `PLAN.md`를 현재 상태로 개정했다. 최초 수집 부하 분석은 유지하고, 별도로 구성된 프록시 현황 및 카드사별 실행·일일 순회 설계를 추가한다. 이번 작업은 플래너의 조사·문서 수정뿐이며 구현·배포·017 개발·VPN/라우팅/서비스 설정 변경은 하지 않는다. 기존 `016/CONNECTIVITY_REVIEW_20261017.md`도 보존한다. 실제 서버 주소·프록시 URL·SSH 키·VPN 설정은 저장소에 기록하지 않는다.

**읽는 기준:** §2–7의 배치 수치와 PDF 규모는 10/19 새벽 배치/최초 조사 시점의 증거다. §8은 이번 재검토에서 확인한 사실과 사용자가 전달한 운영 검증 범위를 구분한다. §9–10은 아직 구현되지 않은 설계 결정과 확인 과제다.

## 1. 핵심 판정

- **매번 PDF 전량을 원격에서 다시 받지는 않는다.** 목록은 매일 조회하지만 동일 source의 정상 로컬 캐시는 기본 168시간 재사용한다. 이후 조건부 GET 또는 원본 전체 다운로드로 재확인한다.
- **로컬 PDF 검증은 캐시 hit에도 매번 수행한다.** SHA256·크기·전체 페이지 구조를 검사하며, 이 디스크 읽기를 카드사 다운로드량으로 계산해서는 안 된다.
- 주요 개선 대상은 전 상품 목록·상세의 매일 반복 조회, 7일 만기의 대량 원격 재검증, DRM의 매일 재다운로드, 단계 전체 재시도, 실제 전송량 계측 부족이다.
- **일별 신규·변경분 취득 + 90일에 나눈 원본 감사**로 변경할 수 있다. 다만 파일명·게시일을 그대로 둔 원본 교체는 감사일까지 발견이 늦어질 수 있다.
- 원격 서버에서는 HTTP 200, 기존 서버에서는 timeout/reset이라는 차이는 IP·경로 관련 가능성을 뒷받침한다. 카드사가 대량 다운로드를 공격으로 판정했다는 확정 로그는 없으며, 이것을 차단 원인으로 단정하지 않는다.
- 현재는 Oracle 경유 HTTP 프록시와 자동 유지 SSH 터널이 별도 구성되어 있다. 운영 Worker에 대한 host별 프록시 기능은 아직 없고, 일부 페이지 HTTP 200이 목록·PDF 수집 복구를 뜻하지 않는다.
- 기존 `CARDRAG_ENABLED_ISSUERS`는 adapter 활성화 기능이다. 비선택 카드사의 전체 serving 보존 및 OCR/후속 처리 제외까지 보장하는 기능으로 확인된 것은 아니다. 선택/순회 모드에는 처리 범위와 게시 범위를 분리하는 추가 설계가 필요하다.

## 2. 조사 범위와 운영 설정

8개 adapter, issuer HTTP/오류 분류, pipeline, PDF cache/downloader, rate limiter, state/contracts/settings/Compose를 조사했다. 운영 증거는 10/16–19 journal, Worker SQLite, 보존된 10/18·19 reports, 현재·직전 serving manifest다.

최초 조사 및 이번 재검토에서 Worker service는 inactive/result=success/exit=0, timer는 active이고 다음 실행은 10/20 03:00 KST였다. 최초 조사는 Worker DB의 WAL 없음과 writer 부재를 확인한 뒤 immutable read-only로 조회했고, 조사 컨테이너는 network=none, 볼륨 read-only였다. 이번 문서 개정에서는 DB를 추가 변경하거나 카드사 목록/PDF를 새로 호출하지 않았다.

| 항목 | 현재 값 |
|---|---|
| 원본 재확인 간격 | `CARDRAG_PDF_CACHE_REFRESH_HOURS=168` |
| PDF 동시성 | 전체 8 / issuer 2 |
| discovery 동시성 / deadline | 4 / issuer당 300초 |
| 최소 요청 시작 간격 | 8개 issuer 모두 0.25초 |
| 단계 최대 시도 | 모두 4회 |
| 원격 요청 수·body bytes 일일 예산 | 별도 상한 없음 |

0.25초는 대역폭 제한이 아니라 issuer당 약 4회/초의 요청 시작 간격이다. 실제 host별 동시성도 제한한다. 매일 03시에 작업이 집중되며, 장기 실패에 대한 run 간 cooldown은 없다. 강제 원본 검증은 pipeline 기본 false이고, 실제 Compose에서 별도로 켜지 않았다.

## 3. 실제 최근 배치의 다운로드·재검증

| 실행일 KST / run | 정상 PDF 다운로드 | 원격 재검증 | 304 | PDF byte revision | cache hit / miss | 결과 |
|---|---:|---:|---:|---:|---|---|
| 10/17 / c9d55b04… | 9 | 1 | 1 | 0 | 3,643 / 17 | succeeded / degraded |
| 10/18 / 73f12ad3… | 390 | 2,504 | 2,114 | 0 | 3,652 / 8 | succeeded / degraded |
| 10/19 / f1871a51… | 0 | 0 | 0 | 0 | 3,652 / 8 | succeeded / degraded |

cache hit에는 만기 후 원격 재검증한 대상도 포함된다. 따라서 hit가 곧 통신 0건이라는 뜻은 아니다.

**집계에서 빠지는 DRM 전송:** 최신 skipped stage와 대조하면 우리4 / KB3 / 현대1, 합계 8건이 DRM이다. body를 받은 다음 hash/magic 검사에서 `ProtectedDocumentError`가 발생하며 PDF CAS에 bind되지 않는다. 다음 실행에서도 miss이므로 다시 body를 받는다. `pdf_downloads.add`는 정상 ingest 이후에 있어, download=0인 10/19에도 이 8건의 전송은 발생했다. 현재 코드에는 미등록 protected 문서도 warning+unsupported로 처리하는 경로가 있으므로 allowlist 7건과 실제 8건은 다르다.

10/18의 저장된 원본 관측시각과 run 구간을 대조한 재검증 내역은 BC120 / 하나715 / 현대543 / KB747 / 삼성379로 합계 2,504다. 삼성379는 validator가 없고 저장 PDF 길이 합계는 **144,008,093 B(144.0MB)**다. 나머지 정상 다운로드 11건의 개별 body bytes는 현 이력만으로 확정할 수 없다. 144.0MB는 저장 길이로 재구성한 하한이지 TLS·header·압축·retry를 포함한 실제 wire 계측값이 아니다. 304 대상의 PDF 저장 길이를 수신량으로 더하지 않았다.

10/19는 정상 PDF 다운로드 0건인데도 DB 기준 약 **87분36초** 걸렸다. OCR provider documents=0, OCR cache reused=5,536, embedding provider call count=0이었다. 이 마지막 수치는 본문 처리 단계이며 startup sample 호출은 별도다. 로컬 재처리와 export 비용은 원격 수집 부하와 분리해야 한다.

10/19 새벽 배치 outcome 기준 신한은 16연속 실패/931문서 carry, 마지막 수집 성공은 10/06 03:05 KST다. 롯데는 8연속 실패/647문서 carry, 마지막 성공은 10/13 03:06 KST다. 두 회사는 해당 배치에서 discovery 실패 후 download skipped다. 이 기록은 프록시가 Worker에 적용된 뒤의 수집 결과가 아니며, 실패 당일 PDF 전량 다운로드가 차단 원인이었다고 확정할 수 없다.

## 4. 공통 검증 흐름

일반 새 run에서는 8개 adapter의 `discover_current`를 호출한다. 같은 run의 resume에서 성공 snapshot을 복원하는 예외가 있으나, 다음 날 새 run이 자동으로 전날 목록을 건너뛰는 것은 아니다.

`pipeline.py:3742` 이후 source별 동작:

1. discovery payload로 source_id를 계산한다. 관측 시각은 제외하지만 상품명·metadata·파일명·version·URL은 포함한다.
2. `PDFCache.lookup`에서 regular file/비symlink/안전한 경로와 hash·크기·페이지 구조를 검사한다.
3. 정상 hit이고 마지막 원본 확인 후 168시간 미만이면 prepare_download와 원본 HTTP를 모두 생략한다.
4. miss 또는 만기이면 prepare_download를 실행하고, 저장된 ETag/Last-Modified가 있으면 조건부 요청 헤더를 넣는다.
5. 304이면 body를 받지 않고 원본 확인 시각을 갱신한다. 200이면 body를 받아 MIME/magic/hash/페이지 구조를 검사하고 CAS에 등록한다.
6. 이후 OCR·embedding은 각각의 별도 캐시를 사용한다.

따라서 validation을 하나로 묶어 끄면 안 된다. **목록 구조 검증, 로컬 무결성 검증, 원격 원본 최신성 확인, 새 파일 안전성 검증**을 분리해야 한다. 새로 받은 파일의 strict 검증은 유지한다.

## 5. 어댑터별 현황

아래 요청 수는 정상 단일 성공 시도의 코드상 식이다. 실제 HTTP 총수를 측정한 값은 아니다. `P`는 목록 페이지 수, `D`는 상세 조회 상품 수, `T`는 게시글 총수다. retry·redirect·첨부 없는 행에 따라 증가한다.

| 카드사 | 매 실행 목록 조회 | PDF 필요 시 전처리 | 현재 변경 판단 자료 | 개선 포인트 |
|---|---|---|---|---|
| 우리 | landing GET + main JSON POST + 모든 대상 상품 detail POST: **2+D** | 원격 prepare 없음. RAONK form 생성 후 PDF POST | code, detail beginDt/gdccVer, filePath/fileNm | 약 900상품 상세 반복. main의 pageSize=1000 고정·전체 건수/페이지 검증도 점검 |
| KB | landing GET + 5개 category 전 페이지 POST: **1+ΣP** | 원격 prepare 없음, 직접 PDF GET | 상품코드, 목록 날짜, PDF path/stem, category | 경량 전체 목록 비교 유지 가능. 원본 감사만 분산 |
| 신한 | PC landing GET + 신용/체크 cursor 전 페이지 POST | **PDF마다** mobile landing GET + 상품명 검색 AJAX 전체 페이지 + PDF GET. 불일치면 빈 검색 전체 목록 fallback까지 추가 | 상품코드·이름·공시일·stable source identity. mobile file token은 전송용 | 원본 1건의 준비 호출이 많음. 배치 내 token map과 session 범위 재사용 검토 |
| 삼성 | landing GET + JSON 전 페이지: **1+ceil(T/100)** | 로컬 identity 확인 후 filedownload GET | official code/post ID/**sysLtChTs**/attachment group·sequence/variant | metadata가 풍부해 차분화 유리. validator 없는 원본의 주간 재수신 축소 |
| 현대 | landing 상품표 GET + 모든 상품 detail API POST: **1+D** | 원격 prepare 없음, 직접 PDF GET | sqno, detail 적용일, PDF 파일명/path, 발행상태 | 우리와 같은 N+1 조회. 상위 표만으로 원본 변경을 알 수 있는지 미입증 |
| 하나 | landing GET + cursor 목록 전 페이지 POST: **1+P**. 과거 행에서 상품별 최신 선택 | 원격 prepare 없음, 직접 PDF GET | code, AN_SDT, APN_FILE_NM/PH_NM, 발행상태 | 과거 목록도 순회. 정렬 보장 없이 선두 페이지만 보고 중단하면 누락 위험 |
| 롯데 | landing GET + 100건 단위 POST: **1+ceil(T/100)** + 알려진 404 source 2건 stream GET 확인 | 원격 prepare 없음, 공식 image CDN GET | DOCID, 적용일, OCY_FILE_NM, 발행상태 | 실패 시 cooldown. 404 반복 확인도 negative cache 후보. 모바일 top은 현행 discovery 입구가 아님 |
| BC | 단일 HTML GET에서 여러 guide link 추출 | 원격 prepare 없음, 직접 PDF GET | root product+semantic variant, path/파일명. source_version 자체가 path | 원래 목록 부하가 작음. 같은 path의 원본 교체는 분산 감사로 보완 |

추가 주의:

- 우리는 상품별 detail에서 최신 PDF 한 개만 선택한다. 최신 목록 919 source에 대응하는 상세 조회가 매일 반복되며, 코드상 main 조회만으로 버전을 알 수 있다고 입증되지 않았다.
- 현대도 577 PDF source를 얻기 위해 상품별 상세를 조회한다. 첨부 없음/날짜 불명 상품은 추가로 조회하고 제외될 수 있다.
- 신한 PDF 준비의 이름 검색은 최대5페이지, fallback 전체 검색은 최대100페이지다. 일반 PC discovery는 cursor 반복/전체300초 제한은 있으나 호출부에서 별도의 최대 페이지·건수 인자를 전달하지 않는다.
- 삼성 목록 정렬은 **sysFstRgTs(최초등록)**다. 최종 변경 sysLtChTs가 있어도 선두 신규 페이지에서 일찍 중단하면 과거 게시물 개정을 놓칠 수 있다. 가벼운 전체 목록 비교가 오히려 적절할 수 있다.

### 현재 PDF 규모와 HTTP validator

6개 정상 카드사는 10/19 discovery를 현재 PDF binding과 결합한 값이다. DRM은 정상 PDF 바이트에서 제외했다. 단위는 10진 MB다.

| 카드사 | discovery source | 정상 PDF binding | 전체 body 취득 상당 저장 길이 | 저장 ETag/Last-Modified |
|---|---:|---:|---:|---|
| 우리 | 919 | 915 | 942,673,479 B / 942.7MB | 없음 |
| KB | 751 | 748 | 687,486,185 B / 687.5MB | 748/748 모두 보유 |
| 삼성 | 565 | 565 | 221,604,252 B / 221.6MB | 없음 |
| 현대 | 577 | 576 | 225,325,308 B / 225.3MB | 576/576 모두 보유 |
| 하나 | 726 | 726 | 530,748,620 B / 530.7MB | 726/726 모두 보유 |
| BC | 122 | 122 | 301,689,966 B / 301.7MB | 122/122 모두 보유 |
| 합계 | 3,660 | 3,652 | **2,909,527,810 B / 2.91GB** | validator 없는 source 1,480건 |

같은 PDF를 여러 상품이 참조하면 source별 길이를 더했다. CAS 고유 저장량과 다르다. 2.91GB는 이 6사의 cache cold miss 또는 모든 원본이 200 body를 반환할 때의 참고 규모이며 매일 받는 용량이 아니다.

실패 중인 신한·롯데는 오늘 fresh 전체 용량을 확정하지 못한다. 현재 serving carry는 신한931문서·**1,293,695,573 B**, 롯데647문서·**612,486,875 B**이며 과거 개정도 포함한다. active cache만 보면 신한886 bindings·1,202,290,282 B(validator 없음), 롯데530 bindings·453,185,875 B(두 validator 보유)다. 모수가 달라 복구 후의 전체 다운로드량으로 바로 쓰지 않는다.

8사 serving은 총5,536문서·PDF 참조 길이 **5,005,491,519 B(5.01GB)**이고 과거 개정이 포함된다. active cache source 5,071건의 참조 길이 합계는 **4,567,738,451 B(4.57GB)**다. 이 두 숫자도 wire 통신량이 아니다.

## 6. 요청한 6개 항목에 대한 판단

**1. 실행마다 전량 다운로드인가?** 아니다. 목록은 전수, 정상 PDF는 캐시 재사용이다. 신규/metadata identity 변경/로컬 유실·손상은 취득하고, 7일 만기분은 조건부 요청 또는 전체 body 재수신한다. 정확한 목록·PDF wire bytes/day는 아직 계측되지 않는다.

**2. 매번 validation하는가?** 목록 구조·중복·건수/retention 검증과 로컬 PDF 무결성은 매번이다. 원격 원본 재확인은 만기/신규 등의 경우다. 로컬 검사 주기만 바꿔서는 카드사 요청 수가 줄지 않는다.

**3. metadata/파일명만으로 판단 가능한가?** 변경 후보 선택은 가능하지만 바이트 동일성 보장은 불가능하다. 삼성 최종변경시각, 우리 detail version, KB/하나/롯데 날짜·파일명을 활용한다. 조건부 GET/304는 이미 구현돼 있다. HEAD를 전 파일에 매일 보내면 요청 수는 여전히 전수이고 POST/서명 방식은 validator를 제공하지 않을 수 있다. Content-Length나 앞부분 Range 일치만으로 전체 내용이 같다고 판정하지 않는다. 참고: [RFC 9110 조건부 요청](https://www.rfc-editor.org/rfc/rfc9110.html#section-13), [HEAD](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.2).

**4. 신규만 취득하고 전체 검증은 3개월인가?** 가능하나 **신규+기존 변경분**을 즉시 취득하는 것을 권장한다. 알려진 source의 metadata가 같고 local cache도 정상이면 원격 요청을 생략할 수 있다. 동일 URL·파일명의 무표시 교체는 최대90일 발견이 늦어질 수 있다. 새로 받은 파일 검증은 유지한다. refresh_hours=2160만 적용하는 것은 단기 방안이며 90일 뒤 동시 만기 폭주를 막는 분산 스케줄이 함께 필요하다. 이번에는 설정을 바꾸지 않았다.

**5. 매번 전부 호출하지 않는 방법은?** 최소한의 목록/공식 변경 신호 확인은 필요하다. 완전한 변경 feed가 없는 상태에서 호출0으로 신규 유무를 알 수는 없다. 목록을 저장 inventory와 비교하여 new/changed/audit_due/local_corrupt만 queue한다. 나머지는 PDF GET/HEAD/prepare를 0건으로 만든다. 우리·현대의 기존 상세도 순환 조회로 줄일 수 있지만 상위 목록에 버전이 없다면 기존 상품 개정 발견도 그 주기만큼 늦어진다. 이 지연을 허용하지 않으면 공식 변경 지표를 확보하기 전까지 상세 전수 조회가 필요하다.

**6. 추가 개선은?** 아래 우선순위를 권장한다.

1. **DRM·404 negative cache**: 내용 식별자, 사유, 마지막 시도·다음 시도를 저장한다. metadata 변경 시 해제하며 영구 제외하지 않는다. 미등록 DRM은 경고와 제한된 확인을 남긴다.
2. **90일 분산 감사**: 현재 후보를 정상6사3,660 + carry신한931 + 롯데647로 단순 추정하면 약5,238 source다. 전체 기준 하루 평균 약59건, 카드사별 올림이면 약64건이다. active cache 4.57GB를 전량 body 기준90분할한 평균은 약50.8MB/일이다. 모수·큰 파일·신규분을 감안해 건수와 byte 예산을 함께 관리한다. 실제 validator의 304가 있으면 body는 더 줄 수 있다.
3. **지속적인 cooldown/circuit breaker**: 403/429/연속reset/timeout 후에는 추가 호출을 줄이고 제한된 probe만 허용한다. Retry-After와 일일 요청/byte 예산을 적용한다. 현재 issuer 실패 후 격리·carry는 있으나 단계4회 재시도와 다음 run까지의 지속 억제는 별도 개선이 필요하다.
4. **실제 원격 부하 계측**: issuer/host/run/목적별 method·status·수신 bytes·retry·중단 사유를 집계한다. DRM·실패·redirect도 포함하고 cookie/token/raw form은 저장하지 않는다.
5. **차분 snapshot 계약**: 지금 snapshot은 전체 목록과 최소 건수/retention을 전제로 한다. delta3건을 그대로 전달하지 말고 저장 inventory와 합성한 complete view를 검증해야 한다. 불완전한 조회나 감사 유예를 단종으로 처리하지 않는다. fresh/stale/audit-deferred를 구분한다.
6. **안정적인 원본 재사용 식별자**: 현 source_id에 상품명/발행상태가 포함돼 표시만 바뀌어도 cache miss가 될 수 있다. 기존 이력을 보존하면서 내용 identity와 표시 metadata, 전송 token을 분리하는 재사용 계층을 검토한다. 서명 URL을 근거 없이 같은 원본으로 취급하지 않는다.
7. **불필요한 재export**: 10/17–19 corpus hash가 같은데 contract hash가 매일 달라졌다. 원시 OpenRouter metadata 전체 hash가 contract에 들어가는 구조가 원인 후보지만 유일한 원인으로 확정하지 않았다. 의미 계약과 관측 증거 분리를 검토하고, 로컬 동일CAS 중복 검증도 안전한 범위에서 줄인다. 이 개선은 카드사 다운로드 감소와 별도다.

## 7. 검증·증거·한계

`uv run --no-sync pytest apps/cardrag-worker/tests/test_pdf_cache.py apps/cardrag-worker/tests/test_pipeline.py -k 'pdf_cache or validatorless' -q`: **28 passed, 129 deselected / 0.64초**. 기존 TTL·304·validator 없는 재다운로드·byte 개정·보호 조건의 오프라인 회귀다.

원본 증거는 `/opt/cardrag/017-evidence/`의 `worker-journal-20261016-19.log`, `journal-run-results.json`, `runtime-collection-settings.json`, `inventory.json`, `serving-inventory.json`, `origin-window-estimate.json`이다. 실제 HTTP 총수·전송량은 현 telemetry로 확정 불가하다. 오래된 run-local report는 native retention으로 남지 않아 최근2건과 journal/DB를 구분해 사용했다.

제품 구현과 운영 반영은 아직 수행하지 않았다. 실행 가능한 후속 범위·보존 조건·인수 기준은 같은 task의 `PLAN.md`에 기록한다.

## 8. 프록시 구성 현황: 확인된 사실과 미검증 사항

### 이번 재검토에서 읽기 전용으로 확인

- `cardrag-oracle-tunnel.service`는 active/running, ExecMainStatus=0이다. 서비스 정의는 SSH local forwarding, WireGuard 내부 peer를 사용하며 Restart=always/RestartSec=5로 유지된다. 호스트 loopback용 및 Worker Docker bridge용 프록시 포트의 LISTEN도 확인했다. SSH 키 내용은 열거나 저장하지 않았다.
- 실제 운영 Compose는 계속 `cardrag-worker:016-d8a9018`와 전체8사 `CARDRAG_ENABLED_ISSUERS`를 사용한다. `CARDRAG_ISSUER_PROXY_URL` 및 전역 proxy 환경변수는 Worker에 전달되지 않는다.
- 저장소 `issuer_http.py:create_issuer_client`에는 현대 전용 TLS mount만 있고, 카드사 proxy 설정·host별 proxy mount는 없다. 따라서 프록시 인프라 구성 완료와 Worker 연결 완료는 구분해야 한다.

### 사용자 제공 운영 검증 사실

사용자는 Oracle 공인 출구를 경유하는 HTTP 프록시를 구성했고, 자동 터널은 WireGuard 내부 peer로 접속한다고 전달했다. 호스트용과 컨테이너용 URL이 별도로 있으며, 운영 Worker 이미지·Docker 네트워크에서 출구 IP 및 신한·롯데 페이지 HTTP 200을 확인했다고 명시했다. 이 페이지·출구 검증은 이번 플래너가 다시 실행한 시험으로 표현하지 않는다.

### 아직 검증하지 않은 범위

신한·롯데의 실제 목록 API POST, pagination, cookie/세션, PDF GET/stream/redirect/304 및 PDF 검증은 미완료다. Worker 재시작·터널 단절 시 동작, 목록과 PDF의 실제 프록시 선택도 제품 경로에서는 미검증이다. HTTP 200 자체가 정상 공시 목록 또는 정상 PDF임을 보장하지 않는다.

### 계획에 반영한 결정

설정명은 `CARDRAG_ISSUER_PROXY_URL`이며 빈 값/미설정이면 직접 연결이다. 값이 있으면 HTTPS origin의 정확한 host `www.shinhancard.com`, `www.lottecard.co.kr`, `image.lottecard.co.kr`에만 적용하도록 설계한다. `m.lottecard.co.kr`와 유사 suffix/기타 카드사/OCR·embedding/백업/MCP에는 적용하지 않는다. 모든 issuer client 생성 경로와 다운로드 redirect별 선택을 일관되게 처리하고 현대 TLS 예외를 보존한다. HTTPS 인증서·대상 host/DNS/redirect 검증은 약화시키지 않는다.

실제 Oracle 공인 주소, WireGuard peer 주소, loopback/bridge 프록시 URL은 서버 비공개 서비스·환경 설정에만 둔다. 이 문서에는 역할과 확인 범위만 기록한다. 저장소 설정 예시는 `CARDRAG_ISSUER_PROXY_URL=`처럼 값이 없는 형태만 허용한다. Worker에는 SSH 키나 VPN 설정을 넣지 않는다. 이 기능은 부하 최소화·cooldown·90일 분산 감사를 대체하지 않는다.

## 9. 기존 카드사 선택·CLI·OCR·publication 조사

| 영역 | 현재 확인한 구현 | 재사용할 부분 / 현재 한계 |
|---|---|---|
| 카드사 목록 | `issuers/registry.py:enabled_issuer_codes/enabled_adapters`가 `CARDRAG_ENABLED_ISSUERS`를 canonical 코드로 검증·정렬. 미설정 시8사, 중복/unknown/빈 목록 거부 | 기존 registry/검증 재사용. 실행 범위를 새 환경변수로 중복 정의하지 않음 |
| 일반 CLI | `cli.py:run_command`에 resume/skip/reuse/dry-run 옵션이 있고 `_run`이 `enabled_adapters()`를 pipeline에 전달 | 현재 일반 run에는 카드사 명시 CLI·순회 옵션 없음. 환경변수만 줄이는 것은 전체 serving 보존의 검증된 방법이 아님 |
| 스케줄러 | systemd가 매일03시 `worker run`; Persistent=true, 지연0 | 기존 timer/service 유지 가능. 현재는 일일 순회 cursor/day ledger 없음. Persistent가 누락된 날짜마다 별도 실행을 보장하는 것도 아님 |
| 동시 실행 | CLI/pipeline/partial 실행에서 기존 `worker_lock`을 사용, 비차단 flock으로 중복 거부 | 모드별 별도 state/lock 대신 동일 lock에서 순회 배정·재시작·게시 처리 필요 |
| 재처리 요청 | `ocr-cache reprocess`는 issuer/document/PDF/all 선택으로 불변 bounded 요청을 queue. `ocr_requests.py`는 다음 미완료 요청을 골라 run에 고정 | 요청 생성의 issuer 필터는 있음. 일반 실행의 선택 카드사와 queue 소비를 자동 교차 제한하지 않으며, 혼합 카드사 요청은 별도 분할/완료 설계 필요 |
| 부분 실행 | `partial_cli.py`, `ExecutionPlan`, `ReuseSource`, `--skip-pdf/--skip-ocr/--reuse-from-run` 등. skip은 원본 run·seal·산출물 계약을 요구 | 원본 취득 없이 OCR 등을 처리할 기반. stage skip과 카드사 범위는 다른 개념이고, frozen source의 전 문서를 replay하므로 그대로는 카드사별 처리 보장이 없음 |
| 장애 자료 승계 | `_carry_failed_issuers`가 실패로 등록된 카드사의 기존 자료를 `_AcquiredDocument`로 복원 | 비선택 카드사 전용 보존 경로는 아님. 승계된 자료도 acquired 처리/OCR·구조·export 흐름으로 들어갈 수 있음 |
| 게시 | pipeline contract와 manifest issuer_codes가 `self.adapters`에서 만들어지고, `acquired`를 OCR/후속 처리한 뒤 합본을 게시 | 선택 adapter 목록과 publication 전체 범위를 분리하지 않으면 기존8사 보존을 보장하지 못함. 단순 adapter 필터만으로 안전하다고 판정하지 않음 |

주요 코드 근거: `cli.py:1618,1825,265`, `pipeline.py:2084,2548,3233,4133,5586,6929`, `partial_execution.py:373`, `ocr_requests.py:141,181`, `state.py:181`. 현재 선택 단일사 실행을 운영에 실제 적용해 serving 보존을 시험하지 않았으며, 위 위험은 코드 검토에 따른 판정이다.

## 10. 선택·순회 설계 결정과 남은 검증

- **세 범위 분리:** configured pool은 기존 ENABLED_ISSUERS, selected work는 이번 수집·OCR·구조·view·embedding·재처리 대상, publication scope는 기존 정상 serving 전체와 선택 변경분의 합집합이다. 비선택 카드를 장애 카드사로 가짜 등록하지 않는다.
- **보존은 재처리가 아님:** 비선택 카드의 기존 serving 행·원본/OCR 참조·구조·view/vector를 검증된 산출물로 보존한다. 해당 문서를 OCR resolver·cache healing·구조 재분석·embedding 요청에 보내지 않는다. 합본 manifest/DB/vector 재배치 등 최소한의 공통 게시 패키징과 무결성 읽기는 허용하되 별도 집계한다. 기존 계약과 재사용 호환성이 불명확하면 전체 처리로 자동 확대하지 않고 게시를 중단한다.
- **선택 미실행과 실패 분리:** `not_scheduled`/보존 자료 상태를 설계한다. 실패 횟수·마지막 수집 성공·원본 감사 시각·단종 관측 횟수를 갱신하지 않는다. 비선택 문서가 초기 기준선에서 없으면 존재하는 것처럼 만들지 않으며, 전체 보존이 필요한 stable 게시 전 기준선 완전성을 검증한다.
- **기존 전체 모드 유지:** 새 옵션 미설정 시 기존 configured pool 전체를 실행한다. 수동 명시 선택과 선택적 일일 순회를 추가하되 CLI/환경설정 명칭은 PLAN의 설계안이며 아직 구현되지 않았다.
- **순회 기준:** Asia/Seoul 실제 실행일마다 같은 카드사 한 개를 고정 배정한다. 배정은 lock 안에서 영속화하고 cursor를 한 번만 전진시킨다. 당일 재호출은 같은 slot이며 terminal 결과는 재사용한다. 실패도 배정 이력으로 남겨 다음 날 다음 카드사가 실행되도록 한다. 성공하지 않은 것을 성공으로 기록하지 않는다.
- **독립 단계 필요:** 수집만 저장하고 나중에 OCR을 처리하거나, 기존 PDF를 원본 통신 없이 OCR 재처리하는 운용이 유용하다. 기존 partial/queue를 재사용하되 아직 없는 unsealed acquisition receipt의 범위·hash·출처 계약이 필요하다. 선택 실행 기본은 수집부터 게시까지이며 자동 순회도 우선 그 전체 단계를 한 slot으로 고정한다.
- **90일 감사:** 비선택일에는 해당 issuer 원본 감사도 실행하지 않는다. due를 방문일마다 미루지 않고 마지막 실제 원본 확인 기준으로 유지한다. 8사 순회라면 각사의 방문일 예산을 일별 균등분할보다 약8배 확보해야 하며 예상 다음 방문까지의 여유를 둔다. 기한 초과는 overdue로 표시하고 비선택 자료를 자동 삭제하거나 오늘 검증된 것으로 표시하지 않는다.

프록시 목록/PDF 실측, 비선택 행·vector의 안전한 합본 재사용, 혼합 OCR 요청의 분할, 날짜 경계의 게시 복구와 순회 배정 결합은 미검증이다. 구체적인 상태 전이·실행 조합·인수 매트릭스는 `PLAN.md`를 따른다. 이번 개정은 이 설계를 구현하거나 프록시를 Worker에 적용한 작업이 아니다.
