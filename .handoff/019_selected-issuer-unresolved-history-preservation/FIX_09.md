# 019 FIX_09 — pending 없는 무바인딩 필수 artifact의 완료 오판 및 운영 명령 정정

2026-10-20 Reviewer: Codex. **FIX_08 재검토 결과 보완 필요.** PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–08와 각 REPORT, 기존 리뷰·운영 자료 및 BACKUP_POLICY_RECHECK_01, 현재 Git 상태/관련 소스를 대조했다. 기존 handoff는 보존하고 HEAD `aaeb2c4` 위 017–019 미커밋 변경을 덮어쓰지 않는다.

## 확인된 개선과 경계

검증 근거 없는 metadata pending 전송 차단, 로컬 cache 근거 재바인딩, bound artifact의 pending 독립 receipt/missing 검사 및 index 완료 확인을 확인했다. 임계값 미달 시 main/partial의 읽기 전용 진단 병합도 구현됐다. 기존 memo 실측 개선은 이전 리뷰대로 인정한다.

정책상 정상 pending과 선택 백업의 검증 미완료는 구분한다. pending을 없애기 위해 전역 force flush하거나 임계값/선택 범위를 우회하지 않는다. 이번 결함 재현은 임시 원장/mock 원격에 한정되며 운영 손상이 발생했다는 판정이 아니다. 배포·운영 flush·운영 원장 수정·배치/timer 기동은 하지 않았다.

## 1. [P1] 필수 무바인딩 경로의 pending이 없으면 선택 완료로 오판

위치: `backup.py:_scope_diagnostics` 현재 1235–1275행, `_scoped_completion` 1318–1332행, `scoped_backup_scope`의 무바인딩 경로 구성.

FIX_08은 evidence 없는 seal reuse-key 경로를 allowed에 남기고, expected에는 넣지 않는 방식으로 전송을 차단한다. 그러나 `in_scope_unbound`는 **backup_pending 행을 순회할 때만** 증가한다. `_scoped_integrity`는 expected만 검사하므로 expected 밖인 이 필수 경로의 누락을 보지 못한다. 결국 metadata pending이 없으면 allowed에 남아 있는 미검증 경로가 완료 검사에서 사라진다.

### Reviewer 재현

`test_fix_06_scoped_backup.py`의 기존 fixture/helper를 사용했다.

1. `_make_document(..., reuse_key_present=True, cache_kind='native')` 및 seal 생성 후 선택 등록한다.
2. run-local native manifest를 제거한다(로컬 cache 근거도 없는 상태).
3. 임시 ledger의 manifest/READY pending만 제거한다. 해당 receipt도 없다. 실제 원장에서 삭제하라는 지시가 아니라 누락 행 상황의 결함 주입이다.
4. 새 `scoped_backup_scope`와 expected를 사용하여 남은 PDF/OCR CAS를 mock scoped flush한다.

```text
manifest/READY in allowed == True
manifest/READY in expected == False
mock remote에 manifest/READY 없음
in_scope_unbound == 0, in_scope_missing == 0
backup_scope_complete == True  # 현 결함
```

```bash
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review9/test_review9.py -q -s
# REPRO: required unbound manifest+READY absent, but scope_complete=True
# 1 passed in 0.30s (결함을 assert한 재현)
```

### 요구와 인수 기준

- **선택 필수 artifact의 집합**, 전송 가능한 검증 바인딩, 실제 pending 행을 구분한다. 필수 경로의 검증 미완료 여부를 pending 행 존재에 의존시키지 않는다. 구현은 기존 구조에서 가장 작게 보완하고 별도 대규모 복원 계층은 도입하지 않는다.
- 필수 무바인딩 경로가 있으면 전송하지 않고 진단에 남기며 완료 True를 금지한다. receipt만 존재하거나 pending/receipt가 모두 없는 경우에도 동일하다. 정상 legacy 근거를 검증하여 재바인딩할 수 있으면 기존 정식 경로를 사용한다.
- doc_map은 귀속의 가시성 근거이며 전송 권위가 아니라는 계약을 유지한다. 정책상 제외/비선택 backlog를 필수 미완료로 확대하지 않는다. 카운트 단위(경로/행)를 명확히 하고 중복 행 때문에 완료 상태가 달라지지 않게 한다.
- 위 fixture의 pending 있음/없음, receipt 있음/없음, 한쪽 metadata만 누락, 정상 재바인딩/재시도, unchanged, deferred/status 진단을 회귀로 검사한다. 실제 CLI 호출부의 expected 전달도 유지한다.
- tmp 파일은 영구 산출물이 아니므로 재현을 저장소 회귀로 옮긴다. 운영 SQL 삭제/허위 receipt/무바인딩 전송으로 해결하지 않는다.

## 2. [P2] 새 운영 복구 명령이 이미지 entrypoint와 중복되어 실행되지 않음

위치: `FIX_08_REPORT.md` §4의 compose 선택 재시도 예시. 기존 보고서는 수정하지 않고 새 보고서에서 정정한다.

보고서 명령은 `worker-compose.sh run --rm worker cardrag-worker backup flush ...`이다. 저장소 Dockerfile과 실제 image의 ENTRYPOINT는 이미 `["cardrag-worker"]`이므로 CLI에 `cardrag-worker`가 하위 명령으로 한 번 더 전달된다.

Reviewer는 운영 volume/env/네트워크를 연결하지 않은 임시 컨테이너에서 인자 구조만 재현했다.

```text
docker image inspect cardrag-worker:019-4dba44664659 --format '{{json .Config.Entrypoint}}'
["cardrag-worker"]
docker run --rm --network none cardrag-worker:019-4dba44664659 cardrag-worker backup --help
exit=2: No such command 'cardrag-worker'.
```

수정 image 배포 후 사용할 명령의 올바른 형태는 다음과 같다. 이번 리뷰에서 실행하지 않았다.

```bash
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori
```

Executor는 release/helper/entrypoint 계약에 맞는 복붙 명령을 새 보고서에 제공하고, 네트워크·운영 state를 연결하지 않는 help/argv 검증으로 CLI 진입까지 확인한다. 현재 운영 image는 FIX_06–09를 포함하지 않으므로 새 옵션의 실제 사용에는 수정 image 배포가 선행되어야 한다. credential/private overlay는 출력하거나 저장소에 넣지 않는다. 이번 correction에서 실제 배포/원격 flush는 수행하지 않는다.

## 검증 결과와 다음 산출물

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1430 passed, 10 warnings in 54.42s
uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
git diff --check
pass
```

전체 suite 통과와 별개로 위 추가 결함이 재현됐다. Executor는 FIX_09를 구현하고 `FIX_09_REPORT.md`를 새로 작성한다. 수정별 회귀, 전체 Worker suite·ruff·diff 검증, 인자 구조 확인 결과와 운영 미검증 범위를 기록한다. 비선택 7사/전체 serving 및 기존 hash/root/lock/원자성/재개/GC pin 계약을 보존한다.
