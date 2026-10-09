# 013 FIX_04 — Worker 본처리 회귀와 기본 복원 경로 수정

작성: 2026-10-09, Reviewer / Codex.
대상: `be1acbdbf517680ecba6459197692e2d73f9c533`, FIX_03_REPORT.md 및 현재 diff.
**판정: FIX_03 핵심 백업 보정은 인정한다. 일반 Worker 실행 회귀와 CLI 기본 복원 경로가 남아 최종 인수는 보류한다.**

이번 correction은 아래 두 건의 재현된 기능 오류를 우선 해결한다. 기존 문서/근거를 보존하며 새로운 전체 OCR·유료 추론·2일 배치 관찰을 요구하지 않는다. 운영 설정·타이머·컨테이너·원격 객체는 변경하지 않았다.

## 1. 인정한 결과

Reviewer가 기존 재현 스크립트를 독립 실행했다. 기존 결과를 덮어쓰지 않고 `evidence/reviewer-fix03-independent.json`에 새 결과를 보존했다.

- 첫 번째/두 번째 백업 후 remote index items1→2, fresh-host restore2건 확인.
- native OCR 3개 객체 복원 후 **Worker state root를 target_dir로 지정했을 때 실제 OCRResolver cache hit**, provider 호출0.
- lost_source가 failed/lost_count1로 표시됨.
- remote root 변경 후 객체가1건 재등록됨.
- 실제 local updater/store activation true / gen-valid.
- 관련 suite 재실행: `test_local_serving_and_backup.py` + MCP `test_transport.py`, **13 passed in 0.40s**. 이 단위 테스트가 정상 Worker 본처리 성공까지 증명하지는 않는다.

## 2. P1 — OCR 처리 후 존재하지 않는 self.settings 참조로 Worker 실패

### 근거

`apps/cardrag-worker/src/cardrag_worker/pipeline.py:4957`:

```python
if getattr(self.settings, "backup_mode", "disabled") != "disabled":
```

WorkerPipeline 생성자에 settings 인자/속성이 없고 CLI/partial/resume 경로에서도 해당 속성을 주입하지 않는다. getattr의 대상인 **self.settings 평가에서 AttributeError**가 나므로 backup disabled라도 안전하지 않다. 현재 설정에 backup_mode가 없을 때의 fallback 문제와 구분한다.

검증1:

```sh
uv run --no-sync --all-packages mypy apps/cardrag-worker/src/cardrag_worker/pipeline.py
```

exit1, 4957행 및4963행: `WorkerPipeline has no attribute settings`, 2 errors.

검증2:

```sh
uv run --no-sync --all-packages pytest \
  apps/cardrag-worker/tests/test_pipeline_v5.py::test_v5_pipeline_seals_publishes_resumes_and_reuses_profile_cache \
  -q --tb=short
```

**1 failed in 1.51s**. 실제 v5 본처리의 OCR 후 경로에서 OCRSystemicFailureError/ocr_unexpected_error로 종료했다. 유료 provider를 쓰지 않는 저장소 fixture 테스트다. FIX_03_REPORT의 단위/재현 스크립트 통과 결과와 별개인 신규 회귀다.

### 수정 지시

- 필요한 backup 설정 또는 callback을 WorkerPipeline 생성자에 명시적으로 전달하고 초기화한다. 전체 전역 Settings 객체를 임의 setattr로 붙이는 숨은 연결은 피한다.
- 일반 run/partial/resume가 같은 설정 전달을 사용하도록 연결한다. backup disabled일 때 ledger 접근0, enabled일 때 **OCR 완료 직후** intent가 등록되는 의미를 유지한다.
- getattr(self, "settings", None)만 추가하여 backup 기능을 항상 disabled로 만들어 회귀를 숨기지 않는다.
- backup intent/ledger 실패는 원래 OCR 및 local publication 성공과 분리하고 경고/상태로 기록한다.
- 위 mypy 및 v5 본처리 테스트 통과를 확인한다. enabled 상태에서 OCR 성공→embedding/export 실패가 발생해도 intent/spool이 남는 작은 fixture도 확인한다.

## 3. P1 — backup restore 기본 명령은 실제 캐시 재사용 실패

### 근거

`apps/cardrag-worker/src/cardrag_worker/cli.py:1925`의 기본 destination:

```python
settings.state_dir / "cache" / "ocr"
```

수정된 BackupLedger.restore는 target_dir를 **Worker state root**로 보고 `ocr-seed`, `cache/ocr`, `audit-reports/state-seed`를 그 아래 생성한다. 따라서 `backup restore`를 인자 없이 실행하면 정상 Worker가 읽지 않는 중첩 위치에 복구 ledger/seed를 만든다.

Reviewer가 CLI 기본값과 동일한 destination으로 기존 native OCR 복원 재현을 실행했다:

- restore succeeded / 객체3.
- 정상 Worker state root에서 실제 OCRResolver cache-only resolve → **OCRCacheMissError**.
- provider 호출0.

증거: `evidence/reviewer-fix03-default-restore.json`. 반대로 target_dir를 state root로 준 테스트는 cache hit였다. 함수 단위 복구 구현은 인정하며 CLI 연결 계약을 바로잡는다.

### 수정 지시

- 기본 restore destination을 Worker state root로 통일한다. --target-dir 도움말/운영·복구 문서도 “복구할 Worker state root”로 명확히 한다.
- 기존의 cache/ocr 경로를 안내한 docs/RECOVERY.md 및 앞선 보고서와 현재 CLI 설명의 차이는 새 REPORT에서 정정한다. 기존 REPORT 자체는 덮어쓰지 않는다.
- **실제 Typer backup restore 명령을 --target-dir 없이** 호출하는 통합 테스트를 추가한다. backup client만 fixture로 대체하고 빈 Worker state에서 resolver cache hit/provider0을 확인한다.
- 잘못된 하위 경로에 이미 복원된 자료를 이동할 필요가 있다면 명시적 import/안내를 제공한다. 이번 Reviewer는 운영 자료를 이동하지 않았다.

## 4. 검증과 제출

1. 위2개 회귀를 수정하고 `FIX_04_REPORT.md`에 코드 위치·명령·결과를 기록한다.
2. 바뀐 pipeline/CLI를 포함한 mypy/관련 v5·partial·backup 테스트를 수행한다. 이 correction은 핵심 pipeline을 바꾸므로 Worker의 기존 오프라인 전체 suite(이전 보고서상 약40초)까지 한 번 실행하여 정상 run 회귀를 확인한다. 유료 테스트나 새 full Worker 기동은 요구하지 않는다.
3. FIX_03의 백업2회/fresh restore/native cache hit 근거는 유지한다. 복구 경로 수정의 영향 범위만 재확인한다.
4. 보고서의 “전수 수정 완료”는 검증한 코드/명령 범위와 일치시킨다. pipeline.py를 제외한 mypy 결과로 전체 Worker의 정적 검증 완료를 주장하지 않는다.
5. 두 문제가 해결되고 기존 필수 조건에 새 실패가 없으면 운영 전환 준비로 진행할 수 있다. 운영 반영 완료와 코드 인수 완료는 구분한다.
