# 016 FIX_03 — FIX_02 실행 결과 리뷰 및 저장소 자원 판정

- 작성: 2026-10-16, Reviewer Codex.
- 대상: PLAN/REPORT, FIX_01/REPORT, FIX_02/REPORT, 현재 working tree diff.
- HEAD `1764aef`; Executor의 FIX_02 구현은 아직 **미커밋 7파일 변경**이다. 이를 보존하고 Reviewer는 제품 코드를 수정하지 않는다.
- 판정: **수정 요청. 292개 테스트는 통과했지만 품질 정책 인수와 롯데 degraded 경로 구현 완료를 인정하기 어렵다.**
- 기존 운영 Compose는 `016-bb56d20`으로 직전 검증 코드를 사용한다. 아래 미완료 정책을 새 이미지로 반영하기 전에 수정/평가한다.

## 1. [P1] 0.990/0.980은 calibration 없이 도입됨

**위치:** `embedding_v5.py` 신규 `REPEAT_COSINE_HARD_REJECT_THRESHOLD=0.990`, `SEMANTIC_COLLAPSE_THRESHOLD=0.980`; `cli.py:_qwen_embedding_provider`; FIX_02_REPORT §2.2/§4.2.

FIX_02는 0.999의 단독 차단을 재설계하되, old-cache/new-embedding anchor와 실제 검색 품질로 안전한 승격 조건을 먼저 검증하도록 요구했다. 이번 변경은 임의 0.990 하한 및 두 샘플 간 0.980을 추가했으며 **anchor/Recall@10/nDCG@10/MRR@10 평가 구현·실측·결정 근거가 없다**. 보고서도 calibration을 향후 과제로 남겼다. 새 숫자에 맞춘 synthetic 통과/거절 테스트는 정책의 타당성 근거가 아니다.

**요구:** 선택 provider 검사/qualification 분리 방향은 유지하되, old-cache compatibility와 업무 gold에 기반한 평가 자료를 확보한다. 새 hard cutoff가 결정되는 근거·warning/promotion 의미·허용 변화량을 기록한다. 평가 자료가 부족하면 기존 검증된 정책을 운영에 유지하고 “새 정책 후보, 미인수”로 표시한다. 단지 오늘 0.9972를 통과시키기 위해 0.990을 고정하거나, 계산상 경계 테스트만으로 안전하다고 주장하지 않는다. 자료 확보 전 신규 정책을 배포하지 않는다.

## 2. [P1] 의미 축퇴 방어는 production 경로에서 꺼져 있음

**위치:** `embedding_v5.py:preflight_openrouter_qwen_providers(check_semantic_discriminability=False)` 및 CLI 호출부(옵션 미전달).

보고서/운영 문서는 “정적 더미 벡터를 차단”한다고 서술하지만 CLI는 새 검사를 켜지 않는다. 오프라인 Reviewer 재현에서 실제 preflight 함수에 **모든 다른 문장에 동일한 정상 차원 unit-vector**를 반환하는 MockTransport를 주입하고 CLI와 동일한 selected provider/daily_smoke/0.999/0.990 설정으로 호출했다. 결과 **quality_status=passed, repeat=1.0**이었다. 신규 테스트는 옵션을 명시적으로 True로 설정한 함수만 검사하므로 production 통합 누락을 잡지 못한다.

**요구:** 명시된 semantic 보호가 실제 production 경로에서 수행되도록 연결하고, §1의 calibration과 함께 안전한 판단 기준을 정한다. 단순히 flag=True만 넣어 미검증 0.980을 새 오탐 gate로 만들지 않는다. 실제 CLI startup을 통한 상수 벡터 거절 회귀와, 정상 의미 샘플/경고 편차의 평가 결과를 검증한다. 검사가 적용되지 않은 상태라면 문서/REPORT에서 보호 완료라고 쓰지 않는다.

## 3. [P2] 미측정 cross-provider가 1.0으로 표시되고 warning의 종단 이력이 부족함

**위치:** `ProviderComparisonReport` default, `preflight_openrouter_qwen_providers` 단일-provider else, CLI의 Qwen passed log/startup-only return/recorder.

단일 provider만 요청하면 실제 cross 비교가 없는데 minimum/mean을 **1.0**으로 채우고 CLI는 `minimum_cross_provider_cosine=1.000000`으로 출력한다. Reviewer의 동일 MockTransport 실행에서도 확인했다. 이는 “미측정”과 “완전 일치”를 혼동시키는 허위 측정값이다. 또한 warning_count/quality_status는 INFO/WARNING 로그에만 있으며 startup history의 passed 기록과 최종 ready JSON은 원측정 warning을 구조화해 보존하지 않는다. 새 warning schema가 선언돼 있어도 실제 warning sample index/hash 이력으로 연결되지 않았다.

**요구:** 미측정은 null/explicit not_evaluated로 표현한다. 사용 provider 수·mode·측정 여부를 포함하고 타입/consumer/tests를 정합화한다. startup final JSON 및 영속 이력에도 warning 상태/유한 수치/허용 sample hash를 보존한다. 전체 stage의 준비 여부와 품질 상태를 구분하고, 미측정 cross=1.0을 테스트로 고정하지 않는다. 기존 schema 기반 누출 방어를 유지한다.

## 4. [P2] 롯데 degraded 재검증은 호출되지 않으며 HEAD 200만으로 PDF를 인정함

**위치:** `issuers/lotte.py:revalidate_degraded_sources`; `rg` 결과 production 참조 없음(정의 1곳과 직접 unit test만 존재).

현행 `discover_current`의 landing/listing 단계는 변경되지 않았고 신규 helper는 pipeline/CLI에 연결되지 않았다. 따라서 보고서의 “단계 세분화/장애 격리 및 degraded 경로 구현 완료”는 실제 제품 동작을 과장한다. helper도 HEAD 200이면 Content-Type/byte/PDF magic/hash 없이 valid source로 반환한다. 로그인·오류 HTML의 200, HEAD 미지원 405 등은 처리되지 않는다. 전체 sources 순회에 별도 개수/총 시간 예산도 없다.

**요구:**

1. 이 경로의 실효성을 기존 carry와 비교한다. 효과가 있으면 production 경로에 bounded 방식으로 연결하고 discovery/attachment/parse 각각의 안전한 진단 이력을 검증한다. 실제 이득이 없으면 사용되지 않는 helper를 제거하고 “공식 CDN은 접근 가능, fresh 목록은 네트워크 장애로 미복구”로 정직하게 범위를 정정한다.
2. 연결 시 HEAD는 가용성 힌트로만 사용하고 bounded GET/기존 PDF 검증·identity/hash를 재사용한다. HTML 200, HEAD 405, redirect/허용 host, timeout, 변경 bytes 및 404를 테스트한다. 이전 inventory의 404를 전체 fresh 목록에서의 단종으로 취급하지 않는다.
3. 전체 호출 수/동시성/소요시간 예산을 정하고 fresh discovery 성공과 degraded/stale를 구분한다. 새 URL hardcode/일괄 skip을 하지 않는다. 목록 단계 TCP timeout이 그대로인 한 전체 수집 복구 완료라고 표시하지 않는다.

## 5. 요청한 3개 저장소 자원의 생성·용도 판정

### Worker state: 개발 잔여물 아님

- volume 생성 2026-10-08 14:43:46 KST, 이름 `cardrag-worker-state`, 실제 운영 Compose external 참조.
- 010 운영 전환 REPORT 및 cutover script는 `cardrag-worker-v130-candidate-state` 등의 기존 데이터를 안정 운영 이름으로 이동한 이력을 기록한다. 즉 시작 단계에 candidate 이름이 있었어도 **현재는 이 MCP_card_prd_detail 프로젝트의 운영 상태**다.
- 현재 latest succeeded run은 오늘 `9270ff0a…`, previous ready publication은 전일 `ec1255d9…`. 두 run이 native retention=2의 보호 대상이다.
- 실측 allocated: runs **34,101,248,000 B**, Worker SQLite **7,226,638,336 B**, PDF cache **4,507,963,392 B**, OCR seed 약 104.6MB, backup ledger 약 24.4MB, 기타 소량.
- run 구성: current sealed 약 15.04GB + documents 약 3.95GB, previous sealed 약 15.02GB + OCR 약 95.7MB. 직전 삭제로 깨진 previous seal을 복원한 바로 그 자산이다.
- SQLite에서 `embedding_cache_v5`가 **6,749,876,224 B**, 실제 다수 4096D 벡터 재사용 캐시다. freelist_count **0**, page_size=4096 → VACUUM으로 회수할 free page 없음.
- 전체 volume/현재·이전 run/DB/cache 삭제는 기동·복구·OCR/embedding 재사용을 손상하므로 수행하지 않는다. 대형 미사용 run은 현재 없다. volume이 dangling인 이유는 oneshot Worker 컨테이너가 종료 후 삭제되기 때문이지 데이터 미사용이 아니다.

### Paddle 모델: 운영 fallback

- volume 생성 2026-09-20 12:23:58 KST, 현재 운영 Compose의 Codex primary + local-paddleocr/PaddleOCR-VL-1.6 fallback이 참조.
- 실제 공식 모델: PaddleOCR-VL-1.6 약 **1,930,518,528 B**, PP-DocLayoutV3 약 **132,014,080 B**, PP-LCNet doc orientation 약 **6,885,376 B**. 중복 구버전 대형 모델 디렉터리는 발견되지 않았다.
- 비어 있는 home/cache/temp 폴더와 29B test marker로 2.07GB를 회수할 수 없다. 전체 삭제는 다음 fallback 시 재다운로드/초기화 비용 및 네트워크 의존성을 만든다. 승인된 fallback을 제거하는 정책 변경 없이 삭제하지 않는다.

### libvirt VM: 다른 프로젝트의 재사용 가능한 개발 환경

- 소유 프로젝트 `/home/lee/projects/antigravity_for_HA`, checkout 식별자 `4e4fc806baf5`, 전용 helper `tools/development/haos-vm`가 생성한 HAOS 18.2 개발·검증 VM.
- domain `antigravity-haos-4e4fc806baf5`는 persistent/shut off/autostart disabled, 내부 snapshot 없음. 마지막 디스크·NVRAM 사용 흔적은 10/14.
- VM overlay는 virtual 32GiB, actual **20,459,876,352 B**, pinned base 약 1.1GiB를 **backing file로 실제 참조**한다. base를 단순 다운로드 캐시로 보고 제거하면 overlay가 깨진다.
- 현재 등록됐다는 사실만으로 영구 운영 자산이라고 단정하지는 않지만, 이틀 전 사용한 다른 프로젝트 환경을 “미사용 잔여물”로 판정할 근거도 없다. guest 데이터/VM 등록/기본 이미지 삭제 대신 내용 보존형 저장 압축을 검증한다. 실제 회수/비교 결과는 별도 `STORAGE_REVIEW_20261016.md`에 기록한다.

### 추가 용량 개선 과제(정책 변경 전 검증 필요)

Worker sealed DB/vector가 serving volume에 복제되어 차지하는 공간은 최적화 후보다. 그러나 현재 consumer는 Worker root 내 정규 파일·seal을 요구한다. 임의 symlink/경로 변경/삭제/hardlink로 보호 계약을 바꾸지 않는다. native retention 및 content-addressed immutable storage/공유 복사 정책을 설계하고 reader·rollback·hash/no-follow·삭제 순서를 검증한 후에만 제품 변경으로 처리한다. 이번 Reviewer는 사용자 변경이 있는 제품 코드에 저장 구조 변경을 섞지 않았다.

## 6. 검증·다음 제출

- Reviewer 재실행: Executor가 제시한 4개 파일 **292 passed / 21.01초**. 이 결과는 scalar 정책 calibration/실제 production 기능 연결의 증거와 다르다.
- 무네트워크 실제 함수 재현: 모든 입력이 상수 unit-vector일 때 daily smoke passed, 미측정 cross 값 1.0 확인.
- `git diff --check`는 docs/OPERATIONS.md의 EOF blank line 1건을 지적했다. 다음 수정에 함께 정리한다.
- 최종 이미지 배포·full batch·유료 embedding probe는 Reviewer가 반복하지 않았다.
- Executor는 **FIX_03_REPORT.md**를 새로 작성하고 각 항목의 구현/평가/운영 반영 여부를 분리한다. 미수행 calibration을 후속 권고로만 남기고 완료 처리하지 않는다. 기존 PLAN/FIX/REPORT 및 미커밋 사용자 작업은 보존한다.
