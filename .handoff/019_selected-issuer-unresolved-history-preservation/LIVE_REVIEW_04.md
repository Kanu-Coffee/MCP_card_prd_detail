# 019 운영 리뷰 04 — 우리·KB·삼성·BC 및 저장 공간

2026-10-20 Reviewer: Codex. 사용자 요청에 따라 네 실행과 디스크 누적을 조사했다. 판정은 **보완 필요, 019 유지 / FIX_14 발행**이다. 현재 HEAD `aaeb2c4`와 기존 미커밋 변경을 유지했다. PLAN, PLAN_AMENDMENT_01, REPORT, FIX_01–13 및 보고서·기존 리뷰의 계약에 따라 판단했다. 이번 조사에서는 제품 코드 수정·배포·배치 재실행·운영 SQL 변경·파일 삭제를 하지 않았다.

## 실행 결과

공통 이미지 `cardrag-worker:019-e7fb8a0ba60c`, 활성 release `/opt/cardrag/019-e7fb8a0ba60c`. 시간은 컨테이너 시작부터 종료까지이며 pipeline 내부 시간과 다르다.

| 선택 | run_id | 종료 | 경과 | 결과 |
|---|---|---|---|---|
| 우리 | f43e8c37e00f419e984fe15b119fda46 | 0 | 85분 07초 | succeeded, 목록 920, serving 우리 980 / 전체 5537 |
| KB | 14706e77682c437c9604f426f313ceaa | 0 | 100분 00초 | succeeded, 목록 751, serving KB 751→757 / 전체 5543 |
| 삼성 | 44d55df9db8645c8b9060dc4631ff463 | 0 | 87분 14초 | succeeded, 목록 565, serving 삼성 766 / 전체 5543 |
| BC | c975974d00404b0ba74779f9547021ee | 1 | 12분 54초 | 목록·다운로드 완료 후 OCR 재사용에서 failed, 게시하지 않음 |

- 성공 3건의 실행 당시 baseline/자체 seal 대조에서 비선택 문서 변경 0, 비선택 stage 0. 현재 stable pointer는 삼성의 `g-44d55df9db8645c8b9060dc4-a7a46a5bb5e6`다. BC 실패로 전체 목록을 BC로 덮거나 실패 세대를 게시한 증거가 없다. 우리 상세 DB 검증은 LIVE_REVIEW_03도 참조한다. 우리 run 폴더는 이후 정상 retention으로 정리되어 이번에는 보존된 실행 당시 결과를 사용했다.
- 현재 8사 문서 수: 신한 735, 삼성 766, 현대 647, 우리 980, 하나 931, 롯데 605, KB 757, BC 122. 목록 수와 serving 수는 과거 개정 등을 포함하는 기준이 달라 직접 일치하지 않는다.
- 우리와 삼성 pipeline OCR/provider 및 embedding/provider 호출은 각각 0/0. KB는 PDF cache hit 742, 다운로드 6, OCR/provider 6, embedding/provider 8, OCR 재사용 751. 새로 게시된 문서 6건은 선생님든든·E1 LPG·반려애·레고랜드·몰테일·마이펫 관련 문서다. 이것을 원천 사이트의 신규 상품 출시 6건으로 해석하지 않는다.
- 성공 3건의 quality warning은 endpoint 사전 점검의 반복입력 cosine 경고다. KB 최소 0.997586, 삼성 0.997209로 경고선 0.999보다 낮으나 차단선 0.99를 통과했다. BC 사전 점검은 통과했으므로 실패 원인이 아니다. pipeline 호출 0과 별도의 사전 점검 호출을 구분한다.
- 전체 재사용이어도 export는 우리 약 42분 07초, KB 44분 41초, 삼성 40분 59초다. 선택 수집·OCR 비용은 줄지만 전체 corpus 게시 산출물 재생성이 남는다. 네 실행으로 전체 배치 대비 일정 비율의 속도 개선을 증명할 수 없으며, 전면 export 최적화는 이번 FIX 범위가 아니다.

## BC 원인: native/adopted OCR에 content 전용 variant ID 전달

오류 문서 `doc_0d3baaab7cdcdf03c487604146ff3628a021c7cb6f727b66f8f0d37e2b33cf08`, PDF `76ae4c0d7e96fe87669954bf221f0b5b79093b2e05da700293fbc0e6fd0c4f94`(816428 bytes, 2쪽). 이전 삼성/KB seal에는 같은 PDF가 `doc_602d2e8e17dae0e08069a8cbc92564ce494d8bd170056f1624eb5c6311538a70`로 존재한다. 기존 OCR은 native, SHA `c3a3556d7b80a20d69f1cd8ca4e329c73ef4e223e6cb1f509b978d55013eb3f8`, 5208 bytes이며 native manifest 파일이 없다. 실패 run에 materialize된 OCR도 같은 SHA다.

`ocr.py:OCRResolver._lookup_prior_local_sealed_ocr`는 검증된 이전 OCR을 읽은 다음 cache_kind가 native/adopted여도 `cache_variant_id`를 항상 채운다(2094–2111행). `OCRResult.__post_init__`는 content 이외의 variant ID를 금지한다. 실제 운영 이미지에 state/serving을 **읽기 전용 mount, network none**으로 연결하고 동일 prior/source로 함수를 호출했을 때 다음 오류를 재현했다. 출력은 임시 디렉터리뿐이며 provider 호출은 없다.

```text
BC_REPRO ValueError OCR variant ID requires content cache kind
```

따라서 origin 수집 오류·디스크 부족·새 OCR 품질 문제로 진단하지 않는다. 같은 조건의 다른 카드사에도 발생 가능한 공통 경로다. 실패 보고서에는 `ocr_unexpected_error`만 있어 진단에는 실제 경로 재현이 필요했다. seed 직접 조회는 해당 새 document_id가 없어 miss였으며 원인 재현으로 간주하지 않았다.

## 백업: KB 완료, 삼성은 정책/진단 불일치

- 우리: 실행 당시 scope 1906 경로 모두 receipt 일치, remote index 완료는 LIVE_REVIEW_03에서 확인. 현재 과거 run 폴더가 정리되었으므로 이번에 재검증했다고 주장하지 않는다.
- KB: 실행 결과 업로드 3,344,244 bytes, 27 requests, complete=True. 이번에 정식 환경의 현재 root로 읽기 전용 원장과 원격 index(GET only)를 재확인했다. 1563 경로, remaining/lost/conflicts/missing/unbound 모두 0, completion=True.
- 삼성: pending 0, lost/conflicts/missing/remaining 0, unbound만 2. scope 2087 중 2085가 검증된 receipt에 바인딩된다. 추가 2개는 아래 과거 native key의 manifest/READY 경로이며 doc_map 때문에 들어온다.

```text
native key: a7745ec9619c6016f7ddffea3423378c9718b74ff0f6c9aa9be2ac90fc55d206
document: doc_bf6e4d54a4cdca2b87a2eca5c79a90ff609d35abea38349c668801a228261958
current kind: content
current key: 8cf30f1b9062834df6849e4aa46ef50d49e9fb9291ebe698c5dcbceb8b7e3501
current OCR SHA: f1bc998c75a6f48eaee52b8231eb194724e8b2904195e51f5823243d3790a1d9
```

과거 doc_info의 PDF/OCR identity는 현재와 같고 해당 native pair receipt도 있으나 로컬 cache pair는 없다. 현재 seal은 content를 명시하며 run에 문서 소유 native manifest도 없다. `scoped_backup_scope`는 content/adopted shared cache를 문서 백업 단위에서 제외한다고 규정하지만 Phase 2의 doc_map은 이 정책을 적용하지 않고 2개를 다시 required/unbound로 넣는다. 따라서 **신규 미전송 2건이나 OCR 소실로 단정하지 않는다.** 백업 최소화 정책과 과거 태그의 진단 분류를 고쳐야 한다. receipt만으로 완전성/현재 metadata 권위를 부여하거나 현재 native 필수 metadata의 진짜 소실까지 숨겨서는 안 된다.

## 저장 공간

조회 시 루트 filesystem 392GiB 중 약 299GiB 사용, 여유 약 76GiB(80%), inode 사용 약 8%. BC 시작 시 약 78.9GiB가 남았고 capacity 점검 통과, 컨테이너 OOM도 아니다. 단일 시점 조사이므로 시간당 증가율/무한 누적을 입증하지 않는다.

| 사용처 | 관측량 | 해석 |
|---|---:|---|
| Worker state | 약 70GiB | runs 약 39GiB, 배포 복구본 약 20.5GiB, DB 약 6.8GiB, PDF cache 약 4.2GiB 등 |
| serving | 약 32.4GiB | 게시 세대 2개 약 28.1GiB + CAS 약 4.3GiB |
| MCP state | 약 46.4GiB | 게시 세대 3개 약 42.1GiB + CAS 약 4.2GiB |
| Docker images | 43.35GB (decimal) | Docker 표시 reclaimable 19.02GB는 공유층·다른 프로젝트·rollback 구분 전 후보일 뿐 |

Worker는 KB/삼성 성공 run 각 약 17.7GiB, 실패 BC 약 3.6GiB와 구형 실패 run만 보관한다. 성공 retention 2 / incomplete retention 2와 실제 폴더 구성이 일치하고, 우리 run의 정상 정리도 관찰했다. serving은 2개, MCP는 **운영 설정 `CARDRAG_MCP_RETAIN_GENERATIONS=3`**에 맞는 3개 세대다. MCP 3개를 GC 장애로 분류하지 않는다. Worker WAL은 0으로 WAL 누수 증거도 없다.

배포 복구본은 `017-fca5ef313a0b`, `019-4dba44664659`, `019-ba273106158e` 각각 약 6.8GiB 및 `019-e7fb8a0ba60c` 약 27.2MiB다. 마지막 복구본은 이전 DB 복구본을 재사용하는 의존성이 있다. 이 경로는 native run retention 관리 대상이 아니며 무조건 매 배포 DB를 복제하면 증가할 수 있다. 보존 의존성·rollback 단위·인벤토리·수동 정리 기준을 작은 운영 보완으로 마련한다. 현재/직전 정상 복구 가능성을 검증하기 전 삭제하지 않는다.

현재 여유는 있으나 다음 실행 중 run/serving/MCP에 대형 DB·벡터 사본이 동시에 생긴다. 최소 free-space 2GiB 통과만으로 충분한 작업 공간을 보장하지 않는다. 다음 배포에서 불변 DB의 불필요한 약 7GB 추가 복제를 피하고 실행 전 여유와 피크를 다시 기록한다. 전체 출력 형식 변경·전면 GC 재설계는 필요성이 입증되지 않아 범위에서 제외한다.

## 증거·검증 한계

컨테이너 inspect/log, 실행별 live baseline/result, retained seal·성능 보고서, 읽기 전용 SQLite, Docker usage, volume별 du를 대조했다. host 증거는 `/opt/cardrag/current/operations/live/`의 `*.result.json`, `four-runs-local-review.txt`, `bc.failure-evidence.jsonl`, `kb.backup-review.json` 등이다. 재현 도구는 operations의 `review-four-deep.py`, 삼성 대조는 `review-samsung-unbound.py`. 원격 backup index는 KB만 이번에 새로 읽었으며 모든 artifact body를 재다운로드하지 않았다. 소스 변경이 없어 전체 테스트를 반복하지 않았다. BC 실제 수정·새 이미지·재실행 결과는 아직 없고 FIX_14_REPORT 및 후속 리뷰 대상이다. 자동 timer를 재개하지 않는다.
