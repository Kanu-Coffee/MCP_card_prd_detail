# FIX_02_REPORT — 격리 후보 준비·중단·회수 결과

작성: Reviewer (사용자의 명시적 요청으로 Executor의 진행 내역과 중단 후 실측을 합쳐 기록), 2026-10-06 13:19 KST. 이 문서는 Executor가 끝내지 못한 단계를 완료로 대체하지 않는다. 근거는 Git/CI, OpenCode 세션 `ses_ef8e105e0ffe1wMq4Pwok4uWvk`, `/tmp/opencode/rr/out/`의 로컬 산출물, 후보 컨테이너·볼륨 및 Reviewer의 읽기 전용 점검이다. `/tmp` 산출물은 영속 증빙으로 간주하지 않는다.

## 판정

**FIX_02 부분 완료, 005 공개 릴리스 미완료.** 문서 정합화, 새 source commit·CI, 후보 이미지 공급망 검증, 운영 상태의 격리 복제와 전수 검증은 진행됐다. 후보 Worker는 전체 live 배치의 OCR 단계에서 Reviewer가 자원·목적 적합성 문제로 정상 취소했다. 신규 generation/READY, 후보 MCP 12도구 실호출, rollback, readiness receipt, 증거 봉인, `v1.0.32` tag, Docker Hub 이미지 및 GitHub Release는 없다. 중단된 실행이나 운영 03:00 결과를 1.0.32 후보 성공으로 세지 않는다.

## Executor가 실제 진행한 사항

1. `docs/RELEASING.md`에서 1.0.32의 writer-free 전체 상태 복제 예외를 문서화하고 공개 검증 진입점·dispatch 입력을 `cardrag_mcp.release_readiness`·`release_readiness_sha256`로 정정했다. Commit `c4a53b63d5729a903cf19beee9566d3995d5d19c`가 `main`/`origin/main`에 있고 작업 트리는 clean이다. [CI run 37390556489](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37390556489)은 success. Executor 세션에는 관련 pytest `2282 passed` 및 문서 대상 테스트 `87 passed`도 기록돼 있다.
2. 이 source commit에서 `1.0.32` 후보 OCI 두 개를 다시 빌드했다. Worker index `sha256:b096b6bdbb09e555738b689b0190b94b1486b7a5241f2e5359897bc2e4a1aca0`, MCP index `sha256:b3a80d08c2005d08b324ed622d256d4e09bf07d456254372cd657d86cb75f7e1`. 두 이미지 라벨의 revision/version은 `c4a53b6…`/`1.0.32`; Executor의 `/tmp/opencode/build/preflight_oci.sh` 결과는 두 역할 모두 `PREFLIGHT-OCI-OK`다. 이들은 GHCR 후보 이미지일 뿐 Docker Hub 정식 발행물이 아니다.
3. 종료된 운영 `cardrag-worker-v130-candidate-state`에서 1.0.32 전용 상태 볼륨을 복제했다. 저장소의 `tools/cardrag_offline_volume_verify.py state` 결과: `status=passed`, 파일 64,911개, 디렉터리 46,387개, SQLite DB 3개, content tree SHA-256 `83e4c230ee06d5c0f5d56722763423ff5236b01b5b465420c3c4958484ca546b`. 이는 복제 당시 전체 상태의 일치·DB integrity 근거다. Codex 인증 볼륨 검증 도구는 운영 원본 루트 권한 0755가 도구의 0700 전제와 달라 `source_root_invalid`로 실패했다. Executor는 별도 byte 비교를 언급했지만 **공식 codex-home 검증 통과로 쓰지 않는다**. 후보 인증은 복제 상태로 준비됐고 Worker 시작·Qwen preflight에는 성공했다.
4. 후보 실행 전 native OCR 캐시 GET 전용 감사에서 발견된 key 308개와 miss probe 4개, 관리 객체 624개를 기록했다. 사전 inventory SHA-256은 `6a47e6272c867444083b2579e2a28039314d18c3cf75d1408c1df66ac48b0fe4`였다. stable channel 원문 SHA-256은 `b63f4a66a2d41b9e97a5265ae15d824ca542844d02f0ea4ae4966ca547ed9ddc`였다.
5. 후보 Worker run `9c9fda8a69ee4c9899d62170281475ad`는 2026-10-06 09:34 KST 시작. PDF 단계는 5,066 source 중 cache hit 5,060, miss 6, revision 2건으로 끝났다. 새 PDF SHA-256은 롯데 1863 `a946e80c…`, 하나 15911 `2fcba28a…`로 각각 이전 `d66b49f1…`, `df16ca47…`와 다르다. OCR 진행 로그는 11:48:47에 `2725/5514`, 실패 0건이 마지막이다. Executor가 하나 15911 처리 완료를 확인했고 롯데 1863의 12페이지 PaddleOCR-VL 추론이 진행 중이었다. 신규 generation과 게시 결과는 없다.
6. Executor는 `/tmp/opencode/rr/`에 WebDAV 감사, 후보 MCP rollout, receipt 어셈블러 등의 임시 스크립트를 준비했다. `rollout.py`, `receipt.py`, `probes.sh`는 **실행 완료 증거가 아니며**, 이번 취소된 Worker run을 전제로 작성돼 재사용할 수 없다. 공개 evidence 디렉터리는 생성되지 않았다.

## Reviewer의 중단 판단 및 사후 확인

후보 overlay `deploy/worker/compose.candidate.yaml`은 `CARDRAG_OCR_PROVIDER=local-paddleocr`, `CARDRAG_OCR_CACHE_MODE=read-only`, `CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=false`, `CARDRAG_PDF_CACHE_FORCE_REVALIDATE=true`다. 운영은 `codex-exec`/OCR 캐시 read-write다. 따라서 candidate가 두 새 PDF를 로컬 Paddle로 오래 처리해도 그 결과는 운영 OCR 캐시에 남지 않고, 다음 운영 실행에서 다시 처리될 수 있다. 당시 Paddle 자식 프로세스는 CPU 추론 중이었고 외부 유료 OCR 호출은 허용되지 않았다. 이후 단계의 원격 embedding/provider 호출 가능성은 별개다. 전체 5,514건 live 배치가 공개 이미지 발행 자체에 필수인 것은 아니므로 현재 검증 계약의 비용이 목적에 비해 크다고 판단했다.

2026-10-06 13:10:57 KST 후보 컨테이너 **한 개에만** SIGTERM을 보냈다. CLI cancellation drain이 끝났고 `/tmp/opencode/rr/out/run_terminal.json`은 `shutdown_complete`, exit 143을 기록했다. 중단 직후 후보 DB의 run 상태는 `interrupted`였다. 컨테이너는 `--rm`으로 사라졌으며, 사용 중이 아닌 `cardrag-worker-v122-candidate-state` 복제 볼륨만 삭제했다. 운영 원본 볼륨과 MCP/stable은 건드리지 않았다. 루트 디스크 여유는 중단 전 약 56GiB에서 복제본 제거 후 **105GiB**로 회복했다.

Reviewer의 사후 GET 전용 감사에서 native OCR 캐시 624개 객체가 사전 목록과 바이트 해시까지 일치했다(inventory SHA-256 위 값 동일). stable channel SHA-256도 위 사전 값과 같고 generation은 `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`다. candidate channel은 이전 `g-c622d3c4b1fb4df5a74a4b13-e95cb9ce7d7f`를 계속 가리킨다. 운영 MCP `cardrag-stable-v1026-mcp-1`은 healthy, 운영 Worker service는 마지막 결과 success/inactive, timer는 active이고 다음 실행은 2026-10-07 03:00 KST다.

## 남은 작업·금지되는 재시도

현재 `v1.0.32` tag/GitHub Release가 없고 `release.yml`의 이번 버전 dispatch도 없다. 복제 볼륨은 제거돼 `run 9c9f…`을 resume할 수 없다. **같은 전체 후보 배치를 재개하거나 다시 clone/Paddle OCR을 시작하지 않는다.** `/tmp/opencode/rr/receipt.py`의 성공 값이나 과거 운영 run을 새 후보 성공으로 기입하지 않는다. 005를 계속하려면 `FIX_03.md`의 경량 공개 발행 계약으로 검증기·workflow·문서를 수정하고 CI/OCI 공급망 증거로 공식 발행한다. 운영 배치의 새 PDF 처리는 운영의 독립된 예약 실행에 맡긴다.
