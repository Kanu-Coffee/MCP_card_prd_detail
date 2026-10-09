# 013 FIX_08 운영 배치 완료 검토 — 2026-10-09

Reviewer / Codex. Commit50a0129, run `8adb7603eef640dd9984a93da09b8ab0`.

## 판정

**운영 배치 및 MCP local publication 정상 완료. Worker exit0 / status succeeded / published=true / local_generation_published=true.** 백업은 partial commit 후 예산 종료(degraded)이며 전체 백업 완료로 판정하지 않는다. 기존 인수 판단에 새로운 배포 차단 사유는 없다.

- 최종 컨테이너 기동20:26:36 → 종료22:10:11: **1시간43분35초**. startup 완료20:27:09.914, CLI 본 실행 종료22:10:04.744. pipeline performance elapsed98분22.644초는 startup과 optional backup/CLI 종료를 포함한 전체 컨테이너 시간과 다르다.
- 수집7카드사 성공, 신한 기존 origin network 실패. 신한931건 carry. 신규 상품0, PDF byte revision1, 최종5520문서(현재5060/이력460), missing_unjustified0.
- OCR5519재사용 / provider1 / succeeded5520 / failed0 / structure_failed0. 신규1건은 Hana15911의 변경 PDF로 OpenCode qwen3.8-flash 처리. 기존 Paddle provenance는 과거 결과 표시이며 이번 Paddle 신규 실행을 의미하지 않는다. structure fallback2건은 기존 허용된 unclassified 경로다.
- embedding cache miss58, stage embedding-v5 성공. export616407 embedding views,5520 revisions 전수 검증 로그 통과.
- local publish ready 및 run succeeded 기록21:56:04.7. MCP 새 generation `g-8adb7603eef640dd9984a93d-035c4632e085` activation22:06:52.247. current.json 및 실제 summary 응답에서 동일 generation 확인.
- 실HTTP MCP smoke: 하나15911, 우리500107, KB09063의 get_product_summary 성공. readiness200/ready=true, MCP healthy. 이번 smoke는 응답/generation 확인이며 상품 내용 전체 재평가는 하지 않았다.

## 단계별 시간(KST)

| 단계 | 구간 | 소요 | 근거 |
|---|---|---:|---|
| 기동·provider 사전 검사 |20:26:37~20:27:10|약33초|startup elapsed32.688초|
| 카드사 수집·PDF 확보·수집 barrier |20:27:10~20:34:14|7분04초|수집 barrier 로그|
| 이전 local OCR 연결·처리 준비 |20:34:14~약20:43:58|약9분44초|기존5519자료 발견20:39:04; 후속 OCR 계측 시작 역산|
| OCR 재사용/신규 처리·구조화·views |약20:43:58~21:08:10|24분12초|ocr_and_local_processing1452.127초|
| token/embedding/capacity·export 준비 |21:08:10~21:12:51|4분41초|OCR barrier 및 pre-export 로그|
| DB·벡터 export/내부 검증 |약21:12:51~21:50:09|37분18초|export2238.169초; 시작/끝은 로그 경계에 따른 근사|
| seal 준비·local publication |약21:50:09~21:56:05|약5분56초|seal mtime21:50:45; publish ready21:56:04.684|
| 게시 후 후처리·정리 |21:56:05~22:05:33|약9분28초|run succeeded와 performance.json mtime; 개별 하위 구간 미계측|
| optional backup·CLI 종료 |22:05:33~22:10:11|약4분39초|pipeline 보고서 뒤 backup; timeout22:09:56, commit last_backup22:09:57, CLI 완료22:10:04|

위 구간은 실제 경과시간의 순차 분해다. 일부 시작/끝은 계측값과 로그로 역산했으며 정확한 하위 span이 없는 부분은 근사로 표시했다. stage.discovery469초, structure1564초, views2286초, local_prefetch1157초 등은 병렬/문서별 호출의 누적 시간이다. 이를 더해 batch 전체 시간으로 해석하면 안 된다. stage.ocr298.691초 역시 전체 OCR wall24분12초와 다르다. token_count262.210초, stage.embedding-v5125.381초는 상위 단계 안에 포함된다.

## 백업 및 자원

- optional immediate backup은 시간예산 안에서 일부 index commit 후 degraded로 종료. 마지막 commit22:09:57.796, pending 약1.289GB / OCR 항목5065. 숫자는 객체/backup queue 항목 수이며 신규 OCR5065건이라는 뜻이 아니다. 원격 index·receipts·pending/spool 보존, 자동 다음 inline retry 가능. 별도 background backup은 정지 유지한다.
- final backup_bytes_uploaded2155648404는 이번 commit에서 처리한 바이트로, 실제 신규 PUT 전송량을 입증하지 않는다. backup_requests1051도 helper 호출 계수이며 실제 모든 HTTP 요청 횟수와 동일하지 않다. 실제 업로드 계측 부재를 새 정상 업로드량으로 오인하지 않는다.
- peak RSS8.45GiB, 종료 후 디스크 여유119GiB/사용69%. OOM/강제 종료 없음.
- 성능 개선 우선 후보는 export37분18초, 변경 없는 문서의 구조화/views 재생성, local prefetch 반복 조회 및 게시 후 정리다. 새 OCR1건이 정상인 이번 배치에서 OCR 모델 변경이나 검증 범위 확대를 우선할 근거는 없다. 개별 cleanup/publication span 추가는 추후 성능 과제로 기록하며 이번 운영 인수를 차단하지 않는다.

근거: `evidence/operations-run-20261009-completed.json` (최종 결과·performance·상태·smoke 요약). SQLite 검토는 Worker/backup 정지 후 DB/WAL/SHM의 임시 복사본으로 수행했고 운영 DB를 수정하지 않았다. 컨테이너 재기동이나 백업 재개를 수행하지 않았다. 사용자의 완료 통보 후 필요한 최종 단발 점검만 수행했다.
