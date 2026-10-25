# 017 PLAN — 증분 수집·90일 감사·host별 프록시·카드사 선택 및 일일 순회

작성: 2026-10-19, Planner Codex. 같은 날 08:20 KST 이후 현재 저장소·서비스를 재검토해 사용자 지시에 따라 직접 개정했다. 근거는 `ANALYSIS.md`와 최초 조사 `/opt/cardrag/017-evidence/`다. Git 기준 HEAD `aaeb2c4`, 구현 `d8a9018`, 운영 Worker `016-d8a9018`.

이번 작업은 **플래너의 조사·계획 문서 수정만**이다. 제품 구현·배포·017 개발·새 운영 실행·SSH/VPN/라우팅/서비스 변경을 하지 않는다. 아래 신규 옵션·상태 모델은 설계 결정이며 구현 완료된 기능이 아니다. 수정 대상은 본 문서와 `ANALYSIS.md` 두 파일뿐이다.

## 1. 목표와 보존 조건

- 일배치를 경량 목록 확인과 신규·변경분 취득 중심으로 전환한다. 알려진 동일 원본은 필요한 분산 감사만 수행한다.
- 전체 원본 재확인 범위는 약90일에 한 번 충족하되 한 날에 몰지 않는다. 새 파일의 MIME/magic/hash/페이지 검증은 유지한다.
- 조사에서 확인한 차단 원인은 미확정이다. 별도 구성된 프록시 인프라를 재사용하는 host별 선택 기능을 계획하되, 출구 변경으로 반복 부하를 해결했다고 보지 않는다. 현재 플래너 작업에서 프록시나 운영 설정을 변경하지 않는다.
- 기존 PDF/OCR/embedding cache, source/lineage/날짜/단종 이력, current/previous seal과 serving 소비 계약을 보존한다. 캐시를 비우거나 기존 source_id를 일괄 폐기해 cold download를 유발하지 않는다.
- 기존 handoff와 사용자 변경을 보존한다. Executor는 작업 시작 시 실제 Git·운영 이미지·writer 상태를 다시 확인한다.
- 기존 전체 배치를 유지하며 명시 카드사 실행과 선택적 하루 한 카드사 순회를 지원하는 구조를 설계한다. 순회에 따른 최신성 저하는 허용하지만 비선택 카드사의 자료·serving 결과를 보존한다.
- 수집 범위뿐 아니라 OCR·구조·view·embedding·재처리 범위도 제한한다. 전체 serving을 보존한다는 이유로 비선택 카드사의 처리를 다시 실행하지 않는다.
- 실제 주소·프록시 URL은 서버 비공개 설정에만 둔다. 저장소에는 범용 기능과 값 없는 설정 예시만 허용하며 SSH 키·VPN 설정을 커밋하거나 Worker에 넣지 않는다.

## 2. 현재 사실

10/19 새벽 배치 기준 매일 전체 metadata 조회, 168h PDF 원본 재확인, 로컬 hash/페이지 검증이다. 캐시 기간 내 hit에는 원본 HTTP와 prepare가 없다. 10/18 PDF download390/revalidation2504/3042114, 10/19 정상 download0이다. DRM8은 매번 body를 받고도 정상 download 집계에서 제외된다. 수치의 기준 시점과 전송량 한계는 ANALYSIS에 유지한다.

우리·현대는 상품별 상세를 전수 조회한다. 신한은 PDF마다 모바일 목록 조회가 추가된다. 정상 수집6사의 현재 정상 PDF body 합계는 약2.91GB, 전체8사 serving은 과거 개정 포함5.01GB다. 전체 HTTP 요청/bytes 실측은 아직 없으며 snapshot 수나 저장 길이를 통신량으로 대체하지 않는다.

### 확인된 현재 상태

- `cardrag-oracle-tunnel.service` active/running, SSH local forward가 WireGuard 내부 peer를 사용하며 자동 재시작한다. host loopback용 및 Worker bridge용 listener도 확인했다. 실제 주소는 비공개 서비스·환경 설정을 따른다.
- 사용자 제공 검증: Oracle 공인 출구 경유 HTTP 프록시이며, 운영 Worker 이미지·Docker 네트워크에서 출구 IP 및 신한·롯데 페이지 HTTP 200 확인. 이번 재검토가 해당 원격 시험을 새로 수행한 것은 아니다.
- 미검증: 실제 공시 목록 API·세션·페이지 순회·PDF/304/redirect·파일 검증. 운영 Worker에는 proxy 환경설정이 없고 `issuer_http.py`에도 관련 기능이 없다.
- `CARDRAG_ENABLED_ISSUERS`의 registry 필터는 존재하나 일반 `run`의 카드사 명시 CLI와 순회 상태는 없다. timer는 매일03시 일반 run을 실행한다. 현재 configured pool은 전체8사다.
- pipeline은 adapter 목록을 계약과 manifest issuer_codes에도 사용한다. 장애 승계는 실패 카드사 전용이며 acquired/OCR 흐름에 다시 들어간다. 따라서 adapter 필터만으로 전체 serving 보존·비선택 처리0을 보장한다고 볼 수 없다.
- 기존 stage skip/reuse/immutable OCR 재처리 queue 및 worker_lock을 재사용한다. 이들 기능이 카드사 선택과 독립 단계의 모든 요구를 이미 충족한다고 해석하지 않는다.

## 3. 구현 단계

### A. 계측·예산·오류 억제

관련 파일: `rate_limit.py`, `issuer_http.py`, `issuer_collection.py`, `state.py`, `performance.py`, `pipeline.py`, `settings.py`, Compose.

1. 일반 GET/POST와 streaming 모두 issuer/host/run/목적별 요청 수, HTTP status, attempt, conditional 결과, 수신 body bytes를 계측한다. DRM·검증실패·redirect·취소 중 이미 받은 bytes도 포함한다. 연결 전 실패와 body 수신 후 실패를 구분한다. cookie/token/raw form/응답 원문은 로그에 넣지 않는다.
2. issuer/host별 request budget, byte budget, 동시성/시작 간격, next_allowed_at을 영속 관리한다. 구체적 상한은 실측 목록량과 신규 유입으로 정한다. 예산 소진은 pending/deferred로 보존하고 목록을 잘라 성공 처리하지 않는다.
3. 403/429/reset/timeout별 동작, Retry-After, 제한된 probe와 run 간 cooldown을 추가한다. 현재의 단계4회 재시도가 전체 목록을 반복하지 않도록 재시도 단위도 검토한다.
4. issuer 시작 시간을 분산한다. 정책·기본값 변경과 관측 결과를 보고서에 명시한다.

### B. negative cache와 90일 분산 원본 감사

관련 파일: `pdf_cache.py`, `state.py`, `downloader.py`, `pipeline.py`, contracts/settings.

1. 목록 관측, 로컬 검증, 실제 원본 확인, 다음 원본 감사 시각을 구분한다. 목록/로컬 검사만으로 원본 확인 시각을 갱신하지 않는다.
2. 기존 source의 내용 metadata가 같고 local cache가 정상이면 원본 GET/HEAD/prepare를 생략한다. 신규·내용 변경 의심·로컬 유실/손상은 즉시 취득하고 기존 strict validation을 유지한다.
3. 안정된 identity와 실제 크기/마감일/issuer 예산으로 due를 분산한다. 기존 캐시가 배포 직후 전량 만료되지 않게 이행한다. 단순 TTL=2160h만으로 동시 만기를 만들지 않는다.
4. 검증된 endpoint는 conditional GET/304를 원본 확인으로 인정한다. validator 없는 대상은 분산 body 감사로 동일 URL의 무표시 교체를 발견한다. 저장 ETag 보유만으로 지원을 입증했다고 보지 않으며 HEAD/Range를 무조건 사용하지 않는다.
5. DRM과 정확히 식별한404는 source/content fingerprint에 연결된 negative cache와 재확인 due를 보존한다. metadata 변경 시 해제한다. 미등록 DRM은 가시 경고와 제한된 확인을 유지하고 영구 skip하지 않는다.
6. 예산/실패로90일을 초과한 대상을 overdue로 노출한다. body 감사와 HTTP validator 확인을 구분 집계하고 수행하지 않은 감사를 완료로 표시하지 않는다.

### C. metadata inventory와 카드사별 차분 수집

관련 파일: `contracts.py`, `state.py`, `issuers/*.py`, `pipeline.py:collect_discovery/select_current`.

- delta와 complete inventory를 분리한다. new/changed/unchanged/removed-candidate 및 검사 범위를 구조화하고, 기존 최소 건수/retention 검사는 저장 inventory와 합성한 complete view에 적용한다.
- **우리·현대:** 상위 상품 identity·표시 metadata를 보존하고 신규/변경 후보만 즉시 상세 조회한다. 기존 상세는 순환 스케줄로 확인한다. 상위 목록에 버전 정보가 없다면 기존 개정 발견도 이 주기까지 늦어진다는 조건을 명시한다. 일별 개정 탐지가 필수이면 공식 변경 지표가 실증되기 전까지 상세 생략을 허용하지 않는다. 우리 main의1000건 고정 조회는 total/pagination 완전성도 검사한다.
- **신한:** 필요한 시점에 category별 mobile token map을 만들어 배치 내 동일 stable identity 대상에 재사용할 수 있는지 session 범위·유효기간을 검증한다. cookie를 근거 없이 전역 공유하거나 token을 영구 캐시하지 않는다. 직접 PDF 경로 변경도 source identity 검증을 유지한다.
- **삼성:** sysLtChTs와 첨부 identity로 변경을 추출한다. 현재 목록은 최초등록순이므로 처음 몇 페이지만 보고 과거 게시물 개정이 없다고 결론내리지 않는다. 경량 전체 metadata walk를 유지할 수 있다.
- **KB·하나·롯데:** 전체 metadata 비교를 우선 사용한다. 정렬/cursor 보장 없이 첫 기존 행에서 조기 종료하지 않는다. KB5분류, 하나 과거행/latest 선택, 롯데 DOCID/총건수·기존404 정책을 보존·이행한다.
- **BC:** 단일 HTML 조회를 유지할 수 있다. 동일 path 교체는 분산 원본 감사로 보완한다.
- 불완전 조회나 감사 미실행을 단종으로 처리하지 않는다. fresh/stale carry 및 기존 retirement 보호를 유지한다. 표시 metadata만 바뀌었을 때는 안전한 원본 재사용 근거와 게시 metadata 갱신을 분리한다.

### D. 별도 로컬 처리 최적화 조사

같은 corpus인데 contract가 매일 달라지고 약87분 재생성하는 현상을 조사한다. raw OpenRouter metadata의 변동이 유일한 원인인지 확인하고 의미 계약과 관측 증거를 분리할 수 있는지 검토한다. 검증 없이 contract에서 필드를 제거하지 않는다.

로컬 동일 CAS의 in-run 중복 hash/페이지 검증을 줄일 수 있는지 조사한다. 파일 변경·regular file·no-follow 보호와 기존 consumer 계약을 유지한다. 이 작업의 절감량을 원격 다운로드 감소와 섞지 않는다.

### E. 기존 HTTP 프록시의 카드사별 선택 연결

관련 파일: `issuer_http.py:create_issuer_client`, `settings.py`, `cli.py`, `partial_cli.py`, `pipeline.py`의 discovery/PDF client 생성부, `downloader.py`, Compose와 `.env.example`, 운영 문서. 파일 변경은 향후 Executor의 범위이며 이번에는 수행하지 않는다.

- `CARDRAG_ISSUER_PROXY_URL`을 명시적으로 받는다. 빈 값/미설정은 직접 연결, 값이 있으면 **HTTPS/443의 정확한 host** `www.shinhancard.com`, `www.lottecard.co.kr`, `image.lottecard.co.kr`만 HTTP 프록시 transport를 사용한다. wildcard/suffix 또는 전체 HTTPS 우회로 구현하지 않는다. URL 형태가 잘못되면 안전한 설정 오류로 거부한다.
- discovery GET/POST와 PDF prepare/stream/conditional GET에서 같은 정책을 사용한다. redirect가 발생하면 기존 허용 host/공개 origin/TLS/redirect 제한을 먼저 유지하면서 실제 각 hop의 host로 경로를 결정한다. 현대 전용 TLS mount는 그대로 보존한다. port 정규화로 다른 port까지 mount가 잡히지 않도록 실제 request origin도 확인한다.
- `m.lottecard.co.kr`와 그 밖의 host, 다른 카드사, OCR·OpenRouter·WebDAV/백업·MCP는 이 설정으로 우회하지 않는다. 전역 `HTTP_PROXY/HTTPS_PROXY`를 추가해 해결하지 않는다. ambient 환경변수 때문에 비대상 host가 우회되지 않는지도 시험한다.
- 설정된 프록시 단절 시 무조건 직접 연결로 재시도하지 않는다. 해당 issuer의 원격 오류·cooldown·carry로 처리하고 다른 카드사를 불필요하게 막지 않는다. 대상 issuer가 선택되지 않은 실행에서는 불필요한 원격 proxy health probe를 하지 않는다.
- 호스트용과 컨테이너용 endpoint는 서로 다르다. 실제 Worker 비공개 환경에는 컨테이너에서 도달 가능한 endpoint를 사용한다. SSH·VPN은 호스트 인프라이며 Worker가 터널을 생성하거나 SSH 키를 읽는 기능을 추가하지 않는다.
- 저장소 예시는 아래처럼 값이 없는 항목만 추가할 계획이다. 실제 Oracle 공인 주소/WireGuard peer/host·bridge URL·key·VPN 설정은 저장소에 넣지 않고 서버 비공개 설정을 참조한다. 로그에는 route 종류·적용 여부만 남기고 proxy URL/SSH 인자/자격증명은 출력하지 않는다.

```dotenv
# 향후 범용 설정 예시: 미설정/빈 값은 직접 연결
CARDRAG_ISSUER_PROXY_URL=
```

기존 페이지200 검증을 목록·PDF 검증 완료로 확대하지 않는다. 향후 실제 검증은 두 회사의 정상 목록 API 응답/schema/session/pagination과 정상 PDF magic/hash/페이지/304 경로를 소수 요청 예산 내에서 확인해야 한다.

### F. 카드사 선택과 전체 serving 보존을 별도 범위로 설계

관련 파일: `issuers/registry.py`, `cli.py`, `partial_cli.py`, `partial_execution.py`, `pipeline.py`, `ocr_requests.py`, `state.py`, `corpus_diff.py`, `retirement.py` 및 export/manifest/local publisher의 실제 구현 파일을 함께 검토한다.

1. **configured pool:** 기존 `CARDRAG_ENABLED_ISSUERS`와 canonical registry 검증을 재사용한다. 새 옵션 때문에 전역 환경변수를 일시 변경하거나 canonical 목록을 중복 정의하지 않는다. 미설정 기본8사는 유지한다.
2. **selected work scope:** 이번 실행의 adapter 목록·PDF·OCR·구조·view·embedding·재처리 대상이다. 명시 선택은 configured pool의 부분집합으로 제한한다. unknown/빈 선택/충돌 옵션은 HTTP·run 배정 전에 거부한다.
3. **publication scope:** 기존 정상 serving의 모든 카드사와 이번 선택 변경분의 합집합이다. configured pool에서 빠지거나 오늘 선택되지 않았다고 기존 공개 데이터를 제거하지 않는다. issuer 제거는 별도 명시 작업으로 두며 017 자동 순회에 넣지 않는다.
4. 실행 선택은 semantic 처리 계약과 분리한 영속 run receipt로 기록한다. 기존처럼 선택된 adapters만으로 전체 manifest/계약/retention/기준선 범위를 축소하지 않는다. schema·parser profile·embedding profile/dtype/차원·normalization 등 재사용 호환성을 검증한다. 실행 선택이 바뀌었다는 이유만으로 비선택 자료를 새 계약으로 재생성하지 않는다.
5. 비선택 자료는 **실패 승계가 아닌 not_scheduled 보존 경로**로 prior serving의 행·OCR 실패/DRM·개정 이력·구조·view/vector와 참조를 유지한다. PDF를 acquired 큐에 넣어 전체 OCR 루프를 다시 태우는 방식은 금지한다. 해당 자료의 issuer HTTP, OCR resolver/provider/cache-healing, 구조·view 재분석, embedding 요청은 0건이어야 한다.
6. 합본 generation을 위한 기존 artifact 읽기·무결성 확인·검증된 행/vector의 복사·offset 재배치와 manifest/DB 게시 패키징은 공통 작업으로 허용한다. 이것을 비선택 카드사의 신규 처리·OCR 재사용 횟수로 세지 않는다. 재사용 호환성이나 참조가 불명확하면 전체 재처리로 확대하지 말고 stable 교체를 중단한다.
7. 선택 카드사에서 확인된 신규/변경/적법한 단종만 병합한다. 비선택 카드사는 실패 수, retirement 횟수/후보, last_successful_collection_at, 원본 확인 시각을 갱신하지 않는다. 범위가 완전한 선택 수집에 대해서만 기존 단종 조건을 평가한다.
8. 선택 카드사 실패는 실제 failure/degraded로 남기고 그 카드사의 이전 결과를 유지한다. 선택 단일사가 전부 실패하면 새 결과가 없는 한 stable pointer를 전환하지 않는다. 비선택을 실패로 가짜 등록해 carry를 호출하지 않는다.
9. 전체 기준선이 없는 초기 설치/손상 상태에서는 운영의 전체 serving 보존을 보장할 수 없다. 선택 stable 게시 전에 검증된 전체 기준선/seed/adopt가 필요하며 없으면 중단한다. 존재하지 않는 다른 카드사 자료를 생성해 보존 완료라고 쓰지 않는다. 수집만 저장하는 단계는 게시와 분리할 수 있다.
10. 기존 local publisher의 원자적 전환·seal/manifest/DB/vector hash·현재/이전 rollback을 유지한다. pending run/수집 receipt/reprocess child가 참조하는 source/artifact는 native retention/GC의 보호 대상으로 포함한다. 새 선택 실행의 증분 백업은 선택 변경 artifact와 합본 게시 필수 공통 artifact를 대상으로 하며, 관련 없는 비선택 OCR 복구/재처리/업로드 큐를 자동 소진하지 않는다.

### G. 전체·명시 선택·하루 한 카드사 순회의 실행 계약

아래 명칭은 **신규 CLI 설계안**이지 현재 사용 가능한 옵션이 아니다.

| 실행 | 계획한 표현 | 동작 |
|---|---|---|
| 기존 전체 | 새 설정 없이 `run` | 기존 configured pool 전체 처리, 기본8사 유지 |
| 명시 선택 | `run --issuer shinhan` 또는 반복 `--issuer` | 지정 카드사만 처리하고 전체 serving 보존 |
| 일일 순회 | `run --round-robin` | 그날 배정한 카드사 한 개만 전체 단계 처리 |
| 명시 전체 | `run --all` | 순회 환경에서도 수동 전체 실행 |

- scheduler opt-in 설정명은 `CARDRAG_ISSUER_EXECUTION_MODE`를 제안한다. 미설정/빈 값은 all, 값 all/round_robin만 허용한다. 저장소에는 값 없는 설정 예시를 둔다. 현재 timer/service의 기존 `run` 호출은 유지하고 향후 private env를 통해 선택적으로 순회를 켤 수 있게 한다. 이번 작업에서는 켜지 않는다.
- CLI의 명시 선택/명시 전체/명시 순회가 환경 기본 모드보다 우선하며 세 종류를 동시에 지정하면 거부한다. resume은 저장된 모드/issuer/source/단계 범위가 우선이고 달라진 scope로 재계산하지 않는다. 모순되는 새 인자는 첫 origin/provider 요청 전에 거부한다.
- 수동 명시 실행·수동 전체·독립 수집/처리 실행은 자동 순회 cursor/day slot을 소비하거나 변경하지 않는다. 해당 실행의 실제 수집/감사 시각은 정상 갱신할 수 있다. 성공한 수동 실행이 있었다고 예약 순회에서 다른 issuer로 당일 배정을 바꾸지 않는다.

#### 순회 상태와 같은 날 재호출

- 기존 Worker SQLite/worker_lock과 같은 상태 영역에 cycle roster/cursor 및 day slot을 영속화한다. 저장 모델 명칭은 가안이며 기존에 존재하는 테이블로 표현하지 않는다. day slot은 schedule ID·publication channel·**Asia/Seoul 실제 실행일**로 유일하고 config revision을 바꿔도 같은 날 두 issuer가 배정되지 않게 한다.
- cycle에는 고정 canonical roster와 revision, 배정 이력/다음 cursor를 둔다. 순서는 registry의 정렬을 재사용할 수 있다. 모든 활성 카드사를 한 cycle에 한 번씩 빠짐없이 배정한다. 성공 보장이 아니라 배정·실패 이력의 완전성이며, 원격 장애를 성공으로 간주하지 않는다.
- lock 획득 및 옵션 검증 후 day slot을 만들고 cursor를 **배정과 같은 트랜잭션에서 한 번만** 전진시킨다. 다른 모드가 lock을 점유하면 busy로 종료하고 cursor를 바꾸지 않는다. startup 전 claim ID를 남기고 실제 run 생성 후 run_id를 결합해, run 생성 전 startup 실패도 해당 날짜·issuer 결과로 기록한다.
- 당일 순회 재호출은 같은 slot을 조회한다. succeeded/no_change/terminal failure이면 기록된 결과를 반환하며 다음 issuer나 새 자동 run을 실행하지 않는다. 일시 중단 run이 있으면 같은 issuer·run·source·누적 시도 예산으로 resume한다. dry-run은 claim/cursor/원격 부작용이 없어야 한다.
- terminal 실패해도 다음 날 다음 issuer가 배정되므로 장애 카드사 한 곳이 다른 카드를 굶기지 않는다. 해당 카드는 다음 cycle 또는 명시 수동 실행에서 다시 시도하며, 같은 날 자동 반복은 하지 않는다. run 안의 유한 retry와 수동 재시도는 따로 집계한다.
- 프로세스가 죽으면 기존 lock 해제 후 slot/run/seal/current pointer를 대조한다. 게시 완료 후 slot 완료 기록 전에 죽은 경우 검증된 게시 증거로 완료를 복구하며 OCR/다운로드를 반복하지 않는다. 수동 실행이 이미 최신 pointer를 바꿨다면 이전 결과로 덮어쓰지 않고 충돌을 보고한다.
- **날짜가 바뀐 미완료 run:** 우선 기존 publication의 취소/완료를 로컬 증거로 안전하게 정리하고 interrupted/failed 이력을 보존한다. 그 옛 slot의 origin/OCR 작업을 오늘 순회에서 자동 재실행해 다른 issuer와 섞지 않는다. 오늘은 새 날짜의 다음 issuer 하나를 배정한다. 안전한 게시 정리가 불가능하면 오늘 실행도 중단하고 이전 serving을 유지한다. 옛 작업의 실처리 재개는 명시 수동 resume으로 두고 자동 cursor는 건드리지 않는다.
- `Persistent=true`로 놓친 실행이 나중에 호출되면 그 **실제 날짜**의 한 slot만 처리한다. 누락 날짜 수만큼 cursor를 건너뛰거나 하루에 여러 카드사를 몰아서 처리하지 않는다. missed 날짜는 실행 없음으로 남기며 단종/장애 횟수를 늘리지 않는다.
- roster는 cycle 동안 고정한다. 추가 issuer는 다음 cycle에 넣는다. 비활성화된 issuer는 다음 배정 전에 명시 migration에서 제외하고 제외 사유를 기록하며 serving은 보존한다. 현재 진행 중 scope를 임의로 바꾸지 않는다. 같은 날 configuration 변경으로 추가 자동 slot을 만들지 않는다. 새 roster 이행 시 공통 issuer의 미배정 항목을 먼저 이어가고 cursor를 무조건 처음으로 초기화하지 않는다.

### H. 수집과 OCR의 독립 실행 및 기존 재처리와의 관계

- 기본 선택 실행과 자동 순회는 수집→선택 문서 OCR/후속 처리→전체 결과 병합·게시까지 묶는다. 초기 자동 순회에는 phase=all만 허용하며 collect/process 단독 단계와의 조합은 옵션 검증에서 거부한다. G의 배정 cursor 전진과 실제 수집·게시 성공 기록은 별도다.
- 독립 단계는 수동 실행에 유용하므로 `--phase all|collect|process`를 가안으로 설계한다. all이 기본이다. collect는 선택 범위의 목록/PDF/분산 감사만 하고 durable acquisition receipt를 남기며 OCR/구조/embedding/export/serving 교체를 하지 않는다. provider는 lazy 초기화하여 사용하지 않는 OCR/embedding 요청도 하지 않는다.
- process는 선택 범위와 검증된 source receipt/run을 명시해 원본 수집 없이 OCR/후속 처리를 수행하고 전체 serving에 병합한다. 기존 `--skip-pdf`/ReuseSource의 검증·lazy provider·resume 경로를 우선 재사용한다. 현재 ReuseSource는 frozen generation/seal을 전제로 하므로 아직 게시하지 않은 collect receipt를 그대로 지원한다고 주장하지 않는다. schema/version/source identity/PDF hash·크기·경로/범위/관측시각을 검증하는 입력 계약이 추가로 필요하다.
- source 전체를 replay하지 않고 선택 issuer 및 필요한 current/history 문서만 처리한다. 전역 `--skip-ocr`는 이전 산출물 재사용 조건을 요구하므로 신규 PDF 수집만 하는 기능과 동일하지 않다. 기존 skip 조합과 phase 충돌은 명확히 거부한다. source에 없는 issuer로 처리 범위를 확대하지 않는다.
- `ocr-cache reprocess --issuer`의 bounded 불변 요청/preview/queue/proof를 재사용한다. 정상 OCR cache hit는 그대로 쓰고 명시 재처리 대상만 새 OCR variant를 만든다. 실행 scope 밖 요청은 pending으로 보존하며 자동 선택·완료하지 않는다.
- 혼합 issuer의 기존 불변 요청은 원본을 수정하지 않고 결정적인 issuer별 child 요청과 parent/child 연결로 이행하는 방향을 검증한다. parent 완료는 모든 child의 proof와 게시 결과를 검증한 뒤에만 가능하다. 미선택 요청이 큐 앞에 있다고 선택 요청을 영원히 막거나, 혼합 parent가 있다는 이유로 비선택 OCR을 실행해서는 안 된다. resume은 고정된 child/target만 재개한다.
- collect 성공/원본 감사 시각과 OCR 성공·serving 반영 시각을 구분한다. collect만 했다고 최신 serving으로 표시하지 않는다. process-only는 수집 때의 원본 관측시각을 유지하고 오늘 원본을 확인했다고 갱신하지 않는다.

### I. 선택·순회와 90일 감사·freshness의 결합

- 원본 감사 due는 마지막 **실제 원본 확인** 기준으로 영속 관리한다. 비선택 issuer는 오늘 원본 요청/감사/프로브를 실행하지 않으며 due·최근 성공 시각을 당일로 갱신하지 않는다. 예외적인 운영 proxy 관리 작업은 issuer 원본 작업과 분리한다.
- 순회 방문이 약8일 간격이면 선택일에 그 사이 쌓인 issuer별 감사량을 처리해야 한다. 초기 일일 균등안의 총 약60건을 선택한 한 카드사에 그대로 적용하지 말고 issuer별 대상 건수·byte·남은 방문 횟수로 예산을 재산정한다. 다음 방문 전 90일을 넘길 대상은 여유 기간을 두고 먼저 배정한다.
- 신규·변경 후보는 그 issuer를 선택한 날 즉시 우선 취득한다. 신규 및 목록 최신성은 순회 주기만큼 늦어질 수 있으며 사용자가 이를 허용했다. 실패·예산 부족으로 원본 감사가 90일을 넘으면 overdue를 보고한다. 기한을 조용히 연장하거나 비선택 issuer를 추가 처리해 하루 한 카드사 조건을 깨지 않는다.
- freshness는 last_scheduled/last_attempted, 마지막 성공 목록 관측, 마지막 원본 확인, 다음 감사/overdue, 마지막 OCR/게시를 구분한다. `not_scheduled`는 정상적인 보존 상태이며 HTTP 실패/degraded와 다르다. 단종 grace_runs는 전체 run 횟수가 아니라 해당 issuer의 완전한 성공 관측을 기준으로 하고 시간 경과만으로 비선택 자료를 제거하지 않는다.
- 전체 모드는 같은 due/negative cache/queue를 사용하여 선택 전체에 대해 감사할 수 있다. 수동 전체의 실제 확인은 due를 갱신하지만 자동 순회 배정은 소비하지 않는다. 두 모드가 서로 다른 원본 확인 시계를 만들지 않는다.

## 4. 인수 기준

1. 정상 캐시·metadata 동일·감사 due 아님이면 PDF GET/HEAD/prepare 호출0. 신규1건/내용 변경1건이면 해당 대상의 필수 전처리+다운로드만 수행한다.
2. 상품명/발행상태만 바뀐 사례에서 원본 재사용의 안전한 근거를 검증한다. 전송 token 회전만으로 전량 miss가 생기지 않게 한다. 재사용 근거가 불명확하면 변경 후보로 남긴다.
3. 같은 파일명/URL의 바이트 교체는90일 감사에서 새 revision으로 등록된다. local 손상은 원본 감사 주기와 별개로 복구하며 새 파일 검증은 약화시키지 않는다.
4. 304 body0/200/DRM/404/403/429/timeout/reset/redirect/retry/cancel의 수치가 정확하다. 알려진 DRM8은 due 또는 metadata 변경이 없으면 다음 일배치에서 재다운로드하지 않는다.
5. lock, restart/resume, 예산 소진, pending queue, overdue, freshness, 전체 inventory 재구성, 단종 동결을 검증한다. 카드사 장애를 publication/storage 실패와 혼동하지 않는다.
6. 기존 cache 이행 시 전량 다운로드 폭주가 없어야 한다. source 이력·OCR 재사용·이전 seal 소비·rollback을 유지한다.
7. 어댑터별 fixture부터 검증한다. 실제 origin 검증은 소수의 명확한 요청/bytes 상한 내에서 수행하고 부하 재현을 위한 전량 호출을 하지 않는다.
8. 기존 관련 테스트와 위 회귀, ruff/diff 검사를 수행한다. 공통 계약 변경 범위에 맞게 전체 suite도 검증한다.

### 추가 인수·검증 매트릭스

| 구분 | 필수 검증과 합격 조건 |
|---|---|
| 프록시 미설정/선택 | 미설정·빈 값은 직접 연결. 설정 시 지정3host의 GET/POST/stream/304/redirect hop만 proxy 사용. 유사 suffix·다른 port·모바일 host·다른 카드사·OCR/embedding/backup은 우회하지 않음. 현대 TLS·인증서·origin/DNS 제한 유지 |
| 프록시 장애/운영 검증 | 단절 시 직접 연결로 몰래 fallback하지 않고 대상 issuer 오류·예산·carry가 정상. 해당 issuer 미선택이면 원본 probe0. 운영 이미지·네트워크에서 소수 목록 API/정상 PDF를 검증하고 기존 페이지200 근거와 구분. actual URL/key/VPN/인자가 저장소·로그에 노출되지 않음 |
| 전체 배치 호환 | 새 설정/옵션 없이 configured pool 전체 실행. 기존8사 수집·OCR·게시·백업 계약과 기본 timer 동작 보존 |
| 선택 단일/복수 | 8사 baseline을 준비하고 한 카드/복수 카드만 실행. 비선택 issuer HTTP·PDF prepare·OCR resolver/provider/healing·구조/view 생성·embedding·재처리 호출이 모두0. spy로 cache hit도 실제 처리 호출인지 구별 |
| 전체 serving 보존 | 선택 성공/no_change/부분 실패/전부 실패 모두 시험. 비선택의 상품·현재/과거 개정·OCR 실패·DRM·source/PDF/OCR 참조·구조·view/vector와 검색 결과를 이전 세대와 대조. 합본 issuer_codes/건수/ID/vector 연결·seal/manifest/current 및 rollback 정상. 불명확한 재사용은 게시 실패로 처리 |
| freshness/단종 | 비선택은 not_scheduled이며 실패 횟수·성공 관측·원본 감사일·단종 grace 관측을 갱신하지 않음. 선택 complete 목록에 대해서만 단종 평가. missing baseline·incomplete delta·실패를 자료 삭제로 바꾸지 않음 |
| 순회 완전성 | 가상 KST 날짜를 이동하여 N개 enabled issuer를 N개 배정일에 각1회 방문. 같은 날 재호출은 같은 slot/결과, 날짜만큼 cursor 점프하지 않음. 중간 실패가 다음 issuer를 굶기지 않고 성공이라고 집계되지도 않음 |
| crash/resume | slot 생성 전후/출처 고정/수집 중/게시 전후/slot 완료 전후 crash 주입. 같은 날 동일 run 재개, 누적 예산 유지, 게시만 완료된 경우 중복 provider 호출0. 날짜 넘김·수동 pointer 변경은 충돌/정리 정책대로 처리하고 최신 serving을 덮어쓰지 않음 |
| 수동·동시 실행·설정 변경 | 전체/선택/순회/부분 실행이 기존 같은 lock을 사용. busy와 dry-run은 cursor 변경0. 수동 실행은 자동 slot 불변. roster 추가/제거·timezone 경계·Persistent catch-up·config 변경에도 당일 다른 issuer 추가 배정0, 남은 활성 issuer 누락0 |
| 수집/처리 분리 | collect는 OCR/embedding·게시0이며 receipt·선택 범위·관측시각 보존. process는 원본 HTTP0, source 밖 issuer 처리0, prior 전체 serving 보존. 기존 skip/reuse 및 손상·만료·잘못된 receipt를 검증 |
| OCR 요청 큐 | 비선택 및 혼합 parent/child 요청을 포함. 선택 child만 수행, 비선택 pending 유지, 중복/조기 parent 완료0, head-of-line 무한 대기0. 일반 실행과 강제 재처리·resume의 scope가 일치 |
| 90일 감사/retention | 8사 순회·수동 전체·장애·큰 파일·예산 부족을 날짜 시뮬레이션. 원본 확인 없이 due 갱신0, 방문일 감사 예산 재산정, overdue 가시화. 비선택 감사 호출0. 모든 합본 참조와 미완료 receipt/source pin이 prune/GC 후에도 유지 |

위 항목은 향후 개발·배포의 인수 기준이며 이번 플래너가 통과 시험을 수행한 것으로 기록하지 않는다. 현재 코드 조사만으로 선택 실행의 전체 serving 보존이나 신규 proxy 경로를 인수하지 않는다.

## 5. 제출·운영 평가

Executor는 본 PLAN/ANALYSIS와 당시 Git·운영 상태를 확인한 뒤 실제 구현 범위를 정한다. 보고서는 새 `REPORT.md`에 구현·검증·미수행·탐지 지연 등 정책 한계를 구분해 작성한다.

개발 순서는 먼저 범위·합본 보존 계약 및 계측/상태 모델을 확정하고, proxy 선택·수집 차분/감사·선택 처리·순회/단계 분리를 기존 경로와 함께 검증하는 것으로 잡는다. 범위 보존 없이 환경변수만 줄여 운영 단일사 실행을 시작하지 않는다. 프록시 활성화만으로 목록·PDF 복구 완료라고 표시하지 않는다.

향후 운영 반영 시 전후 issuer 요청 수/body bytes, 선택/비선택 처리 수, 전체 serving 대조, 신규 revision 탐지, deferred/overdue/rotation receipt, 처리시간과 서비스 정상 근거, rollback을 준비한다. 기존 상태를 삭제하지 않는다. 그때의 구현·시험·미수행은 새 REPORT에 분리한다. **이번 작업은 두 계획 문서 개정만이며 제품 구현·배포·017 개발을 시작하지 않았다.**
