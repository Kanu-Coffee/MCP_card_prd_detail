# 013 최종 구현 인수 검토

작성: 2026-10-09, Reviewer / Codex.
기준 commit: `3eaa35d1af64a36ab2974b756700e3d30d403c6d`.

## 판정

**013 구현 인수 가능. FIX_04의 두 가지 차단 결함은 해소되었다. 운영 반영 준비 단계로 진행할 수 있다.**

이 판정은 현재 코드와 제출된 오프라인 검증의 인수다. 운영 배포·실제 WebDAV 접속·03시 배치 완료·GitHub release 발행을 이번 검토에서 수행하거나 완료 확인한 것은 아니다. 운영 단계의 확인을 새 유료 OCR 재실행 또는 2일 관찰 조건으로 확대하지 않는다.

## 검토한 근거

PLAN, FIX_01~04 및 대응 REPORT, 현재 Git diff/status, 새 회귀 테스트를 검토했다. 과거 untracked 012 운영 경과/증거는 보존했다.

Reviewer 독립 실행:

```sh
uv run --no-sync --all-packages pytest \
  apps/cardrag-worker/tests/test_local_serving_and_backup.py \
  apps/cardrag-mcp/tests/test_transport.py \
  apps/cardrag-worker/tests/test_pipeline_v5.py -q
# 28 passed in 2.01s

uv run --no-sync --all-packages mypy \
  apps/cardrag-worker/src/cardrag_worker/pipeline.py \
  apps/cardrag-worker/src/cardrag_worker/cli.py \
  apps/cardrag-worker/src/cardrag_worker/partial_cli.py
# Success: no issues found in 3 source files
```

Executor 제출 증거: Worker 전체 오프라인 suite 1,255 passed/9 warnings, partial suite42 passed, lint/format 통과. 이미 필요한 gate가 제출됐고 이번 핵심 회귀도 독립 재현했으므로 전체 테스트를 불필요하게 반복하지 않았다.

## 차단 결함의 해소

1. WorkerPipeline 생성자에 settings를 명시적으로 전달하고 보관한다. 일반 CLI/partial 경로에 연결됐고 미설정/disabled는 ledger를 호출하지 않는다. 이전에 실패했던 v5 본처리 테스트가 이번28건에 포함되어 통과했다.
2. enabled 상태의 OCR 성공→embedding 실패에서도 intent/spool이 남는 테스트가 통과했다. ledger 기록 오류를 주입해도 본처리 publication이 완료되는 테스트가 통과했다.
3. `backup restore` 기본 경로를 Worker state root로 통일했다. **실제 Typer 명령을 --target-dir 없이 실행하고 실제 OCRResolver에서 cache_reused=true/provider_called=false/호출0**을 확인하는 테스트가 통과했다. 도움말/RECOVERY 문서도 맞춰졌다.
4. FIX_03의 독립 근거도 유지한다: 백업2회 인덱스 누적1→2, 빈 state 복원2건, native OCR 복원 후 cache hit, lost_source 실패 표시, remote root 변경 객체 재등록, local updater/store activation.

## 운영 반영 시 확인할 항목

다음은 배포 설정과 실제 자료의 확인이다. 코드 인수의 새 개발 차단 항목으로 취급하지 않는다.

1. `/opt/cardrag/current`의 실제 env/overlay를 보존한 새 Worker/MCP 배포를 준비한다. local transport를 명시하고 version 없는 `cardrag-serving`을 Worker RW/MCP RO로 연결한다. 새 볼륨을 UID/GID10001이 쓸 수 있게 초기화한다. 일반 secrets와 optional WebDAV overlay를 구분한다.
2. 기존 정상 sealed generation으로 초기 serving head를 준비한다. 새 전체 discovery/OCR/embedding을 하지 않고 provider-free 게시/재개 경로를 사용한다. 기존 MCP state는 유지하고 read-only 기존 데이터를 직접 수정하지 않는다.
3. 백업 기본값 disabled가 운영 OCR 백업을 의도치 않게 끄지 않도록, 운영 env에서 선택한 정책을 명시한다. PLAN의 권장값은 immediate 증분이며 hybrid를 선택하면7회/30OCR/1GiB/7일 조건과 미백업 기간을 그대로 표시한다.
4. 초기 inventory에서 기존 OCR/PDF와 adopted/Paddle 결과도 보존되는지 확인한다. 예전 generation/OCR CAS를 즉시 삭제하지 않는다. 기존 원격 백업을 보존하고, source/variant/manifest 증거로 등록·복구 가능성을 확인한다. 15건 Paddle를 다시 실행하는 검증은 하지 않는다.
5. FIX_03_REPORT의 standalone backup service는 예시 경로다. `/home/cardrag/app`나 uv 설치 위치를 그대로 복사하지 않는다. 실제 `/opt/cardrag/current` Compose/image/env/state volume을 사용하는 실행 명령과 service/timer를 준비한다. backup enabled에서만15분 재시도를 활성화하고 single writer lock을 사용한다. sudo가 필요하면 사용자용 복붙 명령을 제출한다.
6. 후보/전환 환경에서 최신 generation activation과 summary/bundle/PDF 응답을 확인한다. WebDAV 장애가 기존/신규 local serving을 막지 않는다는 경계는 격리된 config/mock으로 확인할 수 있다. 운영 WebDAV를 끊거나 본배치를 두 번 실행할 필요는 없다.
7. 실제 WebDAV에서 신규 소량 delta와 같은 자료 재실행을 확인하고, backup commit 상태·미백업/유실 상태·last backup을 구분한다. mock 집계의 requests를 실제 HTTP 전체 요청수/전송량으로 확대 해석하지 않는다.
8. 현재+이전1개 유지, source 복사 중의 경합/재시도, serving/spool 여유 공간을 운영 전환에서 확인한다. full Worker 결과를 검증하려면 다음 정상03시 실행을 관찰하며 별도 재OCR 실행을 추가하지 않는다.

## 검증 한계와 보고 구분

- FIX_02의 e2e_local_serving_mcp_test.py는 reader/파일 복사/직접 SQLite SELECT 범위이며 실제 MCP readiness·v5 도구 E2E 근거로 사용하지 않는다. 본 검토의28건은 backend 전달, 실제 v5 pipeline, 기본 CLI 복구 및 오류 격리 경계를 확인한다.
- 새 호스트의 운영 전체 OCR 및 기존 Paddle/adopted 변형 전부를 실제 WebDAV에서 복구한 실증은 이번28건의 native fixture 검증과 다르다. 운영 migration 단계에서 기존 백업을 유지한 채 확인한다.
- 코드 인수, 운영 전환, 실제 원격 backup 완료, 공개 release 완료를 각각 구분하여 보고한다.

## 최종 결론

이전 Worker 본처리 AttributeError와 기본 복구 경로 오류를 더 이상 차단 결함으로 유지하지 않는다. **013 코드 인수를 완료하고 운영 반영 준비로 진행한다.** 이번 Reviewer는 코드/운영 설정/컨테이너/타이머/원격 객체를 변경하지 않았으며 이 인수 문서만 추가했다.
