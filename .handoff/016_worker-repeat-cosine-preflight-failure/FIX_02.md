# 016 FIX_02 — 재리뷰 및 추가 과제: 롯데 수집 경로·전체 호스트 용량·품질 gate 정책

- 작성: 2026-10-16 08:58 KST, Reviewer Codex.
- 검토 대상: PLAN/REPORT/FIX_01/FIX_01_REPORT, 구현 `bb56d20b2d195825737fa50d8fce893a3ad3c494`, HEAD `1764aef`, 현재 코드·운영 증거.
- 사용자 추가 요청에 따라 **호스트 전체 안전 정리와 실제 롯데 호출 테스트는 Reviewer가 수행**했다. 제품 코드·수집 경로·품질 정책은 이번 턴에서 변경하지 않았다.
- 판정: 이전 FIX_01의 진단 필드 누출은 해결, publication seal/산출물 closure 복원은 확인. 아래 신규/확대 과제와 잔여 복원 범위 정정을 **다음 Executor 작업으로 요청**한다. 기존 handoff는 덮어쓰지 않는다.

## 1. 기존 수정의 재리뷰

- reason별 QUALITY_DETAIL_SCHEMAS 및 생성/CLI/history 출력 시 재검증으로 generic token형 문자열과 사후 변조 값의 누출 경로가 제거됐다. 기존 threshold/비재시도/deadline 보호도 유지된다.
- 운영 image `016-bb56d20`, ID `sha256:f4e97de494be67dfd820ea538d0fcd5df49e2455401531e5dcdd8b2784620792`, revision full SHA 및 Compose 지정 확인.
- 복원 ec1255d9: DB **4,912,087,040 B**, vectors **10,100,326,400 B**, seal 존재, objects **11,042개 전부 파일 존재**를 Reviewer가 read-only 확인. private consumer harness에는 current/previous 모두 `_validate_local_seal` 통과 기록이 있다. Reviewer는 해당 전체 consumer/hash 검사를 다시 수행한 것으로 표현하지 않는다.
- 보존된 startup-check exit 0·MCP current g-9270ff0a/ready·timer 유지 근거 확인. 성공 run stdout 유실 및 재구성 seal의 원본 byte 동일성 한계는 계속 명시한다.
- 아래 §5의 structure/views 소실은 publication closure 검증과 별개이므로 “모든 consumer 기능 복원”으로 확대 해석하지 않는다.
- Reviewer 재실행: embedding/CLI/lock 테스트 **178 passed / 3.81초**, retained current/previous/source 보호 회귀 **1 passed / 0.35초**, diff 공백 검사 통과. Executor 전체 suite 결과와 구분한다.

## 2. [P1] 롯데카드: 공시 목록 장애와 PDF 수집을 분리해 복구

### 2.1 실제 실패 단계와 공식 경로

오늘 성공 배치의 롯데 outcome은 discovery failed, reason `issuer_origin_timeout`, 4회 시도, download skipped, 기존 **647문서 carry**다. 이전 source_invalid와 다르며 **개별 PDF 다운로드 실패로 확정할 수 없다**.

공식 상품공시실: [롯데카드 상품공시실](https://www.lottecard.co.kr/app/LPCMNPD_V100.lc). 현행 `issuers/lotte.py`는 이 landing GET을 필수로 거친 뒤 `/app/LPSCHAA_V100.lc`에 `collection=disclosure`, `listcount`, `startcount`, `query`를 POST한다. DOCID와 OCY_FILE_NM으로 상품 identity·PDF URL을 구성한다.

Reviewer 호출 테스트(프로젝트 반영 전 실행):

| 경로/방법 | 관측 |
|---|---|
| www 공식 landing HTTPS GET, apex GET | 이 호스트에서 TCP connect timeout(5–10초 제한) |
| www/apex listing POST, X-Requested-With·Referer·브라우저 UA | TCP connect timeout. JSON/HTML 응답 자체를 확보하지 못함 |
| mobile landing GET, HTTP www | connect timeout |
| 별도 Chromium browser session, 공식 www landing | Page.navigate timeout, session 종료 |
| 공식 image CDN 직접 GET, curl 기본/브라우저 헤더 | PDF 200, 유효한 magic/header/파싱 |
| httpx 기본/브라우저 UA+Referer | Driving Pass PDF 200, 같은 SHA |
| Driving Pass CDN HEAD / Range | 200·Content-Length 233329 / 206·512 B |
| DNS가 반환한 두 번째 CDN node(61.110.199.18) | 정상 TLS hostname 검증 상태에서 PDF 200, 같은 내용 |

여러 클라이언트에서 www의 TCP 연결 단계가 실패하므로 header/parser 변경만으로 해결됐다고 보지 않는다. **로컬 egress/목적지 네트워크·origin 가용성 경계가 우선 조사 대상**이며, WAF 차단이나 롯데 전체 서버 장애로 확정하지 않는다. 검색 도구에서는 공식 공시실 내용이 조회되지만 이는 현재 운영 호스트의 직접 접속 성공을 뜻하지 않는다.

### 2.2 확보한 공식 PDF 다운로드 경로

공통 base: `https://image.lottecard.co.kr/UploadFiles/cardProvisionPath/`.

| 상품 | filename | bytes / pages | SHA256 |
|---|---|---|---|
| LOCA Lite | `P13490-A13490.pdf` | 975,948 / 2 | `27b4748dd3337ebce0e1d6341d6f3d1fed873e3b21762f5b255cefed03664e83` |
| Driving Pass 카드 | `2010-A00046_20261008_10009.pdf` | 233,329 / 6 | `76b3091447fbf95c9615e9364ffd7184a9d419eae436dc92b707ea4a5c7d1524` |
| 롯데카드 I’m CHEERFUL | `2018-A00013_20261008_10007.pdf` | 1,590,577 / 10 | `fb6d687646bf7d89ef8caa84804e5a8c7037930b092a850580ce87cc7a28089f` |
| 롯데 포인트플러스 카드 | `2007-A00006_20261008_10010.pdf` | 224,553 / 4 | `a7fb6120cbc246aeeb88affef109e224339f9978589253534cfe5324a615c379` |

LOCA Lite는 공식 도메인 검색 결과에서 찾아 실제 다운로드했다. 나머지 3개 URL은 현재 서빙 DB의 공식 source_url에서 추출하고 **현재 직접 다운로드해 검증**했다. 후자의 목록 identity/effective_date=10/8은 저장된 근거이지 오늘 fresh inventory 검증이 아니다. 고정 코드에서 URL을 추측해 조합한 것이 아니다.

**확보 범위:** PDF 바이너리의 안정적인 공식 다운로드 경로는 확보했다. 이 호스트에서 **fresh 전체 목록 조회 성공은 미확보**다. 4개 다운로드 성공을 647개 전체 수집/신규 상품 탐색 복구로 표시하지 않는다.

### 2.3 Executor fix 요구·인수

1. landing GET·목록 POST·Status/Content parse·pagination·attachment fetch별 안전한 단계/예외 category/HTTP 여부/소요시간을 분리한다. connect timeout에 response parse 오류나 PDF 오류라는 이름을 붙이지 않는다.
2. bounded 직접 listing POST(필수 cookie/세션 여부 확인), landing-first session, 공식 사이트의 실제 최신 요청 흐름을 순차 검증한다. 쿠키/인증 전체나 raw URL credential을 기록하지 않는다. IP hardcode, TLS 검증 해제, 비공식 proxy/제3자 PDF 미러를 제품 해결책으로 쓰지 않는다.
3. landing이 목록의 필수 dependency가 아님이 입증되고 listing 직접 호출이 성공하면 불필요한 landing 단일 실패 지점을 제거한다. 성공 입증 없이 무조건 제거하지 않는다. 목록 응답·pagination fixture를 확보해 현재 schema/row index 조건을 재검증한다.
4. 목록 장애 시 **검증된 이전 inventory의 공식 CDN PDF를 제한적으로 재검증하는 별도 degraded 모드**를 설계할 수 있다. 이를 fresh discovery 성공이나 신규 상품 없음으로 표시하지 않는다. DOCID/상품 lineage/date/source identity, canonical URL, hash/size/PDF 검증을 보존한다. 이전 링크와 파일명이 바뀐 신규 revision/신상품을 찾을 수 없다는 한계를 표시하고 해당 issuer retirement를 동결한다. 기존 carry와 비교해 얻는 이득이 없는 fallback은 넣지 않는다.
5. 위 확보 URL을 외부 smoke용 자료로 사용하되 테스트는 작은 자체 fixture로 작성한다. URL을 제품 전체 목록으로 하드코딩하거나 날짜/파일명을 임의로 최신화하지 않는다. 기존 명시 404 allowance도 새 근거 없이 확대하지 않는다.
6. 완료는 fresh discovery 전체 pagination + 다양한 PDF 몇 건의 현재 fetch가 검증된 경우와 degraded fallback만 확보한 경우를 구분한다. 후자의 경우 목록 장애가 남았음을 REPORT에 명시하고 origin/egress 해소 조치를 제시한다.

## 3. [P1/P2] 전체 호스트 용량: 실행한 안전 정리와 추가 관리 과제

### 3.1 Reviewer가 실제 수행한 조사·정리

- 범위: Docker **40개 volume/15개 container/41개 image**, system/user VM 목록, `/home/lee`·`/var`·`/opt` 1단계 사용량, 큰 디렉터리의 제한된 하위 조사, 실제 실행/예약 Compose volume 참조.
- Docker가 표시한 “회수 가능 volume **48.04GB**”의 대부분은 **예약 Worker state 45.89GB + Paddle 모델 2.069GB + auth 79MB**다. 현재 연결된 Worker 컨테이너가 0개여서 dangling으로 보이는 **운영 자산**이다. 실제 운영 overlay가 external name으로 참조함을 확인하고 모두 보존했다.
- 다른 프로젝트의 LibreChat Postgres/Mongo/model volume, empty 개발 memory/smoke volume, anonymous 소량 volume은 삭제하지 않았다. 미참조 표시와 데이터 용도를 구분했다.
- `/var/tmp` 약 **21GiB**의 `antigravity-haos-vm-1000-4e4fc806baf5`는 system libvirt에 등록된 shut-off VM이다. domblklist가 해당 system.qcow2/CONFIG.ext4를 참조하고 파일은 10/14에 갱신됐다. **등록된 VM이 꺼져 있다는 이유로 삭제하지 않았다.** `fast-home-control-test/haos.qcow2` 약 2.5GiB도 보존했다.
- local BuildKit 작업 부재 확인 후 unused cache만 정리: `docker builder prune --all --force`. 앞선 until=24h/keep-storage 시도는 회수 0B였으며 실제 효과와 구분한다. 사용 중인 cache/실행 image/volume은 삭제하지 않는다.
- Docker 집계 회수 **36.99GB**, 동작 전후 **df 실제 증가 33,971,699,712 B(약 33.97GB / 31.64GiB)**. free **84,395,307,008 → 118,367,006,720 B**, 이후 최종 조회 118,364,676,096 B, 사용률 대략 **79%→71%**. 논리 cache 집계와 실제 물리 회수량을 혼동하지 않는다.
- after: 원래 15개 컨테이너의 status/health/image **변화 없음**, live 11개. 기존 40개 volume 이름/labels/driver **동일**, 모든 image 및 rollback 보존. MCP ready true. cache는 37.04GB→약 50.21MB(in-use만). 향후 빌드의 cache 재사용은 줄어들 수 있지만 프로젝트 데이터/실행 서비스는 변경하지 않았다.

### 3.2 Executor fix 요구·인수

1. 정리 전후 수치와 보호 대상 목록을 운영 문서에 남긴다. **dangling volume 일괄 prune 금지**, 특히 finite/scheduled Worker와 중지 VM을 active-reference 조사에 포함한다.
2. 임시 전체 cache 정리만으로 끝내지 말고 **CardRAG 빌드 전용 cache 관리**를 검토한다(예산/retention/빌드 후 안전 회수). 다른 프로젝트의 글로벌 daemon 설정이나 자동 정리 정책을 임의로 변경하지 않는다.
3. 대형 중복 generation/run은 native retention·reader/transitive source/backup·복구 closure 검증으로만 정리한다. 다시 공간이 부족하다고 protected run seal이나 VM disk를 수동 삭제하지 않는다. MCP GC는 실제 lifecycle의 보호 근거를 확인하기 전 삭제하지 않는다.
4. 현재 조사에서 **안전하게 삭제 가능한 대형 미사용 volume은 확인되지 않았다**. 원본의 크기만 보고 추가 수십 GB 회수를 약속하지 않는다. 새 후보는 소유 프로젝트/사용 중·예약·복구 참조와 회수량을 검증한 경우에만 개별 조치한다.
5. 이번 정리는 이미 실행됐으므로 같은 cache prune/전체 스캔을 반복하지 말고 최신 디스크·서비스 상태만 필요 범위에서 확인한다.

## 4. [P1] 0.999의 매일 배치 단독 hard gate 적용을 재설계

### 4.1 검토 결론

**0.999 자체를 보편적 정답으로 간주할 근거는 없고, 현재의 “24개 중 하나라도 미달이면 모든 수집·갱신 중단”은 검색 품질에 대한 calibration 없이 가용성을 차단하는 정책이다. 정책 정정 요청한다. 다만 재현성 검사가 무의미하거나 0.9972가 안전하다고 판정한 것은 아니다.**

- 이 검사는 같은 고정 한국어 문장을 두 번 요청한 벡터의 cosine이다. 카드 상품 간 유사도나 검색 결과의 정확도가 아니며 0.999=99.9% 정확도라는 해석은 잘못이다.
- 공식 [Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B) 및 [Qwen3 Embedding 논문](https://arxiv.org/html/2506.05176v3)은 정규화 임베딩과 retrieval/MTEB 평가를 다룬다. 확인한 자료에 이 프로젝트의 **매일 전샘플 반복 cosine≥0.999** 운영 차단 기준을 뒷받침하는 calibration은 없다.
- [PyTorch numerical accuracy](https://docs.pytorch.org/docs/2.14/notes/numerical_accuracy.html)는 backend/platform/batch 방식에 따른 수치 차이를 설명한다. 이는 작은 차이가 있을 수 있다는 근거이며 **오늘 0.9972 차이가 정상적인 roundoff라는 증거는 아니다**.
- 코드 주석도 현재 24개 문장이 provider consistency 검사이며 production gold benchmark가 아니라고 명시한다. 실제 서비스는 cache/profile이 지정한 provider로 query embedding을 수행하는데, startup은 **선택하지 않은 provider까지 매번 2회 검사**한다. 비활성 provider의 품질/가용성 때문에 활성 provider의 배치가 멈추는 결합은 재검토 대상이다.
- 매일 신규 OCR·문서는 소수지만 기존 벡터를 대량 재사용한다. 새 벡터끼리 repeat=1.0이어도 과거 cache/현재 query와의 호환성이 보장되지 않는다. 따라서 검사 폐기보다 **이전 검증 벡터와 현재 결과의 호환성 및 실제 검색 순위 영향**을 검증하는 편이 목적에 가깝다.
- unit-vector 기준 c=0.999는 각도 약 2.56°/L2 delta 0.04472, 관측 c=0.9972087은 약 4.28°/delta 0.07472다. 임의 unit query와의 score 차이 상한도 이 L2 delta다. 이것만으로 실제 순위 악화나 무해함을 확정할 수 없다. 실패 sample0은 전월 30만원/대중교통 10% 문장으로 업무와 관련 있는 입력이다.
- 2/5 실패 관측을 미래 40% 실패확률로 사용하지 않으며, 단순히 과거 실패를 통과시키려고 threshold를 0.995 등으로 정하지 않는다.

### 4.2 Executor fix 요구·인수

1. **daily 운영 smoke와 deployment/provider qualification을 분리**한다. 일일은 선택된 provider의 유효성/model/route/dimension/finite/norm/tokenizer 검사를 필수로 하고, 비선택 provider 비교는 qualification/주기적 진단으로 이동한다. provider 자동 전환은 하지 않는다.
2. repeat 0.999는 **재현성 경고 기준으로 유지하는 방향**으로 정책을 수정하고, 그 단일 scalar만으로 전체 일일 수집을 차단하는 결합을 제거한다. 단, 새 안전 장치 없이 경고 처리만 하여 쓰레기 벡터를 게시하지 않도록 아래 평가/승격 기준을 함께 구현한다.
3. 현재 production profile의 소규모 anchor query/document 및 이전 검증 embedding을 결속해 old-cache/new-embedding 호환성을 평가한다. 기존 gold/retrieval 평가를 우선 재사용하여 Recall@10/nDCG@10/MRR@10·top-k overlap/중요 숫자·제외조건 slice의 변화를 측정한다. corpus/profile 변경 효과와 provider drift를 분리한다. source/profile hash로 anchor의 provenance를 남긴다.
4. 관측한 허용 변동 분포와 실제 업무 영향으로 hard reject 조건·경고 구간·품질 승격 조건을 정한다. 무의미/불필요 검사가 확인되면 제거 근거·대체 검증을 REPORT에 남긴다. 검증 자료가 부족하면 임의 새 수치로 배포하지 말고 정책 후보/확인 부족을 명시한다. 이전 016 PLAN의 0.999 무변경 원칙은 **이번 사용자의 재검토 지시에 따른 정정 대상**이다.
5. malformed/다른 model·provider·dimension/nonfinite/잘못된 tokenizer/심각한 의미 품질 저하는 계속 fail-closed. 동일한 잘못된 벡터를 반환해 repeat=1.0인 경우도 의미 검증에서 거절하고, 작은 반복 차이가 있어도 허용된 검색 품질을 유지하는 경우 warning+성공이 되도록 검증한다. 캐시 namespace/profile을 몰래 혼합하지 않는다.
6. 검증은 먼저 saved vector/오프라인 fixture로 수행한다. 실제 소규모 sample 호출이 필요한 경우 provider·batch 수·sample 수·timeout을 사전 고정하고 전체 corpus 재임베딩을 피한다. Reviewer는 이번 검토에서 유료 embedding probe를 추가하지 않았다.
7. 성공 여부 외 quality_status/경고·anchor 비교 결과·qualification 날짜를 보고한다. 정상화가 단순 “실패 숨김”이 되지 않게 final promotion/ready 의미를 정의한다. 기존 deadline/busy/취소/기밀 경계를 보존한다.

## 5. [P2] 복원 범위: structure/views partial reuse까지 복원됐다는 주장은 정정

FIX_01_REPORT §1.2는 원본 intermediate 밖 파일을 “어떤 consumer도 참조하지 않음”으로 표현한다. 그러나 `partial_execution.py:ReuseSource.structure`는 source run의 `documents/<id>/structure/structure.v2.json`과 `views.v1.json`을 읽고, pipeline의 `--skip structure` 경로가 호출한다.

Reviewer가 복원 ec1255d9의 실제 sample document `doc_37dc81f65ebbbcbe5cd3b104451b2e074f75e9590c8092dca4fcf54c3f1a959c`를 조회한 결과 OCR 파일은 존재하지만 structure/views는 모두 없다. 즉 **publication seal 소비 closure 복원은 확인했지만, 해당 run을 source로 하는 모든 partial skip 기능까지 복구됐다고 할 수 없다**.

Executor는 source run capability를 검사해 지원되는 reuse 범위와 소실 범위를 명시한다. 검증 가능한 원본이 있으면 복원하고, 없으면 해당 skip에 대해 명확한 fail-closed/실행 전 preflight 오류 및 재계산 경로를 유지한다. placeholder 또는 임의 재생성 bytes를 원본이라고 표시하지 않는다. 구조물을 모두 다시 생성하거나 전체 배치를 반복할 필요는 없다. 관련 preflight fixture와 문서로 증명하고 이전 REPORT 대신 새 보고서에서 범위를 정정한다.

## 6. 증거·제출 및 재검토

- private Reviewer 근거: `/opt/cardrag/016-evidence/operations/reviewer-20261016/`의 `inventory-before.json`, `health-after.json`, `builder-cleanup-all-unused.json`, `cleanup-final.json`, `lotte-route-probes.json`, `review-source-refs.json`.
- 이 파일들은 source/env/auth 전체 dump가 아닌 allowlist 자산/측정 요약이다. 상세 쿠키·token을 public handoff에 복사하지 않는다.
- 제품 코드는 Reviewer가 수정하지 않았다. 실제 정리는 user가 승인한 호스트 전체 unused build cache 범위만 수행했고 volume/VM/프로젝트 데이터는 보존했다.
- Executor는 이 FIX를 실행하고 **`FIX_02_REPORT.md`**를 새로 작성한다. 이전 수정 인수 결과와 새 요청 완료 여부를 분리하고, 롯데 fresh inventory 미확보/검색 calibration 부족 등 남은 조건을 숨기지 않는다.
- validation: 변경에 맞는 lotte HTTP/issuer fixture, preflight/quality/누출/timeout, partial source capability, active provider와 qualification 분리, 기존 lint/type. 현재 safe cleanup을 다시 수행할 필요 없다. 필요한 이미지/운영 반영은 증거를 남기며, 충분한 공간·품질·writer 부재 조건에서만 full run 1회를 실행한다.
