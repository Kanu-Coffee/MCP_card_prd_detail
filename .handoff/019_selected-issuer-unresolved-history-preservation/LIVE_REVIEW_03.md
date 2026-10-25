# 019 우리카드 단독 재실행 검토 — 데이터 보존·백업 통과, 품질 경고 및 성능 한계

2026-10-20 Reviewer: Codex. 사용자 수동 배치 완료 후 실제 container/log, 실행 전 baseline과 신규 manifest, 두 serving DB의 상품·계약 행, stage/HTTP 기록, 원장 및 원격 backup index를 읽기 전용으로 대조했다. 기존 handoff는 보존한다.

**판정: 우리카드 배치는 정상 완주했고 비선택 7사 덮어쓰기/재처리 없이 전체 데이터를 보존했다. 선택 백업도 완료 근거를 확인했다. Provider 사전 품질 경고는 남아 있으며, 전체 실행시간은 약6% 줄었지만 export 병목은 개선되지 않았다. 이번 결과만으로 8사 전체 또는 성능 목표 달성을 선언하지 않는다.**

## 실행 및 데이터 보존

- release/image: `019-e7fb8a0ba60c` / `cardrag-worker:019-e7fb8a0ba60c`.
- container `cardrag-live-woori-20261019T233215Z`, exited/exit0, OOMKilled=false.
- 실제 container 시간: 2026-10-20 08:32:16.443–09:57:22.981 KST, **85분6.539초**.
- run `f43e8c37e00f419e984fe15b119fda46`, succeeded/collection complete/published=true.
- run DB finished_at는09:46:39 KST로 후속 단계보다 이르므로 전체 runtime으로 사용하지 않았다.
- generation `g-f43e8c37e00f419e984fe15b-e884484ab66b`.
- frozen scope 및 performance 최상위 issuer_scope 모두 mode=selected, selected=[woori]. monitor metrics.issuer_scope의 null은 해당 키가 metrics 내부가 아닌 최상위에 저장되기 때문이며 실제 all 실행을 뜻하지 않는다.

| 카드사 | 실행 전 | 실행 후 |
|---|---:|---:|
| 우리 | 980 | 980 |
| KB | 751 | 751 |
| 신한 | 931 | 931 |
| 삼성 | 766 | 766 |
| 현대 | 605 | 605 |
| 하나 | 735 | 735 |
| 롯데 | 647 | 647 |
| BC | 122 | 122 |
| 합계 | 5537 | 5537 |

- 실행 전후 serving SQLite에서 `product_lineages JOIN contract_revisions`의 모든 상품·계약 필드를 document_id로 비교했다. **우리 포함 전 카드사 변경0건**이다. manifest의 비선택 document 변경도0건, 새 문서0건이다.
- corpus SHA 및 vector SHA/크기는 동일하다. embedding profile/cache namespace도 동일하다. contract SHA와 embedding policy SHA, DB SHA는 다르므로 “전체 artifact가 바이트 동일”이라고 표현하지 않는다. 새 generation이 게시됐지만 문서가 우리카드로 치환된 것은 아니다.
- 비선택 stage0, HTTP metric issuer도 woori만 존재한다(8 requests/모두200). 나머지7사는 not_scheduled다.
- 우리 discovery/adopted920건, download succeeded916/skipped4. 기존 revision 확장 후 우리980문서를 처리했다. OCR980건 모두 재사용, 본처리 OCR provider 호출0/embedding provider 호출0, 구조 실패0이다. Provider 사전 검증 샘플 호출은 이 본처리 카운터와 별도다.
- unsupported DRM 총8(우리4/KB3/현대1), ZIP unsupported0, OCR failed products0으로 이전과 같다. 기존 structure fallback2개도 문서/사유/hash가 같다. 과거 unresolved ledger/inventory0을 유지했다.

## 품질 경고 — 실행 실패와 구분

이번 `quality_status=warning`은 새 provider 사전 반복 입력 검증 경고다. 이전 run의 passed를 그대로 계승한 표시가 아니다.

- provider deepinfra, sample24 중2개가 warning 기준0.999 미만.
- minimum cosine0.9972087003, mean0.9997831114.
- 실제 실행 차단 gate0.99는 통과했다. threshold를 낮추거나 경고를 지우지 않았다.
- 로그의 추가 경고는 DRM 문서 건이며, serving unsupported8건은 위와 같이 기존 수치다. Traceback/실패 종료는 없었다.

기존 cache vector bytes는 동일하지만 provider 사전 샘플의 변동 경고는 다음 카드사 실행에서도 관찰해야 한다. 이 결과를 provider 품질 완전 무경고로 보고하지 않는다.

## 백업 deferred는 새 미완료가 아님

종료 결과는 backup_status=deferred, pending0/bytes0, 모든 scope 미완료 카운트0, scope_complete=null(read_only)다. 즉 새 업로드 대상이 없어 trigger가 발생하지 않은 상태다. 정책을 우회하여 force flush하거나 `runs_since_last_backup=1`을 실패로 해석하지 않았다.

별도 읽기 전용 검증으로 실제 canonical root의 scope1906경로를 계산하고, receipt와 **현재 원격 committed backup index를 GET 대조**했다.

```text
expected paths=1906
remaining=0, lost_source=0, conflicts=0, missing=0, unbound=0
backup_scope_complete_from_receipts_and_remote_index=true
global pending/lost=0, should_trigger=false
```

원격 artifact를 1906건 모두 재다운로드한 full audit는 아니다. 이번에 새 본문/metadata가 없으며, 기존 receipt 및 committed index의 완결성을 확인했다. 직전 운영 테스트의 A/B 실제 바이트 검증 근거는 OPERATIONS_REPORT_03에 있다. 종료 당시 cached JSON은 rewrite하지 않고 현재 검증 결과를 별도 파일로 보존했다.

## 실행시간과 병목

직전 같은 우리 실행 `41ce678e...`와 비교한다. 직전에는 신규 OCR1/embedding 호출1이 있었고 이번에는 둘 다0이므로 통제된 동일 입력 성능 실험은 아니다.

| 항목 | 직전 우리 | 이번 우리 | 변화 |
|---|---:|---:|---|
| container wall | 90분33.629초 | 85분6.539초 | 5분27.090초 단축, 약6.02% |
| performance elapsed | 5397.323초 | 5079.888초 | 약5.88% 단축 |
| export | 2493.494초 | 2526.805초 | 33.311초 증가, 약1.34% |
| OCR/local processing | 361.798초 | 283.008초 | 감소 |
| export DB fetch | 619134 | 493214 | 125920회 감소, 약20.34% |

bounded memo hit125920과 실제 fetch 감소는 관측됐다. 그러나 export는 **42분6.805초**, performance elapsed의 약49.74%로 여전히 지배 비용이다. query 감소를 전체 export 속도 개선으로 해석하지 않는다.

새 내부 계측의 주요 구간:

- SQLite rows564.526초, vector sidecar531.934초.
- working verify361.465초, final verify517.534초.
- cache loader250.571초, vector encode254.510초, FTS13.654초, VACUUM34.230초, install66.292초.

loader/encode/digest는 sidecar에 중첩될 수 있으므로 위 값을 모두 더해 wall time을 만들지 않는다. 검증/SQLite/sidecar의 공통 비용이 커졌다는 관측은 있으나, 이번 계측만으로 특정 함수 한 개에42분을 귀속시키지 않는다. peak RSS 약10.99GiB, process physical read 약302GB/write 약39.8GB도 기록돼 있다.

같은 corpus인데도 contract SHA가 바뀌어 기존 코드의 exact no-change 조건(corpus+contract 일치)을 충족하지 않았다. source상 endpoint metadata fingerprint가 contract/embedding policy에 포함되며 profile identity는 그대로지만, 이번 두 run의 원본 endpoint snapshot을 모두 확보해 상세 변경 원인을 확정한 것은 아니다. fingerprint를 임의로 제외하거나 integrity 검증을 생략해 재게시 비용을 줄이는 수정은 하지 않았다. 향후 성능 작업에서는 semantic contract 변경과 관측 metadata 변경을 구분하고 위 검증/IO 비용을 함께 살펴야 한다.

## 증거와 다음 단계

host `/opt/cardrag/current/operations/live/`:

- `woori.result.json`: 종료 snapshot, `woori.execution-result.json`: 로그의 최종 원본 payload.
- `woori.serving-review.json`: 두 실제 DB의 행/수치/hash·HTTP/stage/performance 비교.
- `woori.backup-review.json`: 현재 원장+원격 index 완료 대조.
- `woori.contract-review.json`, `woori.profile-review.json`: profile/policy 비교 근거.

본 리뷰는 코드 수정·새 배포·원격 쓰기/flush·수집 재실행·orphan 삭제를 하지 않았다. 실행 중 Worker 없음, timer/service inactive를 유지한다. 기존 테스트를 런타임 검증 대신 반복 실행하지 않았다.

우리 단독 실행의 데이터/백업 검증은 통과했으므로 다음7사 순차 검증으로 진행할 수 있다. Provider 사전 경고와 시간/IO를 계속 기록하고, 전체8사 평가 및 자동 round-robin/timer는 기존 계획대로 마지막에 검증한다. 큰 폭의 성능 개선이나8사 전체 완료를 이번 결과로 선언하지 않는다. 새 기능적 correction을 요구할 결함은 이번 검토에서 확인되지 않아 FIX를 추가 발행하지 않는다.
