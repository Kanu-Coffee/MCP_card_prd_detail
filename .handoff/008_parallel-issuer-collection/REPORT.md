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
