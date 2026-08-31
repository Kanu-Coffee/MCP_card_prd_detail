# CardRAG v1.0.10 OCR provider 종료와 v1.0.11 조치

## 판정

2026-08-31 12:05 KST, container
`cardrag-v110-candidate-worker-862b989`의
`cardrag-worker resume 1f1763a9cd474a81952a6eb6ffb6e397`가 exit 1로 종료했습니다.
Docker `OOMKilled=false`였고 host kernel/OOM 증거도 없었습니다. Worker state의
`ocr-systemic-failure.json`은 Samsung document
`doc_3282a4...`에서 `reason_code=provider_process_exit`, inner Codex exit code 1을
기록했습니다.

이 실행은 `candidate-v1.0.10` one-shot Worker였고 stable publisher가 아닙니다.
publish table은 0건이므로 새 generation이나 channel pointer가 게시되지 않았습니다.
현재 v1.0.9 MCP와 stable serving data에는 이 실패로 인한 변경이 없습니다.

## 확인된 원인 경계

- 실패 image는 application 1.0.10, revision
  `862b9890aeb63dde9ead86954d6e7da6494cc33f`, Codex CLI 0.147.0입니다.
- PDF는 8 page이고 앞선 OCR chunk 0~2는 local checkpoint에 봉인됐습니다. 마지막
  chunk 직전에 Codex child process가 nonzero로 종료했습니다.
- 당시 adapter는 stderr를 수집한 뒤 폐기하고 모든 nonzero exit를 하나의
  non-retryable `provider_process_exit`로 바꿨습니다. 따라서 인증·설정·rate limit·일시
  provider/network 오류를 구분하거나 안전하게 재시도할 수 없었습니다.
- Codex auth volume의 `login status`는 성공했고 token 유효기간도 남아 있었습니다.
  이는 auth가 절대 원인이 아님을 증명하지는 않지만, 만료된 login이 직접 원인이라는
  증거는 없습니다.
- v1.0.10 candidate acceptance receipt와 필수 runtime evidence가 없고 annotated
  `v1.0.10` tag도 없습니다. 따라서 이 image는 승인된 운영 release가 아닙니다.

원 stderr가 의도적으로 보존되지 않았으므로 특정 upstream 오류 문자열까지 사후
복원할 수 없습니다. 직접 원인은 “Codex child nonzero exit”이며, 세부 원인은 관측성
부족 때문에 미확정입니다.

## v1.0.11 조치

1. Codex CLI를 공식 0.151.0 x86_64 musl asset으로 올리고 SHA-256
   `605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6`을 Dockerfile,
   SLSA provenance validator와 tests에 고정합니다.
2. stderr 원문·URL·credential은 report와 exception에 넣지 않습니다. 전체 byte length와
   SHA-256 fingerprint, allowlisted reason/error kind만 기록합니다.
3. authentication/configuration과 unknown exit는 non-retryable systemic failure로
   유지합니다. rate limit, transient network와 provider unavailable만 기존 stage
   max-attempt budget 안에서 재시도합니다. 무한 재시도나 새 전역 retry loop는 없습니다.
4. `1.0.11`, `candidate-v1.0.11`, `cardrag-v111-candidate`와 v111 state/auth/MCP volume,
   port 18011을 release receipt schema와 Compose에 함께 결속합니다.
5. Release workflow는 1.0.11 외 version을 strong evidence block 전에 거부합니다.
   `release-evidence/v1.0.11/**` 이외 evidence-only diff도 거부합니다.

## cache와 운영 데이터

v1.0.10 실패 state의 성공 checkpoint, PDF CAS와 OCR seal은 삭제 대상이 아닙니다.
v1.0.11은 별도 volume/namespace를 사용하고, recovery에 필요한 경우 terminal·WAL 상태를
검증한 read-only source에서 v111 destination으로 snapshot-copy한 뒤 전건 hash를
대조합니다. 공용 native OCR cache는 candidate에서 read-only이며 stable pointer와 remote
GC approval은 계속 분리됩니다.

## 현재 배포 gate

코드 변경만으로 incident가 종결되지는 않습니다. v1.0.11 exact candidate image의
Codex sandbox/login/provider smoke, 실패 chunk 재실행, full four-issuer zero-defect run,
MCP 8-tool/readiness/rollback, native cache zero-write audit와 canonical acceptance receipt가
모두 통과하기 전 상태는 **candidate gate 미통과 / stable 배포 금지**입니다. 자세한
절차는 [v1.0.11 migration](V1_0_11_MIGRATION.md)을 따릅니다.
