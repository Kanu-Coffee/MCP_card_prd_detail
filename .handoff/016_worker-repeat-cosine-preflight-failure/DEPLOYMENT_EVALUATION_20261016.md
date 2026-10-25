# 016 운영 평가 배포 — 사용자 승인에 따른 신규 정책 적용

- 작성: 2026-10-16 12:55 KST, Codex.
- 승인: 사용자가 “모두 운영적용하고 반영해. 어차피 품질기준이니 운영테스트로 하면서 평가할거임”이라고 명시 지시했다. 이전 Reviewer 문서의 calibration 전 배포 보류 조건은 이 최신 지시로 변경한다. 기존 handoff는 역사로 보존한다.
- 범위: FIX_02–04의 검토된 구현 전체, 운영 문서·이력, 현재/예약 Worker 이미지 적용. 새 정책을 운영 평가 대상으로 적용하며 gold/search calibration 완료로 표현하지 않는다.

## 1. 반영 결과

- 구현 및 기존 미커밋 handoff 커밋: **`d8a9018`**, `feat(worker): deploy reviewed daily embedding checks for operational evaluation`.
- 신규 이미지: **`cardrag-worker:016-d8a9018`**.
- 이미지 ID: **`sha256:6aab446cfce359fe25a69ecab99537140961d0d4cfdc8b6048aeaecc0b74d1db`**.
- OCI revision은 커밋 full SHA와 일치. 설치된 Worker/Core Python 파일 **73개 전부** 현재 저장소 SHA256와 일치한다.
- 운영 `/opt/cardrag/current/deploy/worker/compose.secrets.yaml` image 변경, 실제 운영 wrapper의 `config --quiet` 통과. `/opt/cardrag/current/docs/OPERATIONS.md`도 최신 문서 반영.
- timer는 active, 다음 **10/17 03:00 KST** 예약도 신규 이미지를 참조한다.
- rollback 이미지 `016-bb56d20` 및 기존 상태/서빙/Paddle/auth 볼륨 보존. 별도 데이터 삭제 없음.

### 빌드 방식

검증된 기존 `016-bb56d20` 이미지 ID를 확인한 뒤 해당 로컬 런타임/의존성 레이어를 재사용했다. Git 비교에서 runtime source 변경이 cli.py/embedding_v5.py 두 파일뿐이고 dependency/lock/Dockerfile/Core/MCP source 변경이 없음을 확인했다. 변경된 설치 소스 두 파일과 운영 문서를 COPY한 새 이미지를 네트워크 없이 생성했다. 전체 설치 소스 hash 대조로 최신 구현 반영을 검증했다. base 이미지와 새 이미지 모두 보존한다. MCP 자체 코드는 변경되지 않아 MCP 재배포는 필요하지 않았다.

## 2. 실제 운영 기동 검증

- 운영 wrapper와 동일 secret/볼륨/환경/lock으로 명명 컨테이너 **`cardrag-eval-016-d8a9018`**의 full `worker run` 시작.
- 시작 12:52:36 KST, startup 완료 12:52:52 KST, correlation **`ad26fd846d1c6fb4648fe01beb551bd6`**.
- minimum start capacity 통과, 당시 free **124,553,285,632 bytes**, minimum **2,147,483,648 bytes**.
- OCR resolver(Codex 0.162.1), tokenizer, 선택 provider DeepInfra, semantic 검사 및 embedding verification 통과.
- 실제 반복 cosine **minimum=0.9972087002816739**, mean=0.9998836958450698, **1/24** 샘플 경고. `quality_status=warning`, `reason_code=repeat_cosine_warning`으로 **기록 후 pipeline 진입 성공**. 기존 0.999 단독 차단과 달라진 동작을 운영에서 확인했다.
- sample index/hash 및 0.990/0.999 thresholds가 `startup-attempt-history.jsonl`에 보존됨을 직접 read-only 확인. mode=daily_smoke, provider_count=1, cross_evaluated=false, minimum cross=null.
- 스냅샷 시 삼성565/KB751/하나724/BC122 records 수집 완료, 신한 discovery의 `issuer_origin_network` 재관측. MCP `/health/ready`는 ready=true.

systemd service 직접 수동 시작은 현재 세션 권한에서 거부돼, 기존 운영 wrapper의 명명 컨테이너 실행 절차를 사용했다. global 권한/서비스 정의를 변경하지 않았다. 따라서 service에 남은 오늘 03시 실패 상태와 이번 수동 평가 컨테이너 실행은 별개다. 예약 서비스가 참조하는 운영 Compose 이미지 변경은 완료했다.

## 3. 현재 완료 범위 및 계속 평가할 항목

**배포·예약 설정 반영·실제 startup 검증 완료. 기록 시 full batch는 실행 중**이다. full batch exit/publication/새 OCR/신규 상품/증분백업 완료를 아직 주장하지 않는다. 운영 평가 실행은 중단하지 않으며 종료 시 로그/컨테이너 결과를 수집하는 collector를 시작했다. 컨테이너는 증거 보존 전 자동 제거하지 않는다.

증거: `/opt/cardrag/016-evidence/operations/deployment-016-d8a9018/`.

- `deployment-baseline.json`, `build-context/Dockerfile`, `image-verification.json`, `all-installed-source-hashes.json`.
- `compose.secrets.before.yaml`: 운영 이미지 rollback용 원본(비공개 경로, 공개 저장소에 포함하지 않음).
- `startup-history.json`, `runtime-snapshot.json`, `batch-progress.log`: 이번 실기동 증거.
- collector가 배치 종료 후 `batch.log`, `batch-container-result.json`을 기록한다. 이 두 결과를 읽어 최종 JSON의 품질/수집/게시/백업 상태를 별도로 판정해야 한다.

정책 평가에서는 warning 발생 수·최소/평균/실패 샘플·provider metadata hash·기동 성공/실패와 검색 결과 변화를 함께 본다. 필요 시 gold/old-cache anchor 평가를 보완한다. 네트워크 장애가 있는 롯데 fresh 목록/신한 수집 복구는 이 품질 정책 적용만으로 해결됐다고 표현하지 않는다.

직전 Reviewer 독립 검증은 **1313 passed, 10 warnings**, ruff check/format 및 diff check 통과였다. 이번 추가 제품 코드 변경은 없고 운영 승인에 맞춘 문서 변경만 추가했다. 원격 push는 하지 않았다.
