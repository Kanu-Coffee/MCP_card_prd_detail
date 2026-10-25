# 017 Executor REPORT

작성: 2026-10-19, Codex Executor. 사용자의 후속 지시 `executor로서 구현하라`에 따라 PLAN/ANALYSIS의 설계를 구현했다. Planner 문서의 당시 작업 제한은 Planner 단계의 이력으로 보존했다. PLAN.md·ANALYSIS.md 및 기존 016 문서는 수정하지 않았다.

**결과: 구현·회귀 시험 완료, 운영 배포 및 전체 운영 인수는 미완료.** 특히 실제 롯데 목록에서 새 ZIP 첨부가 발견되어 PDF 전용 complete inventory 수집은 여전히 중단된다. PDF 접근 경로가 확보된 사실과 전체 카드사 배치 성공을 구분해야 한다.

## 1. 시작 상태와 변경 범위

- 시작 HEAD: `aaeb2c4`, 운영 증빙 이미지 `cardrag-worker:016-d8a9018`.
- 시작부터 있던 untracked 016 connectivity 문서와 017 PLAN/ANALYSIS를 보존했다. 다른 작업을 reset/삭제하거나 Git commit을 만들지 않았다.
- 읽기 전용 서비스 조회: Worker inactive/dead, Result=success; timer active/waiting; 기존 tunnel active/running.
- 운영 Worker 실행·교체, service/timer/env/SSH/VPN 변경, 볼륨 삭제·캐시 초기화, 운영 OCR·embedding·원격 백업은 수행하지 않았다.
- 변경: Worker 코드, 기존 관련 시험 및 신규 `test_017_scope.py`, `.env.example`, Worker Compose, `docs/OPERATIONS.md`, 본 신규 REPORT.

## 2. 구현

### 원본 호출 정책과 계측

- `origin_policy.py`: 실제 원본 확인 시각과 다음 감사 기한을 별도 영속화한다. 기본 원본 감사 주기는 2160시간이며 정상 재확인은 source별 81–90일로 분산한다. 순회에서는 다음 방문까지의 pool 간격을 앞당겨 due를 판단한다.
- 기존 binding은 한 번만 감사 기한을 분산 배정한다. 아직 90일 전인 binding은 원래 90일 상한 안에 배정한다. 이미 상한을 넘긴 설치는 한 번의 분산 이행 창을 사용하되 실제 원본 나이 기준 overdue를 숨기지 않는다. 로컬 검증/목록 조회로 원본 확인일을 갱신하지 않는다.
- 신규·내용 identity 변경·유실/손상은 즉시 기존 strict downloader 검증으로 처리한다. 동일 캐시·due 이전에는 prepare/GET/HEAD를 생략한다. 상품명만 바뀌고 다른 첨부/버전/날짜/adapter metadata가 전부 같으면 이전 PDF와 실제 감사 시계를 재사용한다.
- 알려진 DRM과 정확한 롯데 404 관측을 source별 negative cache에 저장한다. 알려지지 않은 DRM은 1일 후 재확인한다. hit 시 만료를 계속 연장하지 않으며 source identity 변경은 새 관측을 요구한다.
- `issuer_metrics.py`: issuer/실제 host/목적/run별 요청, status, 읽은 body bytes, 연결 실패/timeout/취소·body 오류를 기록한다. PDF 검증 성공만 집계하지 않으므로 DRM/error body도 포함된다.
- 기본 요청 예산 10000회, body 예산 4 GiB를 issuer/host/run에 적용한다. 값은 환경설정으로 조정 가능하며 자동 복구는 같은 run 누적값을 사용한다. 예산 소진은 `issuer_origin_budget_deferred`로 격리한다. 목록을 잘라 성공 처리하지 않는다.
- 403/429와 Retry-After, 연결 장애 cooldown을 영속화한다. Retry-After 없는 차단은 기본 1시간, 연결 장애는 5분이다. cooldown/예산/잘못된 source에 대한 discovery 전체 반복 재시도를 중단한다.
- 지표는 앱이 실제 읽은 response stream 기준이다. TCP overhead·아직 읽지 않은 OS/HTTP 버퍼까지 포함한 패킷 계측값이라고 주장하지 않는다.

### 목록 inventory와 프록시

- `detail_inventory.py`: 우리·현대는 매번 상위 목록을 확인하고, 저장한 parsed detail을 합성해 complete inventory를 만든다. 신규/상위 metadata 변경은 즉시 상세 조회하고 기존 상세는 상품별 15–30일에 분산 확인한다. 상위 목록에 나타나지 않는 개정 발견은 이 주기만큼 늦어질 수 있다.
- 우리 main 고정 한도 1000건에 도달하면 불완전 목록으로 거부한다. 다른 카드사의 경량 전체 목록 walk와 기존 minimum/retention 보호는 유지한다.
- `issuer_http.py`: 값 없는 기본 설정은 직접 연결이다. `CARDRAG_ISSUER_PROXY_URL` 설정 시 HTTPS/443의 정확한 `www.shinhancard.com`, `www.lottecard.co.kr`, `image.lottecard.co.kr`만 우회한다. 실제 request port도 검사한다.
- discovery/prepare/PDF stream 및 각 redirect hop에 같은 client 정책을 적용한다. 현대 TLS 예외, downloader origin/DNS/TLS/redirect 제한을 유지한다. ambient proxy는 사용하지 않으며 proxy 장애 시 직접 fallback하지 않는다.
- 실제 endpoint·주소·SSH 키·VPN 설정은 저장소에 추가하지 않았다. 예시는 빈 값이고 진단 예외에 proxy URL을 넣지 않는다.
- 실측에서 발견한 신한 `+` 상품명 검색 문제를 보완했다. 정확히 빈 이름 검색 결과일 때 짧은 literal prefix를 제한적으로 다시 조회하되, 최종 선택은 기존 전체 stable identity singleton 검사를 통과해야 한다. source/document identity는 바꾸지 않는다.

### 선택 처리와 전체 serving 보존

- `issuer_scope.py`: configured pool, 이번 selected work, 전체 publication을 분리했다. `run --issuer CODE` 반복 선택, `run --all`, 기존 전체 기본 실행을 제공한다. canonical registry를 재사용하고 선택·옵션 충돌을 provider/origin 작업 전에 거부한다.
- 실행 범위는 immutable run receipt에 고정한다. resume은 원래 범위를 사용하며 모순되는 선택/전체/순회 옵션을 거부한다.
- 비선택 자료는 현재 serving에 바인딩된 retained seal/DB/PDF/OCR/구조/view를 검증해 합본에 복사한다. 비선택 자료를 failed로 등록하거나 OCR 작업 큐에 넣지 않는다. OCR resolver/healing, parser/view 생성, embedding provider를 호출하지 않는다.
- 비선택 vector cache가 없으면 새 embedding으로 보충하지 않고 게시를 중단한다. schema/parser/embedding 재사용 호환성·참조가 불명확해도 전체 재처리로 확대하지 않는다.
- 현재/과거 개정, OCR 실패, DRM, 구조 fallback 이유, unresolved revision 이력도 보존한다. configured pool에서 빠진 기존 카드사의 serving도 유지한다.
- 비선택은 `reports/issuer-collection.json`의 별도 `not_scheduled`로 표시한다. issuer collection 성공/실패 행과 감사 시각을 갱신하지 않고 retirement 판정에서도 동결한다. 합본 manifest/DB와 seal/current/previous 소비 구조를 유지한다.
- 전체 기준선 없는 선택 게시, 손상된 기준선, 유실 vector는 fail closed다. 실제 선택 카드사가 모두 실패하면 기존 serving을 유지한다.

### 순회와 독립 단계

- `rotation.py`: 같은 worker lock 안에서 SQLite에 schedule/channel/KST 실제 날짜별 day slot을 배정하고 cursor를 같은 transaction에서 한 번 전진시킨다. 실패 카드사 때문에 다음 카드사가 굶지 않는다.
- `CARDRAG_ISSUER_EXECUTION_MODE` 빈 값/all은 기존 전체 실행, round_robin은 일일 한 카드사다. 수동 선택/전체/독립 단계는 순회 cursor를 소비하지 않는다.
- 당일 terminal 재호출은 저장 결과를 반환한다. 미완료는 같은 issuer/run/누적 HTTP 예산을 재개한다. 자동 복구에서 기존 finite stage attempt를 초기화하지 않는다.
- 게시 후 slot 완료 전 중단은 기존 provider-free seal resumer로 완료 증거를 복구한다. 같은 날 seal만 남은 경우 게시만 재개한다. 더 최신 수동 pointer는 predecessor fence로 보호한다.
- 전날 미완료 작업의 origin/OCR은 오늘 자동 재개하지 않는다. 이미 게시된 증거를 정리하거나 interrupted 이력을 남기고 실제 오늘 한 slot만 배정한다. missed 날짜만큼 몰아 실행하지 않는다.
- pool 추가는 다음 cycle에 반영하고 제거 시 남은 배정을 이어간다. roster 변경은 migration 이력으로 남기며 기존 serving을 삭제하지 않는다.
- `acquisition.py`, 기존 partial CLI/replay: `--phase collect`는 provider 초기화 없이 수집 receipt만 남긴다. `--phase process --reuse-from-run ID`는 검증된 receipt로 원본 HTTP 없이 처리·병합·게시한다. 수집 이후 serving 기준선이 달라지면 재수집을 요구하며 최신 pointer를 덮지 않는다.
- 미처리 receipt/PDF와 retained/resumable source 참조를 retention/prune에서 보호한다. process 완료 표시는 ready publication 증거를 확인해 해제한다. process-only는 원본 관측 시각을 오늘로 바꾸지 않는다.
- 혼합 OCR 재처리 요청은 원본 불변 상태로 결정적인 issuer별 child로 분할한다. eligible child만 선택하고 비선택 요청은 pending으로 유지한다. parent 완료는 모든 child의 publication receipt를 검증한 별도 conjunction receipt로 남긴다.
- 선택 배치와 process 단계의 백업은 선택 artifact만 등록/flush한다. 관련 없는 전역 native cache 및 pending을 자동 소진하지 않는다. 백업 실패는 이미 완료된 게시를 실패로 되돌려 OCR을 반복하게 만들지 않는다.

## 3. 설계 조정 및 남겨 둔 제한

1. process는 초기 버전에서 **수집 receipt 전체의 고정 선택 범위**만 처리한다. receipt 일부 issuer만 따로 처리하거나 source 밖으로 확대하는 옵션은 거부한다. 기존 stage skip과 scoped phase의 조합도 거부한다. 기존 전체 skip/reuse 기능은 유지한다.
2. scoped/순회 phase=all의 dry-run은 현재 CLI에서 거부해 slot/cursor/origin 부작용을 막는다. 기존 전체 partial dry-run과 collect/process preflight는 유지한다. 순회 선택을 보여 주는 읽기 전용 preview는 구현하지 않았다.
3. 자동 day claim은 로컬 설정·용량 안전 검사와 writer lock 이후, provider 초기화 이전이다. 전역 로컬 용량 검사 자체가 실패한 경우 slot을 소비하지 않는다.
4. 신한 session token map/전역 cookie 공유는 도입하지 않았다. 토큰 유효기간·회전·session 범위가 충분히 입증되지 않았기 때문이다. 이번 literal prefix 경로는 실제 PDF 검증과 fixture로 확인했다.
5. 예산 기본값은 초기 계측용 보수적 상한이다. 운영 8사 실측을 통한 최종 상한 조정과 이미지/네트워크에서 신규 코드의 장기 순회 평가는 남아 있다. 모든 crash 지점·구성 변경 조합을 실제 운영 프로세스 강제 종료로 재현한 것은 아니다.
6. 동일 corpus/다른 contract는 기존 두 운영 run 증빙에서 확인된다. 원격 endpoint raw metadata 변동이 유일한 원인인지 sealed DB를 추가 읽으려 했으나 호스트 volume 권한으로 PermissionError였다. 입증 없이 endpoint metadata hash나 의미 계약을 제거하지 않았다. 87분 로컬 재생성 최적화를 완료했다고 주장하지 않는다.

## 4. 실제 프록시 origin 시험

운영 Worker 전체를 실행하지 않고, 현재 저장소 HTTP factory/downloader를 사용하는 별도 임시 호스트 프로세스로 시험했다. 비공개 proxy endpoint를 사용했고 운영 state/cache/provider는 열거나 변경하지 않았다. 임시 PDF는 검증 후 제거했다. 이미지 안의 신규 코드 배포 시험과 구분한다.

| 대상 | 결과와 한계 |
|---|---|
| 신한 공시·목록·PDF | 최종 bounded 시험에서 요청 **6회**, 읽은 body **3,338,596 B**, 전부 HTTP 200. PC 첫 페이지 10행 해석 후 실제 adapter.prepare_download와 strict downloader 성공 |
| 신한 상품 | **제주은행 신한카드 Discount Plan+**, PDF **3,321,170 B / 8쪽**, SHA-256 `fc5a3a9d960faaa8498645aad6b499d89993613c8d5f3bbef656854c73470b56` |
| 롯데 공시·목록 | HTTP 200이나 첫 100행에 **DOCID 1893**, `2023-A00026_20261012_10004.zip` 첨부가 있어 PDF 전용 parser가 `Lotte disclosure attachment filename is invalid`로 거부. 최종 bounded 목록 시험 요청2회/body201,892 B |
| 롯데 정상 PDF 경로 | 목록의 정상 PDF 1개를 별도 선택해 공식 image host 경로를 strict downloader로 검증. **롯데 패밀리 W I'm WONDERFUL 카드**, **581,818 B / 12쪽** 성공 |

초기 신한 시험은 전체 이름 검색이 빈 결과여서 fallback 전수 순회 중 8회 예산에 도달했다. 공개 목록과 모바일 목록을 소수 요청으로 비교해 같은 상품이 빈 검색에는 존재함을 확인했고 prefix 보완 후 6회 성공했다. 최초 실패를 성공으로 덮어 기록하지 않는다.

롯데의 ZIP을 PDF로 위장하거나 DOCID를 조용히 제외하지 않았다. archive-aware 첨부 모델·추출 한도/경로·PDF 복수 파일 identity와 전체 inventory 보존을 별도로 설계해야 한다. 그 전까지 롯데 complete collection의 운영 인수는 보류이며, 기존 issuer failure/carry와 이전 stable 보존이 필요하다. 목록 전체 pagination, 실제 proxy 304 및 컨테이너 신규 이미지의 full batch까지 검증한 것은 아니다.

## 5. 검증 명령과 결과

- `uv run --no-sync pytest -x -q --tb=short`: **2565 passed, 10 warnings**, 73.89초. 기존 의도된 OCR cache/fallback 경고이며 신규 실패는 없다.
- 마지막 process 백업 연계와 scope 백업 시험 추가 후 `uv run --no-sync pytest apps/cardrag-worker/tests/test_017_scope.py apps/cardrag-worker/tests/test_partial_execution.py apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py -x -q --tb=short`: **64 passed, 1 warning**, 20.38초.
- 신규 scope 시험에는 2사/8사 baseline의 단일사 변경과 비선택 문서 보존·OCR/adapter 호출 제외, collect→process의 부작용 분리, 90일 분산 이행/날짜 유지, 상품명 변경 재사용, host/port/ambient proxy 제한, HTTP status/body/cooldown, warm detail inventory/개정 발견, mixed OCR queue, same-day/manual/cancel 복구, 게시 완료 후 slot 미완료 복구, 최신 수동 pointer 충돌, 선택 백업/전역 cache 제외가 포함된다.
- `ruff check`: 변경 Worker 코드와 관련 시험 통과. `ruff format --check`: 67개 파일 통과. `git diff --check` 통과.
- gitleaks로 변경·신규 제품/시험/설정/운영 문서 약1.17MB 스캔: **no leaks found**. 실제 proxy URL·SSH/VPN 정보는 보고서에도 넣지 않았다.
- 기존 PDF cache 만료 시험은 새 90일 기본값에 맞게 91일 경과를 사용한다. 다른 정상 candidate head fixture는 실제 validated transport처럼 canonical inventory manifest도 제공하게 보완했다.

## 6. 운영 반영 전 Reviewer 확인

- 롯데 ZIP 첨부 문제를 해결하거나 complete inventory를 훼손하지 않는 명시적 보존 정책을 승인하기 전, 롯데 전체 수집 정상으로 판정하지 않는다.
- 배포 시 기존 private env의 명시적 168시간 값은 코드 기본값보다 우선한다. 90일을 채택하면 private env도 이행해야 한다. proxy endpoint는 컨테이너에서 도달 가능한 비공개 값으로 별도 설정한다. 이번에 변경하지 않았다.
- 새 이미지/container에서 소수 요청의 전체 목록 schema·session·PDF/304/redirect와 실제 현재 serving의 8사/과거 개정·벡터 연결을 대조하고 순회 mode를 opt-in한다. 이 보고서는 신규 이미지 운영 인수서가 아니다.
- 일반 전체 배치, 선택 게시, 자동 순회, pending collect/process/OCR child가 참조하는 source를 유지한다. 볼륨/전체 state를 삭제해 이행하지 않는다.
- 예산/overdue/not_scheduled·실패/carry·백업 pending을 분리해 관측한다. 롯데 ZIP 실패와 proxy 연결 실패를 같은 장애로 합치지 않는다.
