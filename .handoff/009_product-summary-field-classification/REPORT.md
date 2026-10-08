# 009 Executor REPORT — 요약 필드 분류 및 Worker 부분 실행

- 작성일: 2026-10-08. 역할: Executor.
- 시작 기준: `main`, `dc35984` (009 PLAN의 Worker 부분 실행 추가 개정).
- 검토용 브랜치: `codex/009-summary-stage-reuse`. 이 REPORT 포함 커밋은 해당 브랜치의 Git 로그로 확인한다.
- 결론: 009의 두 구현 범위를 완료하고 로컬 CI와 작은 통합 검증을 통과했다. 운영 배포/정식 릴리스는 수행하지 않았다. Reviewer의 코드·증거 검토 대상으로 제출한다.
- 시작 시 존재한 루트의 우리카드 Excel 3개는 수정·스테이징하지 않았다. 기존 PLAN/변경 전 증거/이전 handoff는 보존했다.

## 1. 원인과 요약 개선

기존 요약은 구조의 `major_class`와 일부 node_type에 의존했다. 이 때문에 MIXED의 제한 면제 혜택은 조건으로, BENEFIT으로 잘못 분류된 법정 변경 고지는 혜택 제목으로 채택됐다. FOOTNOTE/LIST_ITEM의 실제 혜택은 빠졌고 연회비라는 단어를 포함한 제외 항목이 연회비 설명으로 선택됐다. 약관 bundle은 구조를 확장해서 반환하므로 요약의 필드 분류와 달랐다.

`summary_fields.py`에 결정적 분류기를 추가했다. 상품코드 특례/LLM 없이 문장·조상 제목·표 역할을 함께 사용한다. 할인/캐시백의 실제 비율·금액, 서비스 혜택, 제한 면제, 실적·한도·횟수, 법정 고지, 지급 절차와 제외 항목을 구분한다. 면제만 설명하는 문장은 원문을 유지해 condition에 남기며, 조건부 혜택에는 양쪽 필드의 근거를 제공한다. 표 헤더와 월 한도, 무이자 할부 연체금리 안내는 혜택으로 채택하지 않는다. 보험료 납부 제외 목록을 보험 혜택으로 해석하지 않는다.

catalog 조회는 요청한 revisions 안에서만 필요한 노드·parent·table header를 읽는다. 실제 채택한 node/revision/page를 field별 evidence로 반환하고 제목에도 benefit 근거를 붙인다. 5건 상한과 API 필드는 유지한다. 긴 문장은 끝의 부정/상한이 잘릴 위험을 피하려고 해당 필드 한도 안에 전체가 들어오지 않으면 생략한다. 원문을 새 문장으로 생성하지 않는다. 요약 cache의 읽기/쓰기 키 모두에 `cardrag.product-summary.v2`를 추가했다. 출시일 parser 버전은 별도로 유지한다.

### 500107 전후

동일한 불변 generation `g-eba5ca0d13924abdb1f36937-71a5fd98d58b`, revision `revision_b7edb706176f9ddd019c3d5a9c4338e94c36f035d0fb1ea3f41faf5f398ac6b7`에서 비교했다.

| 필드 | 수정 결과 및 원문 근거 |
| --- | --- |
| benefit_headings | 조건, 한도 없이 든든한 1.2% 할인 — p2, node `node_6bb56127eef7a6fa817af2ae5c82dccb76b3729dd1eb40f72a046eb51a3864e3` |
| benefit_summary_texts | 위 문구와 p3의 국내외 1.2%, 국내/해외 가맹점 1.2%, Two-in-One 0.3%, 2~3개월 무이자 할부 원문 |
| condition_summary_texts | 문제의 1.2% 제목은 제외. p3의 ‘전월 실적 조건 없음(할인한도 제한 없음)’은 면제 설명 그대로 유지 |
| annual_fee_text | 본인카드 15,000원(기본 7,000원+제휴 8,000원) — p5, node `node_94c48ab0c82aa570b4ed008c0b059513f42fbb14fed9551e2321df5e1a137e2f` |
| 변경 고지 | 상품 출시일 및 부가서비스 변경 안내/그 아래 법정 문장은 두 benefit 필드에서 제외 |

같은 약관의 운영 `get_contract_bundle(scope=benefits)` 내부 graph selector로 87개 노드를 읽어 요약의 모든 benefit evidence node가 포함됨을 확인했다. 큰 vector 사전 로드를 생략한 read-only 비교이며, 전체 MCP 호출 경로는 별도 작은 fixture의 HTTP 테스트에서 검증했다. 실제 500107을 새 운영 HTTP 서버로 배포해 호출한 결과라고 주장하지 않는다.

## 2. 다른 상품 및 근거 파일

8개 issuer, 기존 30건을 같은 generation/revision으로 재조회했다. 30건의 필드 결과에 차이가 있고, 전후 값과 node/page evidence를 전부 보관했다. 탐색 표본이며 전체 상품 개선율은 아니다.

- 우리 104022: 국내 가맹점 0.8% 할인을 유지하고 월 할인한도·법정 고지·연체 안내를 혜택 제목/문장에서 제외.
- 우리 104023: 생활 5% 캐시백을 유지하고 캐시백 지급 접수 안내·제외·월 한도 제목의 혼입을 해소.
- 신한 00368: 생활쇼핑 기본10%·최대15%와 실제 추가 할인 조건을 보존. 변경 고지/계산 예시를 혜택 제목에서 제외.
- BC 첫 표본: 원화결제차단 신청 절차/부가서비스 고지를 제거하고 실제 에코머니 적립 혜택을 채택. 다른 BC 표본의 기존 3개 benefit 문장이 모두 법정 고지여서 새 필드는 비어 있음; 기존 정상 혜택을 제거한 것으로 계산하지 않는다.
- 하나15911의 캐시백/당구장50% 표, 하나15758의 바우처·공항 라운지, 하나15968의 구독 요금 할인 제목, 롯데1629의 실적별8,000/12,000/16,000원 할인 표를 확인했다.
- 삼성의 보험료 납부 목록, 우리의 정부지원금·바우처 사용 제외 목록을 실제 보험/바우처 혜택과 구분했다.

증거:

- `evidence/current-summary-sample.json`: 기존 Planner가 수집한 변경 전 30건과 500107 원문. 미변경.
- `evidence/implemented-summary-sample.json`: 후보 코드의 동일 DB read-only 결과 및 원문.
- `evidence/summary-comparison.json`: 30건 전후 필드/evidence.
- `evidence/solid-bundle-comparison.json`: 같은 revision의 bundle selector 대응.
- `evidence/partial-execution-benchmark.json`: 아래 작은 Worker 통합 측정.

실데이터 조회는 기존 MCP 볼륨을 read-only mount하고 후보 소스의 `PYTHONPATH`를 지정한 `docker run --rm --network none`으로 수행했다. PDF 수집/OCR/임베딩/외부 API 호출 및 운영 DB 수정은 없다.

## 3. Worker 부분 실행 구현

새 실행계획/출처 검증 모듈 `partial_execution.py`, CLI 연결 `partial_cli.py`를 추가하고 기존 pipeline 단계 경계에 재사용 분기를 넣었다. 전체 pipeline을 대체하지 않았다. 기본 `run`의 예약 배치, 카드사 병렬 수집, 캐시, finite retry, 실패 carry, baseline/단종, atomic 게시 경로는 기존 경로를 사용한다. OCR resolver 생성은 정상/부분 실행이 같은 helper를 사용해 기존 명시적 fallback·호환 cache 설정을 보존한다.

| 옵션 | 동작 |
| --- | --- |
| 반복 `--skip-stage pdf/ocr/structure/embedding/export/webdav` | 지정 단계의 유효한 결과를 재사용하거나 원격 게시 생략 |
| `--skip-pdf`, `--skip-ocr`, `--skip-embedding` | 핵심 단계 별칭. 중복/혼용 정규화 |
| `--reuse-from-run ID` | 완료 source run을 명시. upstream 스킵에서 필수 |
| `--dry-run` | read-only 출처/산출물/현재 embedding cache 검증 및 예상 호출 출력. provider/writer/run 생성/게시/정리/GC 없음 |
| `--publish-channel` | 기본 candidate-009. stable은 기존 승인+현재 stable과 source generation 일치 필요 |

- PDF 스킵: discovery/download/HTTP revalidation 없이 완료 generation의 전체 문서(현재/과거/실패/DRM), 저장 snapshot·seed source identity, PDF CAS를 동결 재사용. 원래 issuer 실패·수집 결과를 유지하고 discovery 성공/단종 grace를 추가하지 않는다.
- OCR 스킵: 같은 PDF SHA의 OCR bytes, page 순서/해시, variant/reuse provenance를 검증. render/provider 호출 없이 사용한다. pending OCR 재처리 요청과 충돌하면 차단하며 완료 receipt를 만들지 않는다.
- structure 스킵: 현재 parser/contract와 input OCR/page SHA가 일치하는 canonical 구조/view만 재사용한다. 실제 구조 재생성은 요청한 단계로 실행되며 `no_change`가 이를 건너뛰지 않는다.
- embedding 스킵: profile namespace/model/dimension과 실제 formatted input SHA의 기존 cache row를 전량 대조. 모델 초기화/probe/호출을 생략하며 누락/변형은 차단한다. source profile이 같을 때 정상 embedding도 cache miss가 생긴 순간에만 credentialed preflight를 수행하도록 지연한다.
- export 스킵: 동일 corpus/contract/document set/OCR/구조/views에 대응하는 완성 seal만 검증 재사용한다. 새 input을 오래된 DB에 합치지 않는다.
- WebDAV 스킵: 게시·OCR cache 업로드·pointer 변경·GC의 원격 쓰기가 모두0. 로컬 결과는 `local_only`, `published=false`, `local_artifacts`; 기존 DB CHECK enum을 바꾸지 않고 run은 `interrupted`+별도 로컬 완료 receipt로 기록한다. `ready_publish`/게시 성공/baseline 갱신으로 오인하지 않는다. 모든 단계 생략이면 `no_op=true`.

파일 없음/해시·계약 불일치/다른 PDF/source 미존재는 `skip_artifact_missing`, `skip_artifact_incompatible`, `skip_source_unavailable`와 단계/문서 식별자로 차단한다. 숨은 유료 fallback, 0벡터, 임의 최신 폴더 선택은 없다. 임의 경로/심볼릭 링크도 거절한다. 작은 OCR/구조 checkpoint만 새 run에 보존하며 DB 전체 clone/writable hardlink를 만들지 않는다.

`execution-plan.json`에 source/channel/skips를 저장하고 `execution-result.json`에 단계 action·문서수·HTTP/provider 호출·소요시간·게시 여부를 남긴다. `run --resume ID`와 `resume ID`는 원래 계획을 자동 복원한다. 옵션/출처/채널을 바꾸면 새 run을 요구한다. 완료한 로컬 seal 재개는 앞단을 다시 실행하지 않는다. legacy `resume-publication`은 부분 실행의 source/channel fence를 우회할 수 없도록 차단한다. source run 상태는 유지하며 retained/resumable 부분 실행의 transitive source 참조를 정상 정리에서 보호한다.

## 4. 실행 예시

```sh
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-pdf --skip-ocr --dry-run
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-pdf --skip-ocr --skip-embedding --skip-stage webdav
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-pdf
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-ocr
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-embedding
cardrag-worker run --resume PARTIAL_RUN_ID
```

이번의 요약 분류는 MCP 코드에서 즉시 작동하므로 Worker 전체 재처리는 불필요하다. 구조를 수정하는 후속 과제에서는 두 번째 예시로 PDF/OCR/vector를 재사용하고 structure/export만 검사할 수 있다. 새 view 텍스트가 바뀌면 embedding skip이 거절되므로 필요한 범위만 정상 임베딩을 실행한다. 실제 운영 source 부분 실행/새 장기 Worker는 이번에 기동하지 않았다.

## 5. 검증 결과

기존 `.venv` 도구를 사용했다. 의존성 추가·설치/전역 설정 변경 없음.

| 검증 | 결과 |
| --- | --- |
| 전체 CI test 경로 `pytest packages/cardrag-core/tests apps/cardrag-worker/tests apps/cardrag-mcp/tests tests/runtime_v1` | **2,381 passed**, 55.33s, 기존 고장 주입/fallback 테스트의 경고9개 |
| 후속 부분 실행·legacy 게시 우회 방지·기존 seal·CLI 회귀 | **158 passed**, 6.41s; 최종 추가 guard 포함 |
| 요약/metadata/catalog 최종 분류 회귀 | **88 passed**, 1.22s |
| MCP 전체 suite 중간 최종 비교 | **811 passed**, 19.99s; 이후 구체적 분류 보완은 위88건 및 전체2,381건에 포함 |
| `ruff check` CI의 core/worker/mcp/runtime_v1/tools 경로 | 통과 |
| `ruff format --check` 같은 경로 | 220 files formatted, 통과 |
| `mypy` core/worker/mcp src | 106 source files, 통과 |
| `git diff --check` | 통과 |
| `gitleaks dir .handoff/009_product-summary-field-classification --no-banner --redact --exit-code 1` | 누출0 (새 보고서 작성 전 증거/PLAN 검사; 배포용 전체 보안 감사라고 주장하지 않음) |
| 운영 MCP ready read-only 확인 | `ready=true`; 운영 컨테이너/코드/volume 교체 없음 |

부분 실행 suite 최종22건은 개별/조합6가지 스킵, structure/export/webdav 개별 경로, source OCR 변형, embedding cache miss, source 경로 오류, source 문서/원문/구조/view-vector 의미 일치, 새 view hash에 대한 refusal, 같은 계획 resume/계획 변경 거절, baseline/단종/source retention, dry-run 무변경 및 provider/writer 무초기화, 실제 CLI 로컬 재사용의 credentialed OCR/embedding 초기화0, legacy 게시 우회 차단을 검증했다. 기본 경로의 lock·신호 중단·parallel issuer·carry·게시 정합성은 기존 전체 회귀로 확인했다.

작은 1문서 fake corpus의 실측은 전체 0.097887s → PDF/OCR/embedding/WebDAV 생략 부분 실행 0.039795s. PDF/OCR/embedding 호출은 각1회 → 각0회이며 구조/export는 실제 실행됐다. 세부 시간/원본 synthetic run ID는 benchmark JSON에 있다. 운영 corpus에서 같은 속도나 고정 완료 시간을 약속하지 않는다.

인증된 작은 HTTP fixture에서는 initialize → `get_product_summary` → 같은 revision `get_contract_bundle(scope=benefits)`를 검증했다. 실제 500107/30건의 후보 결과는 별도 read-only DB 비교다. 벡터 전량 메모리 적재나 운영 HTTP 교체, 유료 OCR/LLM 호출은 하지 않았다.

최초 전체 검사에서 기존 provider-free `SealedPublicationResumer` 및 최소 pipeline fixture가 생성자를 거치지 않아 새 optional 속성이 없는 실패10건이 발견됐다. optional 속성의 클래스 기본값을 추가해 기존 경로를 유지했고 전체 검사를 통과했다. 실제 표본 비교에서도 보험료/바우처 제외·표 헤더·연체 안내의 새 오분류를 발견해 수정하고 회귀 사례를 추가했다.

## 6. 운영 및 검토 범위

운영은 계속 v1.0.33의 기존 image/배치다. 009 후보가 운영에 반영됐다고 보고하지 않는다. main 병합·운영 image 교체·정식 release는 Reviewer 인수 후 별도 진행 대상이다. GitHub 원격 CI 상태와 로컬 CI 결과는 구분한다; 이 REPORT는 위 실제 로컬 실행 결과를 근거로 한다.

요약은 결정적 규칙과 5건/문장 길이 상한을 가진 발췌이므로 모든 상품의 전체 약관을 대체하지 않는다. 장문 생략·보수적 분류의 정상 null은 허용한다. 애매한 모든 상품을 LLM으로 재분류하거나 전량 OCR를 재실행할 필요는 없다. 현재 필수 재현/추가 사례/부분 실행 기능은 검증돼 있으며, 새 개발적 해석으로 추가 이틀짜리 운영 배치 검증을 요구하는 근거는 발견되지 않았다.
