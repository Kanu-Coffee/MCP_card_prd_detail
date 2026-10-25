# 019 FIX_10 — generation-only OCR metadata의 필수 범위 소실 방지

2026-10-20 Reviewer: Codex. **FIX_09 재검토 결과 P1 한 건 보완 필요.** PLAN/PLAN_AMENDMENT_01, REPORT, FIX_01–09/각 REPORT, 기존 리뷰·운영·정책 재확인 자료 및 현재 Git/소스를 대조했다. HEAD `aaeb2c4` 위 017–019 미커밋 변경과 기존 문서를 보존한다.

## 해결 확인

- seal에 native reuse key가 있는 경우에는 pending 유무와 독립적인 `allowed − expected.keys()` 판정으로 필수 무바인딩 metadata가 완료를 차단한다. FIX_09의 해당 재현과 회귀는 해결됐다.
- compose 명령의 entrypoint 중복은 보고서에서 정정됐다. Reviewer도 로컬 `uv run --no-sync cardrag-worker backup flush --help`로 `--run`/`--issuer` 인자 계약을 확인했다. 실제 배포/원격 flush는 하지 않았다.
- 정책상 정상 pending은 오류로 분류하지 않는다. 기존 content/adopted cache 제외와 비선택 backlog 정책을 이번 결함 수정 때문에 확대하거나 뒤집지 않는다.

## [P1] seal reuse key가 null인 generation-only 결과는 여전히 metadata를 조용히 제외함

위치: `backup.py:scoped_backup_scope`, 현재 959–974행의 doc_map 가시성 처리.

실제 우리 run에서 문제가 된 신규 OCR은 seal의 `ocr_reuse_key`/`ocr_cache_kind`가 null인 generation-only 결과다. 이 경우 로컬 manifest가 없으면 key 기반 필수 경로를 생성할 수 없다. 기존 recorder의 doc_map에는 선택 문서와 metadata 경로의 귀속이 남아 있지만, scope는 **그 경로에 현재 pending 행이 있을 때만** 이를 allowed에 포함한다.

따라서 pending 행까지 없으면 manifest/READY가 allowed에서 사라진다. FIX_09에서 개선한 경로 단위 unbound 검사도 이미 scope 밖으로 빠진 경로는 볼 수 없으며, CAS만 백업하고 `backup_scope_complete=True`가 된다. FIX_09 테스트는 전부 reuse key가 있는 형태여서 실제 generation-only 분기를 검증하지 않는다.

### Reviewer 재현

기존 `_make_document` 기본 fixture(null reuse key)로 seal/정상 선택 등록 → run-local native manifest 제거 → **임시 DB**의 manifest/READY pending만 제거(해당 receipt 없음, doc_map 두 건은 그대로 유지) → scoped flush.

```text
seal ocr_reuse_key == None
doc_map_<manifest path> == selected document_id
doc_map_<READY path> == selected document_id
manifest/READY not in allowed
mock remote에 manifest/READY 없음
in_scope_unbound == 0, in_scope_missing == 0
backup_scope_complete == True  # 현 결함
```

```bash
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src \
  uv run --no-sync pytest /tmp/cardrag019-review10/test_review10.py -q -s
# REPRO: generation-only metadata ownership retained in doc_map; missing metadata dropped, scope_complete=True
# 1 passed in 0.27s (결함을 assert한 재현)
```

임시 SQL 조작은 누락 행 상황의 결함 주입이며 실제 운영 원장 삭제를 지시하지 않는다. tmp 파일은 영구 증거가 아니므로 위 절차를 저장소 회귀로 옮긴다. 실제 운영 데이터 손상이 관측됐다는 판정도 아니다.

### 요구 및 인수 기준

1. generation-only(null key) 문서에 등록됐던 필수 OCR backup unit의 가시성을 pending 유무와 독립적으로 보존한다. 남아 있는 recorder 귀속 근거를 미검증/미완료 표시에는 사용할 수 있지만, doc_map 단독으로 내용 바인딩이나 전송 권위를 부여하지 않는다. 운영 문서/과거 seal을 rewrite하지 않는다.
2. 검증 근거가 사라졌으면 정상 근거 기반 재바인딩/정식 복구 또는 명시적인 미확인·미완료로 처리한다. 범위에서 조용히 제외한 뒤 완료 선언하지 않는다. 새 복원 체계 대신 기존 recorder/scope/진단 경계에서 최소 수정한다.
3. null key와 key 존재 두 형태 모두에서 manifest/READY pending 있음·없음, receipt 있음·없음, 한쪽만 누락, 증거 손상/소실, 정상 재등록·재시도·unchanged 및 읽기 전용 deferred/status 진단을 검증한다. 실제 main/partial/선택 재시도 CLI의 전달 계약을 유지한다.
4. 오래된 다른 문서·비선택 pending·정책상 제외 cache를 일괄 필수화하거나 전송하지 않는다. 같은 문서와 관련된 과거 metadata를 다룰 때 현재 필수 단위와 과거 기록의 차이를 설명하고, 증거만으로 판정할 수 없으면 완료를 단정하지 않는다. 원장 수작업 삭제/허위 receipt/무바인딩 전송은 금지한다.

## Reviewer 검증 및 산출물

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1435 passed, 10 warnings in 54.26s
uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/
126 files already formatted
git diff --check
pass
```

Executor는 `FIX_10_REPORT.md`를 새로 작성하고 기존 문서를 수정하지 않는다. 수정별 회귀·전체 Worker suite·ruff·diff 결과와 운영 미검증을 기록한다. 비선택 7사/전체 serving, hash/root/lock/원자성/재개/GC pin 계약을 유지한다. 이번 리뷰 요청 범위에서 배포·원격 쓰기·운영 원장 수정·배치/timer 실행은 하지 않는다.

사용자 지시에 따라 018은 `../018_worker-manual-live-validation/CLOSURE.md`로 종료하고 남은 운영 검증은 019로 이관했다. 이 종료는 8사 실검증이나 timer 재개 완료를 의미하지 않는다.
