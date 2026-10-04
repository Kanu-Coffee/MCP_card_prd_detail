# FIX_01 — 공개 릴리스 검증 기준을 운영 기능 증거로 전환

## Reviewer 판정과 승인된 범위 변경

`REPORT.md`의 Step 1은 수용한다. `f5b12d0a1deae7d3c849b027d62f37a85bb47be3`의 1.0.32 버전 계약과 CI run `37210571160`은 성공했고, 승인 gold 및 frozen `v109_baseline`이 없어 정지한 판단은 당시 PLAN에 부합한다. 그러나 **005의 목표인 공식 공개 릴리스는 미완료**다. 사용자는 2026-10-05에 선택지 B, 즉 과거 gold·legacy 비교 연구를 이번 릴리스의 필수 게이트에서 분리하고 실제 운영 기능에 맞는 검증으로 진행하는 방안을 명시적으로 선택했다. 이 FIX는 그 결정에 따라 `PLAN.md`의 gold/56파일/5-lane 필수 조건과 이에 종속된 수용 기준만 대체한다. 기존 PLAN·REPORT를 고치거나 덮어쓰지 않는다.

현재 `main`은 `e3101dfe51a41ef4c4f87c0a905c4fa4d07f38cd`, 작업 트리는 clean, 원격 `v1.0.32` tag와 Release는 아직 없고 최신 공개 Release는 `v1.0.29`다. 따라서 **1.0.32를 계속 사용할 수 있으나**, 릴리스 워크플로·검증기 변경 뒤 `f5b12d0`은 최종 candidate source commit이 아니다. 새 소스 커밋에서 이미지와 증거를 다시 결속한다. `v1.0.30`·`v1.0.31`을 이동하거나 삭제하지 않는다. `dockerhub-public` 환경에는 `DOCKERHUB_USERNAME`·`DOCKERHUB_TOKEN` secret 이름이 등록돼 있다(현재 API 재확인 가능). 실제 게시 권한은 공식 preflight/publish에서 검증한다.

2026-10-05 03:00 KST 운영 예약 실행은 run `31afd3b00b2548b9a9755f79c8e3aa7a`, generation `g-31afd3b00b2548b9a9755f79-36bae25dd8cd`, terminal `succeeded`, OCR 캐시 재사용 5,512건으로 끝났다. 이 결과는 **현재 운영 이미지**의 안정성 증거다. 새 1.0.32 후보 이미지의 실행 결과로 대체해 주장하지 않는다. 운영 timer·MCP·WebDAV stable 상태를 유지하고 03:00 예약 배치 2회를 다시 요구하지 않는다.

## 변경할 릴리스 계약

1. 릴리스 목적을 ‘현재 지원하는 Worker/MCP를 재현 가능하고 안전하게 공개 발행’으로 명시한다. 이번 릴리스는 300~500개 승인 gold 질의, `v109_baseline` 및 5-lane 품질 비교, 통계적 품질 우위, 답변 품질 보증을 **수행했다고 주장하지 않는다**. `docs/EVALUATION.md`의 연구 평가 기능과 검증기는 삭제·무력화하지 않고 선택적 후속 품질 평가로 유지한다. README, `docs/RELEASING.md`, GitHub Release 본문에서 수행한 기능 검증과 미수행 품질 비교를 명확히 구분한다.
2. 기존 `.github/workflows/release.yml`의 `acceptance_report_sha256`·`aggregation_profile_sha256`·`capture_set_receipt_sha256` 입력, 56개 portable gold 파일 강제, `gold_capture`·`evaluation` 재생 검증은 이번 발행의 필수 경로에서 제거한다. 이름만 바꾼 가짜 gold/fixture를 넣지 않는다. 대신 **현재 후보의 공개 가능한 release-readiness 증거**를 요구한다. 후보 source commit, 1.0.32 버전, Worker/MCP OCI index·platform/config digest, 후보 generation 및 manifest/READY hash, terminal Worker 결과, OCR 재사용/새 공급자 호출, MCP 12도구의 실제 discovery·호출 결과와 response schema/generation 결속, baseline 복귀, stable 포인터·공유 OCR cache 불변을 서로 결속한다. 실패·미실행을 `passed=true`로 적지 않는다. 기존 `cardrag_mcp.candidate_smoke`/`candidate_acceptance`의 gold와 독립적인 검사를 재사용할 수 있으면 우선 재사용한다. 해당 모델의 필수 필드가 품질 연구 산출물에 종속된다면 연구 receipt를 허위 작성하지 말고 별도 좁은 release-readiness schema/validator를 만든다.
3. 새 공개 번들은 `release-evidence/v1.0.32/`에 명시적인 allowlist로 봉인한다. 원본 운영 로그·인증정보·사설 URL·비공개 PDF/OCR 본문·개인 질의와 56개 연구 파일을 공개 Release asset에 싣지 않는다. 공개 증거에는 검증 가능한 비밀 없는 수치, 상태, 제한된 테스트 요청/응답 또는 그 검증 가능한 digest와 source/image/generation 결속을 담는다. 비공개 원본의 hash만으로 통과를 주장할 수 없다면 실제 검증은 공개 가능한 별도 후보 데이터에서 재실행한다. 파일의 canonical JSON, 크기, SHA-256, 중복 키, symlink/path traversal, 허용되지 않은 추가 파일, 커밋 간 변경 범위를 검사한다. 새 manifest schema와 검증기는 **검증 실패 시 발행을 중단**해야 한다.
4. 후보는 최종 source commit의 원격 Git context에서 GHCR에 Worker/MCP를 빌드한다. OCI provenance/SBOM, 고정 build args, 공개 GHCR package 소유·visibility, exact digest, strict filesystem/image secret·취약점 검사, Docker Hub immutable tag preflight, digest 그대로 복사, cosign 서명·attestation·asset checksum 및 원격 재검증은 기존 강한 게이트를 유지한다. 운영 볼륨/DB에 새 writer를 붙이지 않고 격리 state·candidate channel에서 **후보 1회**를 실제 실행한다. terminal 성공/검증된 `no_change`와 게시 READY, MCP `/health/ready`, 기본 12도구의 실제 호출·검색 근거, 변경 후 재시작·baseline 복귀를 확인한다. 반복 정기 배치 2회는 요구하지 않는다.
5. 워크플로의 `validate` → portable evidence upload → `publish` 전 재검증 → `release` 조립·asset 검증까지 **모든** 연구 파일 참조를 새 allowlist/manifest로 일관되게 바꾼다. 현재 약 3,300행짜리 workflow의 중간 한 곳만 우회해 뒤 단계가 실패하거나, publish 전에 증거 검증 없이 넘어가도록 하지 않는다. `tests/runtime_v1/test_v110_release_readiness.py`, `test_candidate_acceptance_release_gate.py` 및 관련 검사에서 과거 gold 필수 주장을 새 계약으로 교체한다. 누락·변조 증거, 다른 source/image/generation, 실패한 Worker/MCP 호출, stable 변경, 기존 Docker Hub tag 충돌을 거부하는 회귀를 추가한다. 기존 연구 평가 자체의 검증 테스트는 유지한다.

## 실행 순서와 태그 관리

1. `main`/원격 tag·Release·Docker Hub 충돌, 운영 상태, 환경 secret 이름을 재확인한다. 위 계약 변경을 코드·문서·테스트에 구현하고 CI(린트·타입·runtime·Compose·이미지·보안)를 통과시킨다. 수정된 validator 설치는 고정 `uv 0.8.17`과 실제 `--all-packages --no-dev` 경로로 확인한다. 그 **새 커밋**을 candidate source commit으로 고정한다.
2. 그 커밋의 두 후보 이미지를 빌드·스캔하고 격리 기능 실사를 수행한다. 공개 가능성 검토를 거친 readiness 증거를 만들고, 공식 워크플로와 같은 검증기를 사용한 **발행 전 비게시 preflight**로 입력·증거·후보 digest를 확인한다. tag를 먼저 만들어 검사하는 방식은 피한다. 후보 검증 후 코드·workflow·의존성이 바뀌면 새 source commit과 영향을 받는 검증을 다시 수행한다.
3. source commit 다음의 **증거만 추가한 commit**에 allowlist 파일을 봉인한다. 차이에 다른 경로가 없음을 확인하고 두 커밋의 CI를 확인한 뒤 처음으로 annotated `v1.0.32` tag를 그 봉인 commit에 생성·게시한다. 이미 tag가 생겼다면 이동하지 않고 다음 미사용 버전으로 전환한다.
4. `v1.0.32` tag에서 공식 `release.yml`을 실제 receipt SHA와 Worker/MCP digest로 dispatch한다. Docker Hub와 GitHub Release가 발행되면 tag commit, source commit, 이미지 digest, 서명·SBOM·provenance, 공개 asset checksum 및 Release URL을 대조한다. 실패 시 발생 단계와 실제 원인을 보고하고 공개 완료라고 쓰지 않는다. 소스 수정이 필요한 실패는 기존 tag를 보존하고 다음 미사용 버전으로 재시도한다. 이 과정은 운영 stable 이미지 자동 교체를 포함하지 않는다.

## 수용 기준과 보고

- 공개 GitHub Release와 Docker Hub Worker/MCP 이미지가 **공식 workflow 성공 결과**로 존재하며 같은 불변 source·후보 digest·봉인 tag에 결속되고, 기존 서명·출처·SBOM·strict scan·remote checksum 게이트가 통과한다.
- 현재 후보의 격리 Worker terminal 결과, generation READY, OCR 재사용과 공급자 호출, MCP 12도구 호출, 안정 포인터·캐시 불변 및 baseline 복귀가 실제 증거로 확인된다. 운영 03:00 결과는 별도 참고 자료로 구분한다.
- 공개 문서/Release는 연구 gold·legacy 비교를 이번 버전에서 **미실행**으로 표시한다. 합성 fixture나 이전 evidence를 현재 실사처럼 쓰지 않고 비밀·비공개 원문을 공개하지 않는다.
- `FIX_01_REPORT.md`에 새 source·봉인 commit, candidate/public image digest, CI/workflow run 및 URL, preflight·운영 격리 결과, 공개 bundle 파일/해시, 문서상 제한, 미완료 항목을 기록한다. 외부 자료·권한 문제로 공식 발행이 끝나지 않았다면 완료라 쓰지 않고 정확한 남은 입력과 다음 행동을 보고한다.
