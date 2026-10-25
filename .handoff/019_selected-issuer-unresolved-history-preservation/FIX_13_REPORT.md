# 019 FIX_13_REPORT — variant 증거 검증과 실제 CLI 복구 경로 보완 실행

작성: 2026-10-20, Executor OpenCode (qwen3.8-flash). `FIX_13.md`의 P1 세 건을
구현하고 Reviewer 임시 재현(A/B/C)을 저장소 영구 회귀로 이관했다. 기존
handoff 문서는 수정하지 않았고, 이 파일만 신규 산출물이다.
Git 기준: HEAD `aaeb2c4` 위의 019 미커밋 작업 트리(기존 변경 보존).
운영 배포·원격 쓰기·운영 원장 수정·provider/배치/timer 실행은 이번 턴에서 수행하지
않았다(FIX_13 §"산출물과 다음 운영 검증" 준수). 코드 변경 파일:
`apps/cardrag-worker/src/cardrag_worker/backup.py`, `cli.py`, `partial_cli.py`,
`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`.

## 1. 구현 내용

### 1.1 [P1-1] doc_info만으로 손상 receipt를 정상 variant로 인정함 — 해결

`backup.py:_is_valid_remote_ocr_variant` 전면 재작성:

- 검증 권위는 **receipt에 바인딩된 실제 바이트**만 갖는다. 후보 receipt 행
  (SHA, size)마다 로컬.spool/(SHA 파일) 및 로컬 OCR cache
  (`cache/ocr/{key}/manifest.json`, `variants/*/manifest.json`)에서
  ① 바이트 SHA-256과 size가 receipt와 일치하고, ② canonical 파싱
  (`canonical_bytes()==body`)이며, ③ reuse_key와 sealed PDF SHA에 바인딩되고,
  ④ 출력이 신규 본체와 다르고, ⑤ **그 manifest로부터 파생한 canonical READY
  바이트의 (SHA, size)가 현 root의 READY receipt 행과 일치**하며, ⑥ 로컬에
  존재하는 READY 파일 바이트가 파생본과 일치할 때만 variant로 인정한다.
- `doc_info` fallback(구 4단계)과 테스트 전용 `client.objects` 조회 경로를
  삭제했다. doc_info/doc_map은 탐색·index 항목 메타 가시성 보조로만 남고
  검증 권위가 없다.
- manifest/READY receipt 조회를 모두 `remote_path = ? AND remote_root = ?`로
  제한하고 `fetchone()`을 폐기했다. 동일 root 다중 SHA 행은 임의 선택이 아니라
  **행별로 개별Corroboration**하며 어느 행도 입증되지 않으면 미확인 보류(canonical
  경로 유지 → flush에서 conflict/missing으로 보고)한다.
- 로컬 증거 소실 시: 동기 스코프 계산에서 임의 GET을 발명하는 대신
  **"명확한 미확인 보류"** 분기를 선택했다. 원격 pair의 GET 검증은 정식 읽기
  경로(`backup restore`/`audit`)가 담당하며, restore가 A pair를 로컬 cache+receipt로
  복원하면 이후 재시도가 바이트로 인증한다(재현 C 회귀가 이 순서를 CLI e2e로 검증).
  손상/불명 receipt는 정상 variant로 분류되지 않는다. 실제 source/contract/output
  variant 공존(재현 통과 조합)은 계속 지원한다.
- **동일 OCR body·다른 canonical manifest** 정책: 명시적 보류(variant 라우팅 거부).
  CAS 본체는 공유되므로 충돌 경로를 무조건 재사용하지 않고 conflict로 운영자
  검토에 넘긴다.
- `remote_root` 파라미터를 `resolve_ocr_bundle_paths`,
  `verified_document_ocr_bundles`, `scoped_backup_scope`, `scope_remote_paths`에
  thread-through하고, record/scope/diagnostics/CLI/partial-CLI 호출부가
  `_canonical_remote_root(settings)`(공개别名 `canonical_remote_root`)를 같은
  근거로 쓰도록 해 **등록과 scope의 라우팅 근거를 일치**시켰다.
- root 변경 시 물리 덮어쓰기 방지: `_scoped_integrity`와 flush 수송 루프에
  **foreign-root 충돌 가드**를 추가했다. 다른 root receipt가 같은 경로를 다른
  SHA로 주장하면 수송을 차단하고 `in_scope_conflicts`로 보고하며, 기존 원격
  바이트는 절대 덮어쓰지 않는다(같은 SHA면 기존대로 완료 근거가 아니다).
  잘못된 기존 variant 주장이 index에 재발행되는 것도 expected 바인딩 필터로
  차단(회귀에서 검증).

### 1.2 [P1-2] 실제 `backup flush --run`이 새 variant를 등록하지 않음 — 해결

`cli.py:backup_flush_command`가 `--run` 지정 시 scope 계산 앞에
`ledger.record_run_success(run_id, state_dir, settings, selected_issuers=selected)`를
자체 호출한다. 보존된 seal/run 산출물과 검증 근거(cache/spool)만으로 현재 run의
variant metadata를 **정식 등록**하고(멱등: receipt/pending·recorded_runs 기존 행
건드리지 않음, `is_new_run` False라 `runs_since_backup`도 변동 없음), provider·
OCR·수집 재실행, SQL 삭제, receipt 조작, seal rewrite는 일절 없다. CLI e2e
회귀(`test_cli_backup_flush_run_registers_and_transports_variant_live_like`)는
재현 C가 지적한 실제 운영 형태 — pending 0, A spool 없음, doc_info는 B, A
remote pair+root receipt만 존재, index에서 A 누락 — 에서 recorder 선행 호출·
비운영 hook 없이 WebDAVClient.from_env 수송만 mock해 다음을 확인한다:
B variant trio+CAS+PDF 전송, A 바이트 무변경, index에 A 회수+B 등록,
`backup_scope_complete=true`, 두 번째 CLI 재시도는 `unchanged`·전송 0·멱등.

### 1.3 [P1-3] pending 없는 index 복구가 실제 client 생성 경로에서 미실행 — 해결

`flush()` 재구조화:

- injected → `_make_webdav_client` hook → `WebDAVClient.from_env`(자격증완비 시,
  self-owned)의 **표준 client 생성·수명(close)·lock·예산 사슬**을 pending-less
  분기에서도 동일하게 사용한다. CLI 일반 경로는 주입/hook이 없고 from_env만
  존재하므로, 이제 pending 0·`force`에서도 정식 복구 probe가 실행된다
  (재현 B等价 회귀: monkeypatched from_env만으로 drift 회수·성공 확인, 재실행은
  unchanged·무쓰기).
- 새 `_remote_index_repair_needed()`: **읽히거나 파싱된 committed index만** 판정
  대상. index 부재 → 회수 대상 아님(신규 원격의 첫 index는 실제 전송 배치 생성,
  기존 `test_live_like_generation_only_bundle_is_flushed_and_idempotent` 멱등
  semantics 유지), 파싱 실패·transport 오류 → 어떤 write도 하지 않는다
  (회귀로 3 상태 모두 검증: 무puts·index 원본 보존).
- 회수 진행 시 artifact body 전송 없이 선택 범위 밖 업로드로 대신하지 않고
  (pending 0이므로 루프 공전) receipt 증거로 index만 재발행한다. commit 실패/
  중단/재시도는 기존 회귀(`test_scoped_flush_index_commit_failure...`)가 계속
  커버하고, 정상 기완료 unchanged는 신규 재시도 assert로 강화했다.

## 2. 실행 명령과 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1731 passed, 10 warnings in 59.30s   # Reviewer 시점 1719 + 신규 FIX_13 회귀 12건

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format packages/cardrag-core/ apps/cardrag-worker/
2 files reformatted                  # Reviewer가 지적한 backup.py·test_fix_06의
uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
153 files already formatted          # 포맷 FAIL 시정(파일 포맷 준수)

git diff --check
pass
```

Reviewer 임시 재현 파일(비영구) `/tmp/cardrag019-review13/test_review13.py`는
결함 단정 문서를 그대로 재실행해 **3건 모두 실패로 전환**됨을 확인했다(재현 A:
metadata 라우팅 차단 / 재현 B: factory 경로 복구 실행 / 재현 C: CLI가 B 전송·
complete=true). 동일 시나리오는 위 12건의 저장소 상시 회귀로 이관했다:

- baseline 양(+) 경로, 재현 A(forged doc_info + 위조 manifest/READY receipt 보류·
  index 재발행 금지·원격 바이트 무변경), size 불일치, READY 짝 불일치, 다중
  receipt 행 개별 corroboration, 동일 body·다른 manifest 보류, spool 소실+cache
  만으로 공존 인정, cache에 현 revision만 있는 경우 미인증, foreign-root
  receipt 미인증+덮어쓰기 차단, 재현 B(factory 복구+멱등 unchanged), index
  없음/손상/읽기 실패 refuse, 재현 C(CLI end-to-end 등록+전송+A 회수+complete+
  2차 멱등).

## 3. 운영 절차 정정 (FIX_13 지시 반영)

`FIX_12_REPORT.md` §4의 `/opt/cardrag/state/backup-ledger.sqlite3` 예시는 실제
named-volume 구조와 맞지 않으므로 아래로 정정한다(운영 원장은 컨테이너
`worker-state:/var/lib/cardrag-worker`, `CARDRAG_WORKER_STATE_DIR`
= `/var/lib/cardrag-worker`, compose.yaml 기준).

```bash
# 1) 정식 CLI 읽기 전용 선택 스코프 점검(원격 호출 없음)
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup status --run <RUN_ID> --issuer <code>

# 2) 원장 raw 대조(읽기 전용): worker 컨테이너의 정식 state 경로
#    (CARDRAG_WORKER_STATE_DIR=/var/lib/cardrag-worker, external named volume
#    cardrag-worker-state를 compose가 마운트)에서 mode=ro 접속만 사용한다.
#    별도 -v 마운트 중복을 피하기 위해 서비스 자체 마운트를 그대로 쓰고,
#    SQLite mode=ro로 커밋·WAL 쓰기를 원천 차단한다.
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  python -c "
import sqlite3
conn = sqlite3.connect('file:/var/lib/cardrag-worker/backup-ledger.sqlite3?mode=ro', uri=True)
key = '<reuse_key>'
for row in conn.execute(
    'SELECT remote_path, sha256, size_bytes FROM backup_receipts WHERE remote_path LIKE ?',
    (f'%{key}%',)):
    print(row)
"

# 3) 정식 회수·완료 절차(SQL 삭제·receipt 조작 없이)
/opt/cardrag/current/operations/worker-compose.sh run --rm --no-deps worker \
  backup flush --run <RUN_ID> --issuer <code>
```

3번이 이번 구현으로 A pair 검증(필요시 restore가 로컬 증거를 먼저 복원)→ B
variant 등록 → scope/expected 확정 → 전송 → index 회수까지 실제 호출 경로로
수행한다. image entrypoint가 `cardrag-worker`이므로 CLI 이름 반복이 필요 없다
(docs/OPERATIONS.md §backup 절차와 동일 형식).

## 4. 계획 deviation / 판단 기록

- 로컬 증거 소실 시 GET 검증은 "명확한 미확인으로 보류"로 구현했다(§1.1).
  동기 스코프 계산에서 await할 수 없고, 기존 `client.objects` 조회는 비운영
  injection이라 권위를 제거하는 것이 FIX_13 요구("비운영 factory hook으로 문제를
  가리지 않는다")와 정합적이며, restore/audit이 정식 읽기 경로로서 GET+SHA/size
  검증을 수행한다.
- index 문서 자체가 없는 상태는 transport-less recovery로 생성하지 않는다(기존
  멱등 unchanged 계약 유지). drift(존재하나 불일치)만 복구한다.
- `--run`에서 seal 없는 config_error 경로에서도 record가 선행되어
  `backup_recorded_runs`에 run 행이 남을 수 있다(무seal 시 등록 물품 없음,
  runs_since 증가 없음). 무해하나 문서로 남긴다.
- completion probe가 injected/hook/from_env 공통 사슬을 사용하도록 통일했다
  (기존 from_env 전용에서 확장; hook client는 소유권 없음 = 기존 close 계약 불변).

## 5. 남은 리스크 / 다음 단계

- 운영 원장(현 `/opt/cardrag/019-ba273106158e` 활성 release)에는 이번 코드가
  아직 배포되지 않았다. Reviewer가 FIX_13 판정과 위 회귀 증거를 통과시키면,
  사용자가 이번 턴에 승인한 범위(운영 반영 → 백업 실테스트)를 이어간다. 그때
  확인할 항목: A canonical pair 불변, B metadata/CAS 복원 가능성, A/B
  index·receipt, 선택 complete, 두 번째 재시도 멱등, serving 5537/우리 980
  보존, pending 정상 별도 상태 유지.
- foreign-root 가드는 물리 버킷 공유 가정에 기반한다. 다중 root가 실제 분리된
  저장소인 환경에서는 conflict가 나도 전송 차단이 안전 측면 오버다(차단은 안전,
  해제 시 운영자 판단 필요) — 운영 실테스트에서 root 문자열 실물 확인 권장.
