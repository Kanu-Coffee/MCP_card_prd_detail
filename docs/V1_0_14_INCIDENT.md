# CardRAG v1.0.13 remote publication incident

## 결론

v1.0.13 candidate Worker는 embedding, local vector/DB 생성과 `publish.json` local seal을
완료했지만, WebDAV immutable DB의 업로드 후 검증 단계에서 컨테이너의 2 GiB `/tmp`
tmpfs를 초과했습니다. 검증 구현이 방금 업로드한 2,647,711,744-byte DB를 임시 파일로
다시 내려받았기 때문에 `ENOSPC`가 발생했고, WebDAV `MOVE`와 generation activation 전에
종료되었습니다.

이 사고는 OOM, `SIGBUS`, kernel I/O fault 또는 embedding 실패가 아닙니다. 고비용
embedding cache와 완성된 local DB/vector/seal은 원본 v1.0.13 volume에 보존되어 있으며,
v1.0.14는 그 원본을 변경하지 않고 새 전용 volume에 offline copy한 뒤 동일 run을
`resume-publication`으로 게시 단계만 resume합니다.

## 10:54 KST read-only 관측

| 항목 | 확인값 |
|---|---|
| container | `cardrag-v113-candidate-worker-acceptance` |
| application version | `1.0.13` |
| source revision | `03a24f5e549e5466dfe99db61e9ebbf6b58f8410` |
| exact OCI index | `sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9` |
| state | `exited` |
| exit | `1` |
| terminal timestamp | 2026-09-03 09:57:42 KST |
| OOMKilled | `false` |
| restart count / policy | `0` / `no` |
| `/tmp` mount | tmpfs, `rw,noexec,nosuid,size=2g` |
| run ID | `1f1763a9cd474a81952a6eb6ffb6e397` |
| generation ID | `g-1f1763a9cd474a81952a6eb6-2405a03c6f8e` |
| local seal completion | 2026-09-03 09:14:16 KST |

Container, volume과 WebDAV에 대한 점검은 read-only였습니다. 원본 container는 재시작하지
않았고 source volume에 checkpoint, cleanup 또는 write를 수행하지 않았습니다.

완료된 local artifact는 다음과 같습니다.

- serving DB: 2,647,711,744 bytes, SHA-256
  `ce78fe2cf4cedf58987d9403e087f8611b38086f52de0eb3528f907585d0aeab`;
- vector sidecar: 5,234,016,256 bytes, SHA-256
  `410c7ec0753d8d9e32f4661d7bd0cf2745c9de21719076ac970a9117cdcbaf4e`;
- vector shape: 319,459 rows x 4,096 dimensions;
- embedding cache v5: 225,001 rows;
- sealed object inventory: expected 6,276, actual 6,276, missing/size/hash mismatch 0.

v1.0.13 exact image로 local seal 전수를 다시 검증한 결과는 331.183초에 통과했습니다.
DB file hash, SQLite integrity/foreign-key 검증, 모든 sealed object의 SHA-256, vector 전체
319,459 x 4,096 값의 finite 검사와 L2 norm 검사를 포함합니다. 따라서 local seal의 손상은
종료 원인이 아닙니다.

같은 시점의 WebDAV read-only `HEAD`에서는 이 generation의 DB, vector, manifest,
`READY`와 candidate pointer가 모두 `404`였습니다. 즉 게시 완료나 candidate activation은
발생하지 않았습니다.

## 직접 원인

v1.0.13의 immutable publication은 다음 순서였습니다.

1. local sealed artifact를 검증한다.
2. WebDAV incoming 경로에 DB를 업로드한다.
3. `CASPublisher._verify_remote()`가 `tempfile.TemporaryDirectory()`를 만든다.
4. `WebDAVClient.download()`로 remote DB 전체를 그 임시 디렉터리에 내려받는다.
5. 내려받은 파일의 size와 SHA-256이 일치하면 incoming object를 최종 경로로 `MOVE`한다.

`TemporaryDirectory()`에는 별도 `dir` 인자가 없어서 컨테이너의 `/tmp`를 사용했습니다.
해당 mount의 한도는 2 GiB(2,147,483,648 bytes)인데 DB는 2,647,711,744 bytes였습니다.
필요 공간이 한도를 정확히 500,228,096 bytes 초과하므로 full-file readback은 반드시
`ENOSPC`로 실패합니다. 실패는 final `MOVE` 전이었고 cleanup이 incoming object를
제거했으므로 generation과 pointer가 모두 `404`인 원격 상태와도 일치합니다.

## 관측성 및 잠복 결함

직접 원인 외에 두 결함을 함께 확인했습니다.

- `_publish_sealed()`가 remote publication 예외를 포괄적으로 잡은 뒤 원래 분류와 errno를
  버리고 일반 `RuntimeError`로 바꿨습니다. 그 결과 terminal record에는
  `worker_unexpected_failure`/`runtime`만 남고 실제 `ENOSPC`가 보존되지 않았습니다.
- `validated_current_generation()`도 current DB, vector와 모든 CAS object를 기본 `/tmp`의
  임시 디렉터리로 내려받았습니다. 이번에는 candidate pointer가 생성되기 전에 종료되어
  실행되지 않았지만, current generation이 존재하면 같은 용량 결함이 재발할 수 있는
  잠복 경로였습니다.

또한 새 seal은 remote publication 진입 전과 진입 후 중복 검증되었고 resume 경로에서는
최대 세 번 검증될 수 있었습니다. 5.234 GB vector의 전체 Python scan 때문에 seal부터
종료까지 불필요한 지연이 커졌습니다. 이것은 `ENOSPC`의 원인은 아니지만 복구 시간과
운영 관측을 악화시킨 요인입니다.

첫 v1.0.14 격리 복구 리허설은 source `6ea33ed4f71526763a351eddf573f5b686afef43`
이미지에서 일반 `resume`을 사용했습니다. 이 경로가 live endpoint metadata preflight와
issuer discovery를 다시 수행해 3,094개 source를 열거하고 OCR cache 순회로 재진입한 것이
로그에서 확인되어 즉시 `SIGTERM`으로 중단했습니다. 당시 seal contract와 live contract가
달라진 직접 메타데이터 snapshot을 함께 봉인하지는 않았으므로, endpoint metadata drift에
의한 contract mismatch라는 설명은 이 실행 결과와 코드 경로에 근거한 추론입니다. 중단된
리허설의 v114 container/volume만 제거했으며 v1.0.13 원본과 WebDAV stable 채널은 변경하지
않았습니다. 이 리허설 receipt는 아래 최종 복구 증거로 재사용하지 않습니다.

## v1.0.14 수정과 회귀 방지

v1.0.14는 data/publication channel `candidate-v1.0.11`을 바꾸지 않는 runtime reliability
patch이며 다음을 적용합니다.

- WebDAV GET response를 고정 크기 chunk로 읽으며 SHA-256과 byte count를 동시에 계산하는
  constant-space remote verification을 추가합니다. DB/vector/CAS 검증은 local tempfile을
  만들지 않습니다.
- immutable publication과 current-generation validation을 모두 같은 streaming verifier로
  전환합니다.
- 검증을 마친 seal에 canonical digest를 결속한 validated-seal 값을 publication에 넘겨,
  동일 bytes일 때만 결과를 재사용합니다. 새 run과 resume 모두 한 번의 full local seal
  validation만 수행합니다.
- remote publication 실패의 안전한 `phase`, 분류와 `errno`를 terminal failure record에
  전달합니다. URL, credential, response body나 원본 예외 문자열은 기록하지 않습니다.
  같은 사고라면 `phase=remote_publication`, local-I/O 분류와 `errno=28`로 식별할 수
  있습니다.
- 작은 tmpfs에서 legacy download 경로가 호출되면 강제로 실패하도록 한 회귀 시험,
  streamed size/SHA mismatch 시험, current-generation tempfile 금지 시험과 seal 검증
  단일 실행 시험을 추가합니다.
- 전용 `resume-publication RUN_ID`는 기존 실패/interrupted run 또는 잠금 해제 뒤의
  stale-running run과 canonical local seal을 검증한 뒤
  WebDAV 게시/조정만 수행합니다. 일반 `resume`처럼 live issuer discovery나 embedding
  endpoint metadata preflight를 다시 하지 않으므로, 격리 리허설에서 관측 결과로부터
  metadata drift로 추론한 contract mismatch가 있더라도 OCR/embedding으로 재진입하지
  않습니다.

## 첫 MCP activation health-gate 실패

Same-run publication과 WebDAV five-object gate가 통과한 뒤 source
`dc41f7d79a8bc446e59dc45cebf883043f5b6634`의 첫 v1.0.14 MCP를 기동했습니다. MCP는
DB/vector download를 완료했지만 `ServingDatabaseV5Error`로 generation을 활성화하지
못했고 readiness는 계속 `503`이었습니다. 원인은 초기 v5 Worker가 unsupported-document
배열을 relational `(issuer, product_code)` 순서로 hash한 반면 MCP validator는 canonical
payload 순서의 hash만 허용한 cross-role 호환성 결함입니다. 둘 이상의 unsupported
document에서 두 순서가 달라지면 봉인된 DB가 손상되지 않았어도 검증이 실패합니다.

첫 실패의 exact MCP index
`sha256:a78512283a5d7fab3809a9a7229832ee240fed514fbdf3d55dc0660e7521747d`,
`cardrag-v114-candidate-mcp-1` container와
`cardrag-mcp-v114-candidate-state` volume은 후속 hotfix 검증과 분리해 사고 증거로
보존합니다. Container는 OOM 없이 operator stop 뒤 `exited 143`이며 volume은 삭제하거나
재사용하지 않습니다. Hotfix는 기존 early-v5 hash와 canonical hash라는 두 개의 정확한
encoding만 허용하고, 새 Worker export는 canonical 순서로 고정합니다. 재배치는 새 exact
image, 별도 Compose project와 빈 MCP state volume으로만 수행하며 상세 절차는
[v1.0.14 migration](V1_0_14_MIGRATION.md)의 MCP 격리 재시도 절에 있습니다.

## 복구 및 release 경계

원본 v1.0.13 container와 `cardrag-worker-v113-candidate-state`/
`cardrag-worker-v113-candidate-codex-home` volume은 사고 증거이므로 변경하지 않습니다.
v1.0.14 exact images와 새 v114 전용 volume을 사용한 상세 gate는
[v1.0.14 migration](V1_0_14_MIGRATION.md)에 있습니다.

Candidate 복구가 성공해도 stable 승격을 의미하지 않습니다. Stable runtime, volume,
WebDAV stable pointer와 소비 경로는 운영 acceptance와 별도 cutover 승인 전까지 그대로
유지합니다.
