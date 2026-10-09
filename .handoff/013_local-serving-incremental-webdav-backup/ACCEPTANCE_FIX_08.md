# 013 FIX_08 최종 구현 인수 검토

작성: 2026-10-09, Reviewer / Codex. 기준 commit `50a0129dcc210be06e49e7b5389e9e49383046f1`.

## 판정

**FIX_05~08 보완 구현 인수 가능. 추가 FIX 없이 수정본 운영 반영 및 Worker 재기동 단계로 진행할 수 있다.**

이번 판정은 이전 운영 배치에서 발견된 local content OCR 재사용 회귀와 그 보완의 코드 인수다. 기존 ACCEPTANCE.md와 후속 FIX/REPORT/운영 기록은 보존한다. 새 이미지 배포·새 운영 배치 완료·실제 원격 백업 완료·공개 release는 별도 상태이며 이번 리뷰에서 수행하지 않았다.

## 독립 검증

- 지정5파일 pytest: **53 passed, 1 warning / 3.29s**. warning은 손상된 OCR fixture의 의도된 거부 경로다.
- 수정4파일 mypy: **Success**.
- 기존 reviewer-fix06-repro.py: provider0/cache hit, 원variant/Paddle provider/model 보존, 동일 입력 반복variant 안정, epoch1에서 epoch0 miss 확인.
- 실제 영향 대상 전수: 구 seed에 없는 **328건(323content + 5native)**을 실제 OCRResolver로 실행. **328 cache hits / provider calls0 / 원OCR SHA328 / 원variant328 / exceptions0**.
- native5건: prior.provider/model을 미리 채우지 않은 실제 pipeline 형태의 입력에서 **opencode / alibaba-token-plan/qwen3.8-flash** 반환을 엄격 assert하여5/5 통과했다.

운영 volume은 read-only, Docker network none, 출력/state는 임시 디렉터리로 격리했다. 검증 컨테이너 UID0은 호스트 소스 읽기 권한을 위한 것이며 운영 volume에 쓰지 않았다. 이전 runtime의 라이브러리와 현재 Git 소스를 사용했다. 운영 Worker 또는 유료 provider를 구동하지 않았다.

근거: `evidence/reviewer-fix08-independent.json`, 재현 script `evidence/reviewer-fix07-actual-inventory.py`. Executor의 신규 `executor-fix08-verification.py/json` 및 FIX_08_REPORT도 검토했다. 이전 잘못된 앞쪽323 표본은 이번 실제328 근거로 대체하며 기존 증거 파일은 보존한다.

Executor 제출 전체 Worker/MCP suite:2146 passed/10 warnings/0 deselected. 독립 핵심 gate가 통과했고 전체 gate도 제출되어 전체 suite를 불필요하게 재실행하지 않았다.

## 확인한 수정

native manifest의 contract를 정식 OCRArtifactManifest로 읽고 canonical/source PDF/OCR identity가 일치할 때 provider/model을 복원한다. 이미 명시된 prior 값은 유지한다. legacy dictionary fallback도 PDF/OCR identity를 확인하며, 정보가 없는 content 자료를 특정 모델의 출력으로 재표기하지 않는다.

FIX_07에서 통과한 원variant 보존, epoch 경계, 부분 backup commit reserve, commit 실패 후 receipt 재사용, actual 전송량 미측정(None) 표시는 그대로 유지한다. helper requests를 실제 HTTP 전체 횟수로 확대 해석하지 않는다. 30초 item cap과 작은 예산에서 commit 최소10초 허용은 기존에 기록한 운영 제한이며 이번 인수에 새 개발 조건으로 추가하지 않는다.

## 운영 후속

1. 현재 운영 MCP는 `cardrag-mcp:013-3eaa35d`이고 healthy다. Worker와 backup은 정지 상태다. 수정 commit의 이미지로 새 배포를 준비하며 기존 secrets, state/auth/Paddle 및 cardrag-serving 볼륨을 유지한다.
2. 기존 local publication/옵션 증분 backup 설정을 유지하고 Worker와 background backup의 경합을 피하도록 실행한다. 기존 pending/spool/receipts/원격 backup을 삭제하지 않는다. 사용자 기존 운영 반영·기동 승인은 유지되므로 같은 승인을 다시 요청하지 않는다.
3. 불필요한 재OCR로 중단한 run29f7877bcc2e4e13b19447f023975e16의 자료/체크포인트를 보존한다. 재실행 시 기존 sealed authoritative OCR을 재사용한다. 구 remote cache 의존성을 되살리지 않는다.
4. Worker 기동 시 startup 완료 및 최초 진행만 확인하고 턴을 종료한다. 사용자가 완료를 알려준 뒤 결과 JSON, cache reuse/provider 신규 호출 사유, local generation 활성화와 summary/bundle/PDF, 백업 commit/pending 및 잔여 용량을 검증한다.
5. provider0은 이번328개 기존 자료에 대한 실증이다. 실제 신규/변경 PDF 또는 명시적 재처리에는 정당한 OCR 호출이 발생할 수 있으며 향후 전체 batch의 호출0을 보장한다고 보고하지 않는다.

이번 Reviewer는 운영 설정/데이터/컨테이너/타이머를 변경하지 않았고 Worker를 재기동하지 않았다. 새 인수 문서와 독립 검증 결과만 추가했다.
