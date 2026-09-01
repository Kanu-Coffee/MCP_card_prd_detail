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

## v1.0.11 candidate capacity 종료

2026-09-01 23:46 KST, exact source revision
`6b28b5a279b4cd61e0822e4bf73856984b9b0c29`의 Worker container
`cardrag-v111-candidate-worker-6b28b5a`가 exit 1로 종료했습니다. Docker
`OOMKilled=false`였고 run `1f1763a9cd474a81952a6eb6ffb6e397`의 terminal error는
`predicted v5 serving database exceeds the configured file limit`입니다.

네 카드사의 OCR, structure와 derived-view stage는 3,085건 모두 성공했습니다. 실패는
315,067개 view의 첫 embedding miss를 다운로드하기 전 capacity preflight에서 발생했으며,
`embedding_cache_v5`와 publish row는 각각 0건이고 sealed DB/vector/pointer도 없습니다.
따라서 유료 embedding 호출이나 candidate/stable publication은 발생하지 않았습니다.

Read-only artifact reconstruction은 다음 exact prediction input과 결과를 확인했습니다.

| 항목 | bytes/count |
|---|---:|
| database payload | 1,876,077,491 B |
| database rows | 6,692,163 |
| FTS indexed text | 171,794,956 B |
| secondary-index text | 444,565,586 B |
| predicted serving DB | 8,148,455,424 B (7.589 GiB) |
| predicted vector sidecar | 5,162,057,728 B (4.808 GiB) |
| predicted logical growth, WAL baseline 제외 | 27,855,376,384 B |
| predicted peak growth, WAL baseline 제외 | 52,300,742,656 B |

기존 4 GiB serving DB 한도는 실제 four-issuer corpus보다 작았습니다. 향후 카드사 네 곳이
현재 네 곳과 같은 규모·중복률이라고 가정해 exact input을 두 배로 재계산하면 DB 예측은
16,295,858,176 B(15.177 GiB), sidecar는
10,324,115,456 B(9.615 GiB), DB·sidecar·unique PDF의 generation download는
32,183,169,962 B(29.973 GiB)입니다. 이 값은 16 GiB DB/32 GiB download/64 GiB state에
운영 여유를 거의 남기지 않습니다.

따라서 v1.0.11은 Worker와 MCP의 단일 serving DB 한도를 32 GiB(`34359738368`)로,
Worker/MCP state를 128 GiB(`137438953472`)로, MCP 단일 generation download를
64 GiB(`68719476736`)로 올립니다. Candidate Compose와 acceptance receipt는 이 exact
literal을 결속해 ambient override를 거부합니다. 16 GiB sidecar, 32 GiB candidate startup
floor와 2 GiB reserve는 현재 four-issuer acceptance를 위해 유지합니다. 이 8개 카드사
2배 projection의 peak는 104,588,886,016 B(97.406 GiB), reserve 포함 필요 free-space는
106,736,369,664 B(99.406 GiB)입니다. 2026-09-02 capacity 조사 시 Docker backing
filesystem의 82,115,493,888 B(76.48 GiB) free-space로는 약 22.93 GiB 부족했습니다.
실제 추가 카드사의 문서량·중복률에 따라 더 커질 수 있으므로 확장 실행 전에 storage를
별도로 provision하고 최신 `df`와 corpus-derived preflight를 다시 통과해야 합니다.
Worker/MCP named volume이 같은 backing filesystem을 쓰면 물리
free-space도 공유됩니다. 8개 카드사의 Worker state와 MCP retention/staging을 합친 순간
사용량은 약 150.84 GiB까지 예상됩니다. 현재 host의 다른 사용량과 두 서비스의 reserve를
합치면 256 GiB도 부족하므로 최소 320 GiB급 shared backing filesystem 또는 각 요구량을
독립 충족하는 별도 filesystem을 권장합니다. 실행 전에는 당시 host baseline으로 다시
산정해야 합니다. 기존
실패 container와 volume은 forensic/resume boundary로 보존하고, 수정 source로 새 exact
Worker/MCP image를 만든 뒤 같은 terminal run을 명시적으로 resume해야 합니다.

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
