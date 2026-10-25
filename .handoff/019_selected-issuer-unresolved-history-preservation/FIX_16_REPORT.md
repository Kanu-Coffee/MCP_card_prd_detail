# 019 FIX_16_REPORT — 소유 백업 의무 보존·RO 조회·운영 전환 준비

2026-10-20 Executor: Codex. FIX_16.md와 REVIEW_05 및 019 PLAN/변경계획·이전 FIX/REPORT 계약을 기준으로 구현했다. HEAD `aaeb2c4` 위 기존 미커밋 변경과 과거 문서를 유지했다. **운영 배포, 이미지 빌드, 실제 DB snapshot/복구/수정, 원격 쓰기, BC 배치, timer 기동, 파일 삭제는 하지 않았다.** 로컬 fixture에는 의도적으로 행 삭제·lost_source 전환·receipt 생성을 수행했다. 운영 자료에는 RO mount/network-none만 사용했다.

## 1. 변경

### 1.1 전송 상태와 소유 의무를 분리

`backup.py`에 `_record_owned_ocr_paths`를 추가했다. 검증된 문서 소유 native bundle을 등록하는 세 경로(선택 run recorder, 전체 recorder, 즉시 document OCR recorder)가 기존 backup_meta에 run/doc/key별 필수 manifest/READY 경로와 sealed PDF/OCR identity를 보존한다. 별도 schema migration은 없다.

이 기록은 **required 경로의 가시성**만 제공한다. 기록의 문자열·doc_info·receipt로 expected SHA/size를 만들거나 전송을 허용하지 않는다. scope는 기존 run-local bundle/cache의 독립 바이트 검증으로만 expected를 바인딩한다. 현재 seal의 문서/PDF/OCR에 일치하는 의무는 전송 행이 pending/lost_source/receipt-only이거나 아예 없어도 paths에 남는다. 증거가 소실되면 unbound로 완료를 막는다. 손상된 의무 기록도 조용히 성공으로 전환하지 않고 unverified-ownership 경로로 보류한다.

기존 원장의 의무 기록 없는 경우도 pending과 **lost_source 모두** 보수적 가시성을 유지한다. 정상 삼성 content 전환의 과거 native shared-cache 2건은 현재 run 소유 의무로 새로 추정하지 않으므로 정상 제외를 유지한다. 같은 경로의 오래된 metadata만으로 새 업로드를 강제하지 않는다.

현재 정상 native bundle 및 cache 재바인딩, stale revision/variant, 다른 issuer/root의 기존 검증은 유지했다. `ocr.py`의 BC 수정은 이번에 바꾸지 않았다.

### 1.2 RO helper와 진단 초기화

scope 진입 시 메모리 진단 상태를 안전하게 초기화한다. DB 생성자를 호출하지 않는 기존 RO subclass도 AttributeError 없이 사용할 수 있다. 없는 run 조회에서도 제외 수를 0으로 재설정해 이전 run의 진단값이 섞이지 않는다. 실제 operations monitor를 변경하거나 운영 release 파일을 덮어쓸 필요가 없었다.

### 1.3 실행 가능한 운영 준비

- `docs/OPERATIONS.md`: 컨테이너 내부 glob 확장·RO 용량 조회, SQLite WAL/online backup, 두 lock 유지, 현재/이전 snapshot 내용 동일성 조건과 시점/의존성 기록을 반영했다. 코드 패치라는 이유로 옛 DB를 재사용하는 안내를 제거했다.
- `deploy/worker/snapshot-lock.py`: 명시적 유지보수 컨테이너용 도구. Worker/backup flock을 비차단 획득한 뒤 두 DB를 online backup하고 quick_check/hash/size/latest run/stable 증거를 남긴다. ready 후에도 두 lock을 계속 보유한다. 바쁜 lock이나 실패한 snapshot은 ready가 되지 않는다. hold-only 모드는 코드 복귀의 fence용이며 DB snapshot/배치를 실행하지 않는다.
- `FIX_16_OPERATIONS.md`: 기존 host operations 전체 복사, tracked/untracked frozen 입력 digest, root Dockerfile worker target, 설치 소스 대조, private overlay의 release/image 갱신, deployment metadata, 새 snapshot과 lock holder를 유지한 activation, 기동 없는 복귀, 새 성공 BC run의 status/flush 절차를 연결했다. 과거 고정 label overlay 예시를 그대로 쓰지 않는다. shell 명령은 구문 검증했으며 운영에서 실행 완료했다고 주장하지 않는다.

새 도구는 기존 복구본을 덮거나 정리하지 않는다. 실패 중 생긴 partial snapshot은 ready/evidence 없는 복구점이므로 운영자가 완료로 표시하지 않도록 문서화했다. 다음 배포에는 현재와 다른 과거 DB를 재사용하지 않고 새 snapshot을 준비한다. 사용자가 요청한 운영 공간 정리 자체는 실행하지 않았다.

## 2. 회귀 및 검증 결과

### 제품/운영 도구 테스트

`test_fix_14_bc_samsung_backup.py`에 실제 CLI/factory 경로 테스트를 추가했다. 동일 현재 소유 metadata의 lost_source·대기 행 삭제·receipt-only·ownership 기록 없는 legacy lost_source, 재시도, 다른 issuer/root에서도 required/unbound 2건을 유지하고 complete=False를 확인한다. fixture factory만 대체했으며 운영 provider/원격 호출은 없다. source와 spool을 실제로 제거하여 단순 lost_source 표기 후 자동 회복되는 조건과 구분했다. 새 ledger 인스턴스로 재조회해 메모리 상태에 의존하지 않는 것도 검증했다.

실제 RO SQLite subclass를 통한 조회, 없는 run의 진단 reset과 DB bytes 불변을 검사했다. `test_fix_16_snapshot_lock.py` 4건은 열린 WAL의 committed 데이터 백업, snapshot 재사용 거부, 두 lock 각각의 충돌 거부, ready 이후 lock 유지와 종료 후 해제를 검사한다. snapshot은 작고 격리된 임시 DB만 사용했다.

최종 명령/결과:

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1752 passed, 10 warnings in 57.71s

uv run --no-sync ruff check apps packages deploy/worker/snapshot-lock.py
All checks passed!

uv run --no-sync ruff format --check apps packages deploy/worker/snapshot-lock.py
222 files already formatted

git diff --check
통과
```

관련 백업/019 세트는 추가 운영 도구 테스트 전 111건 통과(6.16s), snapshot 도구 4건 별도 통과(0.12s) 후 전체 1752건으로 최종 확인했다. 최종 전체 실행의 10 warnings는 기존 OCR 손상/원격 variant 충돌·fallback 회귀의 경고다. 문서의 Bash 블록 5개는 `bash -n` 통과. 수정 코드·신규 테스트·snapshot 도구·운영 가이드 약196KB를 gitleaks stdin/redact로 검사하여 누출 0건이다.

### 실제 삼성 RO 대조

운영 이미지 `cardrag-worker:019-e7fb8a0ba60c`에 최신 backup.py만 RO overlay하고 state/serving/live 및 기존 monitor-state.py를 RO mount, network-none으로 연결했다. **기존 실제 monitor의 status samsung.json 호출이 exit0**이었다. 새 helper로 대체하거나 DB initializer를 호출하지 않았다.

```text
allowed=2085, bound=2085, excluded_shared_cache=2
remaining=0, lost_source=0, conflicts=0, missing=0, unbound=0
pending_out_of_scope=0
```

원격 index/body를 이번에 새로 읽지 않았다. RO status는 completion=null이며 위 일치가 새로운 실제 flush 완료의 증명은 아니다. host 임시 증거: `/tmp/fix16-samsung-monitor.json`, `/tmp/fix16-scope-ro.py`.

stable은 삼성 성공 세대 `g-44d55df9db8645c8b9060dc4-a7a46a5bb5e6`, 비선택 문서 변경0, 전체5543이다. **현재 manifest의 정확한 카드사별 수**는 하나735·삼성766·롯데647·우리980·신한931·현대605·KB757·BC122다. 이전 일부 리뷰/보고서의 같은 숫자에 붙은 하나/신한 및 롯데/현대 명칭은 잘못 매핑돼 있었다. 실제 manifest·문서를 바꾼 것이 아니라 이 보고서에서 명칭을 정정한다.

## 3. 식별과 잔여 범위

현재 worker/core Python 소스 80개의 경로+NUL+bytes+NUL SHA256:
`d6685263a45526e28a4a3b1db2fc43ab2bd99cede49403f7f9a9b2bf9ad5b6a8`.
이는 **작업 트리 소스 식별**이며 설치된 운영 이미지 digest가 아니다. 새 배포 때는 helper/lockfile 등 전체 실제 build 입력을 고정하는 FIX_16_OPERATIONS 절차를 사용한다.

- 과거 의무 기록이 없고 run-local/cache/전송 행 등 소유 근거까지 모두 사라진 content/adopted의 태그만으로 현재 소유였다고 소급 추정할 수 없다. 이 경우 승인된 shared-cache 제외 정책을 유지한다. 새 검증 등록부터 의무 기록을 보존하고 legacy pending/lost_source는 보수적으로 보호한다. 운영 원장을 일괄 추측 보정하지 않았다.
- 새 ownership 기록은 소형 ledger metadata이며 body 사본을 추가하지 않는다. 기존 ledger metadata 보존 체계에 남는다. run/serving/MCP 전면 GC 변경이나 새 자동 정리 서비스는 추가하지 않았다.
- 조회 시 filesystem 여유 약76GiB(80% 사용), timer/service inactive였다. 새 snapshot 약7.3GB와 다음 run/serving/MCP 임시 공간은 실제 배포 전에 다시 확보·관측해야 한다. 복구본 정리 후보의 약13.6GiB를 실제 회수 완료로 표시하지 않는다.
- 새 root Dockerfile 빌드, 이미지 설치 소스 확인, 실제 snapshot/activation/rollback은 Reviewer 통과 후 수행할 운영 단계다. 도구의 fixture 검증·문서 구문 검증과 혼동하지 않는다. 새 BC 배치 완주와 삼성 원격 backup completion도 아직 남아 있다.

Executor 구현·검증을 마쳤으며 Reviewer 재검토 대상으로 인계한다. 이번 보고서만으로 운영 적용·019 전체 검증·timer 재개 완료를 선언하지 않는다.
