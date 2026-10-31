# 022 FIX_07 — v1.0.38 운영 이미지 출처·복구점 정합성 보완

작성일: 2026-10-31 KST  
역할: Reviewer  
판정: GitHub 발행·소스 검증 확인, 운영 인수는 아래 2건 보완 대기.

## 1. 검토와 통과 증거

FIX_06와 REPORT, 기존 REVIEW/FIX 이력, 현재 Git 상태와 tag, 실제 GitHub PR/release/CI, 운영 compose/image metadata/source 및 복구 안내를 대조했다. GitHub connector의 combined-status 응답은 statuses 빈 목록뿐이어서 이를 CI 성공으로 해석하지 않고 gh의 실제 Actions check/run을 확인했다.

- PR #49 merged, merge commit `99b5ba69fb5907be2218873c10bbb945d6181dcd`.
- PR CI `41143931129` success (head `7434b6495f4f1e116fea322fe90ceca7e330100d`). main push CI `41144473379`도 동일 merge commit에서 success.
- annotated tag object `024a134631870a8a9aa1987ce1b2986e4db97bbd`, peeled commit은 위 merge commit과 일치.
- 정식 GitHub release `https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.38`, draft=false/prerelease=false. 게시 UTC `2026-10-30T23:26:44Z`.
- main에는 릴리스 후 문서 전용 `0f274c3` commit이 있으며 tag를 이동하지 않았다.
- `cardrag-worker:v1.0.38` image ID `sha256:d1bd4bb35090056778d90c2a59480733655181918d3524e7d8d22ec9c4d3d511`. package core/worker 실제 버전 1.0.38. image 안 Worker 모든 `.py`와 `git show v1.0.38:...`의 SHA-256 대조에서 불일치 0.
- active release `/opt/cardrag/v1.0.38`, 실효 Worker compose는 해당 image 및 UID/GID10001 사용.
- MCP는 새 image가 빌드됐지만 **실행 중인 것은 기존 `cardrag-mcp:013-50a0129`, package 1.0.34**다. unchanged MCP를 유지한 것은 FIX_06의 의도와 맞으며 새 MCP까지 운영 전환 완료라고 해석하면 안 된다.
- Worker/MCP current generation은 `g-df29d8e5dbb34048942a5296-5a3a99224f92`로 유지됐고 live MCP OCR 페이지 2회 호출 성공. timer active/enabled, next 10/31 10:00 KST, 기존 worker service의 failed 표시는 10/30 과거 실행이다.
- startup-check는 ready이나 **quality_status=warning**, minimum cosine `0.9974565514265082` (gate .990, warning .999)다. 정책상 허용된 경고이며 passed와 구별한다.

Reviewer 직접 재실행:

```bash
uv run --no-sync --all-packages pytest -q
# 2893 passed, 11 warnings in 92.16s
uv run --no-sync --all-packages ruff check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1
# All checks passed!
uv run --no-sync --all-packages ruff format --check packages/cardrag-core apps/cardrag-worker apps/cardrag-mcp tests/runtime_v1
# 244 files already formatted
git diff --check
# 통과
```

신규 Trivy ignore는 공개 commit에서 확인했다. CI 통과를 별도 취약점 remediation으로 해석하지 않는다. Worker v1.0.38의 실제 runtime Python에는 pip/msgpack package와 msgpack import spec이 없음을 확인했다. 이 리뷰에서 CVE 자체의 전체 공급망 재감사나 mypy를 재실행한 것은 아니며, 후자는 성공 CI 증거를 참고했다.

## 2. [P2] 운영 image OCI version/revision이 기본값임

Worker 및 새 MCP image의 OCI labels가 각각 다음과 같다.

```text
org.opencontainers.image.version=dev
org.opencontainers.image.revision=unknown
```

image tag와 package version만 1.0.38이고, Dockerfile `APP_VERSION`/`VCS_REF` 출처 입력을 지정하지 않은 결과다. FIX_06 3.A/6의 최종 source/image provenance 정합성 요구에 미달한다.

수정 요구:

- 이미 공개한 Git tag/release를 이동하지 않는다. 로컬 image의 version=1.0.38, revision=`99b5ba69fb5907be2218873c10bbb945d6181dcd`, source URL이 실제 입력과 일치하게 한다.
- 최종 commit에서 필요한 build args로 빌드하거나, 검증된 기존 image의 source/package/lock 정합성을 근거로 명시적인 **metadata-only 파생 image**를 만들 수 있다. 후자의 경우 기존 image ID와 source 대조 증거를 남기고 clean rebuild/provenance attestation이라고 주장하지 않는다. tag를 몰래 덮어쓰지 말고 신규 식별자를 사용해 전후 ID를 기록한다.
- 실제 운영 compose, installed package/CLI 및 OCI labels를 함께 확인한다. 새 MCP image를 수정하더라도 실행 중 MCP를 버전 맞추기만을 위해 교체할 필요는 없다. source/Worker/MCP/build-only 각각의 버전을 보고한다.
- 정상 실행 중인 worker를 종료하지 않는다. timer 유지와 기존 deployment/worker lock을 지키고 예약이 시작됐으면 변경은 정지 구간에 수행한다.

## 3. [P2] 현재 배포 복구점이 재개 이전의 snapshot이며 rollback guidance도 불일치

`apply-v1.0.38.sh`는 manual-start/deployment lock만 잡고 옛 022 snapshot의 무결성 검증 뒤 전환한다. 현재 state와 옛 snapshot의 동등성 검사나 현재 snapshot 생성은 없다. FIX_06 6.4는 정비 구간 현재 snapshot을 요구했다.

보호 중인 snapshot은 10/30 22:37 KST 생성된 배포 전 복구본이며, latest_run=`df29...` **failed**, stable은 이전 BC generation이다. 그 뒤 우리카드 resume 성공으로 DB와 backup ledger가 변경됐다. 이 snapshot의 무결성 통과를 현재 배포 직전 상태와 동일하다는 증거로 사용할 수 없다. 역사 복구본으로서 유효한 것과 현재 상태의 복구점인 것은 별개다.

또한 현재 안내가 서로 다르다.

- `/opt/cardrag/v1.0.38/operations/recovery-point.json`: active=v1.0.38, code rollback=022.
- `/opt/cardrag/022-woori-session-auth-20261030/operations/recovery-point.json`: active=022, code rollback=021.

수정 요구:

1. 실행 중인 writer/backup이 없는 정비 구간에서 기존 worker.lock과 backup.lock을 모두 비차단 획득해 유지하고, 현재 worker DB/backup ledger를 SQLite online backup으로 일관되게 snapshot한다. snapshot 생성·정합성 검증 동안 예약 writer와 같은 lock으로 배제한다. timer를 disable하지 않는다.
2. quick_check/hash/size/WAL 상태, 생성 시점 latest run 및 serving generation을 검증한다. 현재 source/package 정합성과 별도로 DB restore 호환성은 검토 전 승인하지 않는다.
3. 최신 독립 snapshot 1개 정책을 유지한다. 새 검증을 완료하고 의존성과 안내를 전환한 뒤 옛 복구본을 정리한다. 새것과 옛것을 둘 다 장기 보존해 용량을 누적시키지 않는다.
4. active와 실제 code rollback release(022)의 recovery-point/retention을 동일하게 한다. 배포의 역사 snapshot 기록은 보존하되 현재 복구점 선택에서 혼동되지 않게 상태를 표시한다.
5. 수정 script는 실제 writer lock을 포함하고, warning 상태를 무조건 passed로 기록하는 `runtime_validation` 문구도 실제 결과에 맞춘다. 이미 수행된 배포를 되돌리거나 데이터 재수집할 필요는 없다.

## 4. 문서·결과물과 승인 범위

위 운영 보완은 기존 FIX_06의 승인 범위다. 배포·출처·복구점 정합성을 완성한 뒤 새 `FIX_07_REPORT.md`에 전후 image ID/labels, 보호 snapshot 증거, active/rollback 안내 일치, actual Worker/MCP version, startup 경고와 결과를 기록한다. 기존 문서/공개 tag를 고쳐 과거 검증을 소급하지 않는다.

소스 릴리스 자체는 확인됐으므로 재발행하거나 v1.0.39를 만들 이유는 없다. 신규 `RELEASE_REPORT_v1.0.38.md` 등 완료 기록에 PR/CI/tag/release와 이번 보완을 연결해 현행 index의 RELEASE_PENDING이 역사 시점의 상태임을 명확히 한다. 필요한 공개 문서 후속은 문서 전용 commit/PR로 처리한다.

이번 Reviewer는 위 운영 보완을 실행하지 않았다. 용량 정리는 별도 사용자 지시로 수행했으며 그 결과는 REVIEW_03.md에 기록한다.
