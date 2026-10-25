# 019 REVIEW_04 — FIX_14 보완 필요

2026-10-20 Reviewer: Codex. PLAN/PLAN_AMENDMENT_01, REPORT와 FIX_01–14/REPORT 및 운영 리뷰의 계약, 현재 Git status/diff와 실제 소스를 대조했다. HEAD `aaeb2c4` 위 기존 미커밋 작업을 보존했다. **FIX_14 전체 승인은 보류하고 FIX_15를 발행한다.** BC 원인 수정은 확인했으나 백업 scope에 잘못된 완료 판정이 남는다. 활성 release `/opt/cardrag/019-e7fb8a0ba60c`는 유지했으며 배포·배치·timer 기동·운영 원장/원격 쓰기·삭제를 하지 않았다.

## 확인한 수정

실제 `ocr.py` 변경은 정규화된 `cache_kind == "content"`일 때만 prior.variant_id 또는 OCR SHA를 variant로 전달한다. native/adopted에는 None을 전달한다. 보고서 1.1의 예제(prior.reuse_key를 variant로 사용)는 실제 코드와 다르므로 예제를 구현 사실로 사용하면 안 된다.

이전 BC 운영 PDF/prior를 실제 이미지에 연결하고 최신 ocr.py 한 파일만 RO overlay하여 network-none으로 재검증했다. 결과는 native, variant_id=None, 원본 OCR SHA `c3a3556d7b80a20d69f1cd8ca4e329c73ef4e223e6cb1f509b978d55013eb3f8`, provider_called=False였다. 원본 state는 RO mount, 출력은 임시 경로다. BC 국소 오류는 해결된 것으로 판단한다. 아직 새 이미지의 BC 배치 완주는 아니다.

## [P1] content 문서의 현재 소유 metadata 소실을 정상 제외로 오판

`backup.py:1265–1266`은 content/adopted의 태그 rebind를 일괄 건너뛴다. `1330–1333`은 현재 native bundle 검증에 실패해 doc_has_bound_metadata=False가 되면 해당 문서의 모든 태그를 shared cache로 제외한다. **현재 문서 소유 bundle을 잃은 상태도 과거 공용 cache와 동일하게 처리**된다.

Executor가 추가한 `test_samsung_content_run_with_verified_native_bundle_preserves_bundle`을 그대로 실행해 fixture를 만든 후 scope를 확인하고, 임시 fixture의 native-manifest.json만 제거한 다음 scope/flush를 다시 호출했다. 제품/운영 데이터는 변경하지 않았다.

```text
VALID_BUNDLE       scope=6 bound=4 unbound=2 excluded=0
LOST_OWNED_BUNDLE  scope=2 bound=2 unbound=0 excluded=5
FLUSH_AFTER_METADATA_LOSS
  backup_scope_complete=true
  in_scope_unbound=0
  pending_out_of_scope=2
  remaining_pending=2
  excluded_shared_cache=5
```

manifest가 정상일 때도 현재 bundle과 다른 key의 과거 태그 2개가 unbound로 남는다. 기존 테스트는 현재 bundle 경로 포함 여부만 확인하여 이 오탐을 놓쳤다. manifest가 소실되면 이번에는 현재 bundle의 pending metadata 2개를 범위 밖으로 밀어내 완료=True로 판정한다. 현재 소유 metadata는 검증 복구하거나 미확인 상태로 완료를 막아야 한다. 과거 shared cache 제외 정책 자체를 철회하라는 요구가 아니다.

제외 카운트도 scope에 남는 OCR CAS doc_map을 제외로 세어 5가 된다. 실제 required 경로와 제외 경로가 겹치지 않도록 진단 의미를 바로잡아야 한다.

## [P2] 배포/조회/rollback 명령이 현 배포 구성과 불일치

FIX_14_REPORT 4절의 `deploy/worker/Dockerfile`은 없다. 실제 Dockerfile은 repo root이며 `--target worker`도 필요하다. `git rev-parse --short HEAD`만 사용하면 현재처럼 같은 HEAD 위 미커밋 수정들을 구분하지 못한다. operations/private overlay 보존 및 current 전환 절차도 제시되지 않아 준비한 새 이미지로 실행된다는 보장이 없다.

`docker run ... -v cardrag-worker-state:/state:ro ... backup status`는 실제 state 경로 `/var/lib/cardrag-worker`와 설정/인증을 전달하지 않는다. status는 RO 진단이므로 `backup_scope_complete`는 null이며 이 명령에서 True를 요구하는 주석도 틀리다.

rollback의 `worker-compose.sh up -d`는 worker `command: ["run"]`을 실행한다. 수동 카드사 검증 중 timer를 비활성으로 유지하는 경계에서 rollback이 즉시 배치를 기동해서는 안 된다. 실제 실행하지 않고 compose/Dockerfile과 명령만 대조했다.

## [P2] 현재 DB와 다른 과거 DB를 다음 복구점에 재사용하도록 안내

문서는 불변 DB 재사용 원칙을 설명하지만 실제 보호 집합·의존성·정리 후보 목록과 확보량을 검증하지 않았다. REPORT는 다음 배포에서 특정 과거 worker DB를 참조하고 backup ledger만 새로 보존하도록 지시한다. 현재 운영 DB와 그 DB는 이미 다르다.

```text
current worker-state.sqlite3                              7305965568 bytes
deployment-backups/019-ba273106158e/worker-state.sqlite3   7291777024 bytes
```

사이에 우리/KB/삼성/BC 실행이 있으므로 코드 패치 배포라는 이유로 과거 DB를 현재 복구점으로 간주할 수 없다. 현재 일관된 snapshot과 내용 동일성이 입증된 경우에만 재사용하고, 다르면 새 snapshot을 보존해야 한다. 단순 파일 크기만 같다고 동일성을 증명할 수도 없다. 약7GB 중복 방지는 같은 내용일 때의 최적화이며 정상 복구점보다 우선하지 않는다.

## 검증

- `uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_06_scoped_backup.py apps/cardrag-worker/tests/test_019_unresolved_history.py apps/cardrag-worker/tests/test_fix_14_bc_samsung_backup.py -q`: **104 passed, 6.04s**.
- 변경 관련 backup/ocr/cli 및 신규 테스트에 `ruff check`: 통과. `ruff format --check`: **신규 테스트 1파일 미포맷으로 실패**, 소스 3파일은 통과. REPORT에는 format 검사 통과 근거가 없다.
- `git diff --check`: 통과.
- 위 추가 fixture 재현과 실제 BC RO 검증을 별도로 수행했다. 재현 스크립트는 `/tmp/review_fix14_repro.py`, `/tmp/review_bc_fix14.py`이며 임시 진단 도구다. 전체 Worker/Core 테스트는 보고서의 1474/267 통과 기록을 참고하되 이번 리뷰에서 재실행했다고 주장하지 않는다.

FIX_15에서 백업 판정과 운영 인계를 보완한 뒤 재검토한다. 기존 FIX_14_REPORT는 이력으로 유지하며 잘못된 명령을 실행하지 않는다.
