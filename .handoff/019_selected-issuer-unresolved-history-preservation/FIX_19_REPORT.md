# 019 FIX_19_REPORT — metadata pair 선택과 cache 복구 쓰기 경계 보완

작성: 2026-10-21, Executor OpenCode. `FIX_19.md`의 P2 두 건을 구현했다. REVIEW_07의
재현 두 건은 수정 후 각각 정상 canonical 복구/안전한 거부로 전환됨을 확인했다.
기존 handoff 문서는 보존했고 이 파일만 신규 산출물이다. 기준: HEAD `aaeb2c4` +
019 미커밋 작업 트리(FIX_01–18 변경 보존). 코드 변경:
`apps/cardrag-worker/src/cardrag_worker/backup.py`,
`apps/cardrag-worker/src/cardrag_worker/cli.py`,
`apps/cardrag-worker/tests/test_fix_06_scoped_backup.py`, `docs/OPERATIONS.md`.
이번 턴에서 운영 배포·cache 복원·scoped flush·원격 쓰기·배치/timer 기동·원장 수정은
수행하지 않았다. 019와 timer 비활성 상태를 유지한다.

## 1. [P2] 검증된 pair 단위 후보 선택 — 해결

`recover_verified_ocr_metadata`의 후보 탐색을 manifest 단위 중단 방식에서 **전체 pair
단위 검증**으로 바꿨다. 후보(variant, canonical)마다 다음을 모두 한 묶음으로 검사한다:

1. 원격 manifest GET + canonical 파싱 + `reuse_key`(contract+source 유도) 일치
2. 현재 seal의 PDF SHA/size/page, OCR SHA/size 일치
3. manifest bytes의 현 root receipt SHA/size 일치
4. manifest로부터 파생한 canonical READY와 원격 READY **바이트 일치**
5. READY의 현 root receipt SHA/size 일치
6. `resolve_ocr_bundle_paths`가 현재 scope에서 실제 선택하는 라우팅 경로와 후보 경로 일치

무효 후보는 원격/receipt를 수정하지 않고 다음 후보를 계속 평가한다. 한 후보의 manifest가
유효해도 READY/receipt/라우팅이 불완전하면 선택되지 않으므로, 부분 게시된 variant가
정상 canonical pair 복구를 막지 않는다. 반대로 canonical이 무효면 유효 variant를 쓴다.
모든 후보가 무효면 `partial`과 후보별 사유를 보고하고 unbound/완료 차단을 유지한다.
manifest와 READY를 서로 다른 후보에서 섞지 않는다.

REVIEW_07 재현 결과(수정 후):

```text
HEALTHY_CANONICAL_PLUS_INCOMPLETE_VARIANT
status=succeeded verified=1 unbound 2 -> 0
manifest_path=canonical error=None
DANGLING_SYMLINK
status=partial written_outside_cache=false pair=error
```

## 2. [P2] cache 경계·기존 바이트 보호 쓰기 — 해결

기존 `_r18` write_bytes 경로를 제거하고 기존 OCR cache의 no-follow/atomic 패턴을
재사용하는 `publish_cache_pair()`를 추가했다.

- **경계 검사**: `ocr.py:_open_beneath_directory`(state root에서 각 구성요소를
  O_NOFOLLOW|O_DIRECTORY로 열고 uid 확인)로 key/variants 디렉터리를 고정한다. leaf는
  `_read_nofollow_regular`로 regular/uid/nlink/size/read-consistency를 검사한다.
  dangling leaf symlink, key/variants/parent symlink, 비정상 leaf는 **따라가지 않고**
  오류로 보고한다.
- **no-replace 원자 게시**: 새 leaf는 O_CREAT|O_EXCL|O_NOFOLLOW 임시 파일에 쓰고
  fsync 후 `os.link`로 원자 게시한다(기존 파일 대체 없음). 게시 경쟁에서 다른 writer가
  먼저 만들었으면 바이트가 동일할 때만 `present`로 인정하고, 다르면 `conflict`로
  실패한다. 게시 후 최종 바이트를 다시 no-follow로 읽어 일치할 때만 성공을 보고한다.
  manifest 성공 후 READY 실패/충돌 시 이번 호출이 만든 leaf만 롤백한다.
- **기존 variant 보존**: canonical에 다른 variant(정상 regular 상이 바이트)나 부분
  쓰기가 있으면 덮어쓰지 않고 `variants/{ocr_sha}/`에 게시한다. variant 대상에도 상이
  바이트가 있으면 오류다. unreadable/unsafe/부분 쓰기를 "존재"만으로 성공 처리하지 않는다.
- **직렬화**: `state_dir/ocr-cache-recovery.lock`에 비차단 `flock`을 잡고 recovery와
  `restore`가 공유한다. 이미 다른 cache writer가 실행 중이면 `status=locked`(CLI exit 1)로
  쓰기 없이 반환한다. 일반 Worker 배치는 run-local artifact만 쓰고 `cache/ocr`를 쓰지
  않으므로 이 lock과 충돌하지 않는다(문서화).

## 3. 회귀 테스트 (신규 9건)

- 부분/다른 output variant + 정상 canonical → canonical 복구.
- 정상 variant + 무효 canonical → variant 복구·scope 재바인딩.
- 모든 후보 무효 → partial·후보별 오류.
- dangling leaf symlink, key 디렉터리 symlink, variants 디렉터리 symlink → 경계 밖 쓰기 0.
- variant 대상 기존 상이 파일 → 무덮어쓰기·오류.
- 디렉터리 permission deny → 거짓 성공 없음·파일 미생성.
- cache 쓰기 lock 점유 → `locked`·쓰기 0.
- CLI recover-metadata(부분 variant 포함) → canonical 복구 → flush complete.

기존 stale CAS scope, ownership/lost_source/receipt-only, variant 공존, FIX_18 recovery
회귀는 모두 유지된다.

## 4. 실행 명령과 결과

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1775 passed, 10 warnings in 57.51s   # FIX_18 시점 1766 + 신규 9건

uv run --no-sync ruff check packages/cardrag-core/ apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check packages/cardrag-core/ apps/cardrag-worker/
156 files already formatted

git diff --check
pass
```

Reviewer 재현 스크립트 `/tmp/review-fix18-repro.py` 재실행 결과는 §1과 같다
(불완전 variant → 정상 canonical 복구, dangling symlink → partial/경계 밖 쓰기 0).

## 5. 운영 baseline 정정

FIX_18_REPORT의 다음 검증 기준 `serving 5537/우리 980`은 오래된 값이다. REVIEW_07 지시대로
이 보고서의 기준은 LIVE_REVIEW_06의 **전체 5545, 우리 980**이며, 후속 운영 배포 시에는
실제 current generation/manifest와 볼륨 수치를 다시 식별한다. staging delta(현대
required -3, BC -1, recovery +4)는 실제 운영 전체 completion 주장이 아니다.

## 6. 남은 리스크 / 다음 단계

- 복구 lock은 recovery와 restore가 공유한다. 임의의 외부 스크립트가 lock을 무시하고
  cache에 직접 쓰는 경우까지 막지는 않지만, no-replace 게시·최종 바이트 재검증으로
  덮어쓰기/거짓 성공은 방지한다.
- `os.link`를 지원하지 않는 파일시스템에서는 안전하게 오류로 끝난다(대체 rename 없음).
- Reviewer 통과 후 기존 승인 범위에서 새 release와 현대/BC 검증 회수를 준비한다.
  확인: 현대/BC `required=bound`, `in_scope_unbound=0`, `backup_scope_complete=true`,
  원격 index 완료, A/B receipt·원격 객체 보존, 두 번째 재시도 멱등. 신한/롯데 batch
  완주·timer 재개·전체8사 인수는 이번 범위가 아니며 선언하지 않는다.