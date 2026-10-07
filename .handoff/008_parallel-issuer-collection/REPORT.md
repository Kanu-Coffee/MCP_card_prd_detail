# 008 Executor REPORT — 카드사 병렬 수집·장애 격리 구현, 운영 배치 인계 준비

작성 역할: Executor. 2026-10-07 KST. 브랜치 `codex/008-parallel-issuer-collection`, 기준 `ff94347`에서 006/007을 그대로 계승했다. **이 문서 최초 작성 시 구현·자동 검증은 완료됐으며 운영 첫 배치 결과는 아직 없다.** 배포/구동과 사용자의 종료 알림 뒤 검증을 아래에 추가 기록한다.

## 구현

- issuer별 독립 HTTP client/cookie와 limiter로 discovery를 제한 병렬 실행(기본 4, 범위 1~8). issuer 전체 discovery retry 포함 deadline 기본 300초. 결과 순서는 scheduler 입력 순서로 유지한다.
- issuer origin/parser 오류는 typed 안전 reason으로 반환한다. PDF는 기존 전역 8/issuer 2 병렬 scheduler를 유지한다. 한 issuer 다운로드 예산이 소진되면 해당 issuer의 실행 중 요청만 취소·회수하고 대기 항목은 terminal skip한다. 다른 issuer와 공용 scheduler fail-fast는 유지한다.
- issuer 단위로 성공 결과를 채택한다. 부분 다운로드 실패 issuer의 신규 결과는 제외하고, 실제 stable manifest/serving DB의 current·historical·availability·supersedes 관계를 복원한다. canonical snapshot/seed source identity와 검증된 local PDF CAS를 재사용하며, 로컬에 없을 때만 WebDAV CAS를 SHA/크기/쪽수 검증해 복원한다. origin 재접속 및 OCR 재호출로 우회하지 않는다.
- carry 문서는 기존 OCR SHA pin·content variant를 사용하여 기존 OCR·embedding 재사용 경로와 전체 exporter 입력에 포함한다. 검색 DB/vector에 빠지는 manifest-only 병합은 하지 않는다. `ocr_failed`와 unsupported DRM의 기존 disposition도 복원한다.
- retirement에 failed issuer freeze를 전달한다. 기존 candidate/grace/reinstated를 실패 관측으로 진행시키지 않는다. 전체 corpus baseline/PDF pruning에 carry 자료를 포함한다.
- `issuer_collection` 상태 테이블은 run별 성공/실패·정상 목록 건수·실제 관측시각을 보존한다. content-key snapshot이 나중 실패 run으로 이동하더라도 이전 정상 retention 기준이 사라지지 않는다. 원래 snapshot/run/stage 상태 계약은 유지한다.
- `issuer-collection.json`은 issuer별 안전 reason/class/시도/건수/freshness/carry 기준과 collection_status를 제공한다. 수집 중 `terminal=false`, 종료 후 true. 일부 장애 정상 마감은 exit 0 + degraded, 모두 실패는 OCR/게시 전 nonzero다. 공용 WebDAV/SQLite/디스크 오류는 전체 실패다.
- failed issuer 재처리 대상은 요청을 완료 처리하지 않고 대기한다. 대상이 전부 deferred면 불필요한 재게시 없이 no-change가 가능하다. 새로운 run에서 복구 issuer를 다시 시도한다.
- 설정·CLI 요약·Compose·env 예시·OPERATIONS를 갱신했다. core/MCP schema 및 Dockerfile 변경은 없다. 006 OpenCode opt-in은 유지하며 운영 활성화는 하지 않았다.

## 검증 근거

1. 초기 관련 suite: 255 passed / 1 기존 오류 기대값 불일치. 모두 issuer 실패 정책에 맞춰 테스트의 예외 기대를 수정했다.
2. 최초 전체 suite: **2,338 passed / 1 failed**. OCR 완료 barrier를 검증하는 monkeypatch가 새 discovery 단계에도 적용됐던 문제였다. discovery를 명시적 issuer group/per-group 1로 실행하여 기존 OCR 검증 범위를 보존했다.
3. 상태 기준 보완 후 관련 테스트 **40 passed in 6.07s**.
4. 최종 전체: `uv run pytest packages/cardrag-core/tests apps/cardrag-worker/tests apps/cardrag-mcp/tests tests/runtime_v1 -q --tb=short`: **2,339 passed, 기존 warning 9건, 51.41초**. 출력 `/tmp/cardrag008-fulltests-final.txt`.
5. CI 범위 Ruff check/format: 통과(215 파일). mypy core/Worker/MCP: **103 source files 통과**. `git diff --check` 통과.
6. base 및 OpenCode overlay Compose `config --quiet` 통과. 실제 `/etc/cardrag/worker.env` + 운영 secrets overlay 조합도 통과. gitleaks Worker src/신규 테스트/deploy: **no leaks found**. Dockerfile·workflow·repo shell은 변경하지 않아 해당 검사 반복 생략.

추가한 12개 케이스에는 discovery overlap·deadline·reset·parser·prepare 실패, 다운로드 도중 issuer 원자적 제외/다른 issuer 지속, 동시성 상한·외부 취소 drain, 모두 실패, 공용 storage 오류, 설정 범위, 정상 freshness 기준 보존, 단종 freeze가 있다. 실제 WebDAV facade + 격리 MockTransport의 v5 통합 시나리오는 다음을 확인했다.

- 최초 생성 → 신한 개정 PDF 추가 → 신한 reset + 우리 신규 PDF 추가의 전체 export/publish.
- 신한 current/superseded 두 문서의 manifest/PDF/OCR/variant 동일, 검색 document_pages·embedding_views에 두 문서 유지.
- 신규 우리 PDF만 provider 1회, 다음 run no-change/0회, codex→opencode 설정 변경도 no-change/0회.
- 신한 복구 후 complete, 신한-only 재처리 요청은 장애 시 deferred/no-change·미완료 영수증 유지.
- carry PDF CAS 손상은 게시 실패로 처리하며 stable pointer 및 provider 호출수 유지.

## 배포 전 확인과 범위

운영 MCP `cardrag-stable-v1026-mcp-1`은 기존 007 이미지로 healthy, Worker 실행 중 없음, timer active다. current는 `/opt/cardrag/007-31edb1d`, 가용 공간 약 69G다. 기존 migration verify(5,172 variant/5,512 OCR)와 baseline은 유지한다. 신한 공인 IP 차단은 확정하지 않았으며 URL/parser를 바꾸지 않았다.

기존 provider codex/qwen, epoch 0, publication approvals, 동일 project/state/auth 볼륨, 원격 GC false를 유지한다. 새 Worker만 exact commit 이미지로 교체할 예정이며 MCP 재시작·migration apply 반복·강제 OCR/Paddle·전체 state clone은 수행하지 않는다. OpenCode 운영 활성화·PR merge·정식 공개 릴리스는 별도 게이트다. 장기 배치는 구동/실제 running 1회 확인 후 턴을 종료하고 사용자가 결과를 알려준 뒤 검증한다.

## 2026-10-07 17:38 KST — 배포 및 첫 운영 배치 기동

**구현·자동 검증·배포·기동까지 수행했다. 실제 장기 배치 결과 및 운영 인수 검증은 미완료이며 사용자 종료 알림 뒤 수행한다.** 실행 중 ExitCode 0을 완료로 해석하지 않는다.

- 구현 커밋 `6b42a1a55899899be8fdab5d3b3555ef7b5390da`, remote `codex/008-parallel-issuer-collection`에 push 완료. 이후 REPORT 추가 커밋은 문서만 변경하므로 실행 이미지는 이 구현 커밋으로 고정한다. main 병합/공개 릴리스는 수행하지 않았다.
- [GitHub CI run 37594483364](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37594483364): 동일 구현 커밋, **completed / success**. 로컬 최종 전체 테스트 2,339 passed와 정적 검증도 통과했다.
- 빌드: `docker build --target worker --build-arg APP_VERSION=1.0.32-008 --build-arg VCS_REF=6b42a1a55899899be8fdab5d3b3555ef7b5390da -t cardrag-worker:008-6b42a1a .` exit 0. OCI revision 일치. 이미지 ID `sha256:2eed6edbeb4f1d57d69481bd0d576dfbfcc957d29cb3bd89a5591619f5e9d121`. 빌드 로그 `/tmp/cardrag008-build-exact.log`.
- exact archive `/opt/cardrag/008-6b42a1a` 생성, host-local secrets overlay에서 Worker image만 override했다. `current`를 007에서 새 디렉터리로 원자 교체했다. `/etc/cardrag` 비밀/env 변경 없음. MCP는 `cardrag-mcp:007-31edb1d`와 기존 healthy 컨테이너를 유지했다.
- 실제 운영 설정을 사용하는 짧은 read-only preflight exit 0: stable `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`, codex-exec / qwen3.8-flash, cache read-write / epoch 0, discovery concurrency 4 / deadline 300초, pending reprocess 없음, GC 승인 false. 동일 stable의 007 migration 검증/기존 OCR baseline을 재사용했다. migration apply/전수 검증/Paddle/강제 재OCR/전체 state clone은 수행하지 않았다.
- Compose project `cardrag-worker`. 기존 `cardrag-worker-v130-candidate-state`, `cardrag-worker-v120-recovery-auth-20260910`, `cardrag-worker-paddleocr-models` 볼륨과 동일 mount 확인. Paddle 모델 볼륨의 보유는 Paddle 실행을 의미하지 않는다. Compose의 기존 외부생성 볼륨/종료된 orphan 안내는 경고이며 새 볼륨 생성·삭제나 기존 컨테이너 제거를 하지 않았다.
- 기동 명령: `/opt/cardrag/008-6b42a1a/operations/worker-compose.sh run -d --no-deps --name cardrag-prod-008-first worker run`.
- 컨테이너 `e3a34489ff0a68e7bd900c3b74ede237b145f5a9ed3ccdee516c3b3cc9c35446`, 시작 **17:37:44 KST**, 실제 state **running**, command `["run"]`, AutoRemove **false**, OOMKilled false. 1회 상태 확인 후 추가 장기 감시/폴링은 하지 않았다. 시작 로그의 capacity preflight 통과, free bytes `64071401472` (약 59.7 GiB). 실제 discovery 후 필요 공간 계산은 기존 동적 정책을 따른다.
- timer active, 다음 **2026-10-08 03:00 KST**. systemd는 `/opt/cardrag/current`의 새 코드/overlay를 사용한다. 수동 run과 예약 run은 동일 state/worker.lock으로 중복을 방지한다. timer를 중지하거나 예약 실행을 추가하지 않았다.
- 이번 배포 rollback 참조는 직전 007 코드/이미지/설정 **1세트**로 기록했다. 새 대형 백업·state 복제 없음. 이전부터 남은 배포 디렉터리/운영 증거의 추가 삭제는 이번 미인수 배치 중 수행하지 않았다.
- 운영 근거: `/opt/cardrag/008-6b42a1a/operations/{rollback.json,prior-evidence.json,image-config.json,preflight-result.json,worker-start.json}`. 사용자 비밀값이나 전체 env는 저장하지 않았다.

### 사용자의 감시 및 다음 검증

```sh
docker logs -f --tail 50 cardrag-prod-008-first
docker inspect cardrag-prod-008-first --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}'
```

`exited exit=0 oom=false`가 정상 프로세스 종료 기준이다. `running exit=0`은 실행 중이다. 종료/오류를 알려주면 run의 `reports/issuer-collection.json`, exit, issuer별 수집 결과, 신한 기존 문서 carry/단종 freeze, 기존 OCR SHA·호출수 및 신규 OCR 구분, 전체 corpus/export와 MCP loaded generation을 검증한다. 신한이 여전히 reset이면 다른 issuer가 정상 처리되어도 신한 최신화는 미완료로 명시한다. 신한 접속 복구 자체는 인수 필수 조건으로 추가하지 않는다.

006 OpenCode 코드는 통합했지만 운영 provider 전환은 별도 승인 게이트로 **미실행**이다. 준비된 `deploy/worker/compose.opencode.yaml`을 사용할 수 있으며 승인 후 provider/model/effort/secret/fallback/rollback을 확인한다. 현재는 기존 codex 설정을 유지한다.
