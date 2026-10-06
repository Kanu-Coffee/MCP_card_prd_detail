# FIX_03 — 전체 후보 배치 없이 1.0.32 공개 발행 마무리

## Reviewer 결정과 목표

`FIX_02_REPORT.md`의 중단 결과를 수용한다. 이번 005 과제는 **공개 이미지·GitHub Release 발행**이지 운영 stable 이미지 전환이나 5,514건 corpus 재처리가 아니다. 004의 운영 인수와 2026-10-06 03:00 운영 run은 *현재 운영 시스템*의 기능 참고 증거이며, 1.0.32 후보 Worker가 실행 성공했다는 증거는 아니다. 따라서 `FIX_01`/`FIX_02`에서 요구한 신규 후보 generation·Worker `succeeded`·OCR 공급자 호출·MCP 12도구·rollback 5단계·13파일 readiness receipt를 **이번 공개 발행의 필수 게이트에서 철회**한다. 이 연구·운영 전환 검증기는 저장소에 남겨 선택적 후속 검증에 사용한다. 새 릴리스가 운영에 자동 배포되지는 않는다.

**이 수정은 사용자에게 자원 부담을 준 과한 게이트를 바로잡는 것이다.** source commit/이미지 digest/CI/OCI 출처·SBOM/strict scan/immutable Docker Hub tag/cosign 서명/asset checksum/공식 workflow 성공은 그대로 필수다. 수행하지 않은 후보 full run, 답변 품질 gold, legacy 비교는 README/릴리스 노트에 명시한다. 공개 발행 상태를 운영 배포 승인으로 표시하지 않는다.

## Executor 실행 범위 — 이 순서만 수행

1. `main`/원격 tag·Docker Hub·GitHub Release·작업 트리를 확인한다. `v1.0.32`가 비어 있으면 유지한다. 기존 `c4a53b6`의 CI success와 Worker/MCP GHCR 후보 이미지 digest를 출발점으로 사용하되, 아래 코드/문서 변경 후에는 **새 final source commit**으로 두 이미지를 다시 한 번 빌드한다. 현재 후보 state 볼륨은 제거됐다. 재생성하지 않는다. `/tmp/opencode/rr/receipt.py`, `rollout.py`, `run_worker.sh`는 이번 계약에 사용하지 않는다.
2. `.github/workflows/release.yml`의 필수 13파일 `release-readiness` 경로를 **경량 공개 발행 증거**로 일관 교체한다. Dispatch 입력은 `release_readiness_sha256` 대신 `release_qualification_sha256`로 통일한다. `release-evidence/v1.0.32/release-qualification.json` 한 파일(`schema_version=cardrag.release-qualification.v1`)의 canonical JSON·SHA-256·크기·중복 키·symlink/path traversal·추가 파일 거부 및 source/tag/image digest 결속을 validate→publish 전 재검증→release asset/SHA256SUMS 전 단계에 적용한다. 새 검증기를 별도 작은 모듈로 구현하거나 기존 workflow 코드로 구현하되, 허위 `candidate_worker_succeeded`·`candidate_generation`·`passed=true`를 채우지 않는다. 이 파일은 `release_version`, final source commit, 두 후보 OCI index digest, CI run URL/commit/conclusion, **참고용** 운영 run `03fbc4f18a3c450bb017e2fd6f6442c4`/generation `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`/운영 이미지 source `1ba366db8029c6947c0509c2d480ed6182cf996b` 및 다음의 명시적 미수행 항목을 담는다: `candidate_worker_full_run`, `candidate_mcp_12_tools`, `gold_quality_evaluation`, `production_cutover`. 운영 run의 source가 final candidate source와 다름을 그대로 기록한다. Workflow는 외부 운영 run을 후보 런타임 성공으로 검증·주장하지 않고, 공개 발행 게이트는 GitHub CI와 OCI·보안·서명 증거에 둔다.
3. `docs/RELEASING.md`와 README/Release notes를 위 계약에 맞춘다. 1.0.32 발행에 full volume clone, PDF discovery, PaddleOCR, 외부 OCR/embedding, 신규 WebDAV generation, 후보 채널 게시·rollback을 요구하지 않는다. 004 운영 인수 및 03:00 실적은 출처가 다른 참고 증거라고 표시한다. 운영 이미지 전환과 새 후보 full run은 별도 작업으로 남긴다. `candidate_acceptance`, `release_readiness`, 연구 평가 코드/테스트는 삭제하거나 느슨하게 만들지 않는다. 필요하지 않은 기능 코드 수정은 하지 않는다.
4. 관련 release gate 테스트와 workflow YAML/내장 스크립트 파싱을 갱신한다. 특히 다른 source/tag/image digest, CI 미통과, receipt 변조·추가 파일·symlink, 이미 존재하는 Docker Hub immutable tag를 거부하는 검사를 유지한다. 최종 소스 commit의 CI 전체 성공을 확인하고, Worker/MCP 이미지를 **최종 commit 원격 Git context**로 빌드해 기존 OCI provenance/SBOM·strict 보안 검사와 exact digest 검증을 통과시킨다. 이미지 시작/버전 확인은 `--network none`의 짧은 읽기 전용 명령으로만 수행한다. Docker Hub secret 이름은 등록돼 있으나 게시 권한은 workflow에서 실증한다.
5. tag 이전에 공식 workflow와 같은 검증기로 비게시 preflight를 수행한다. source 다음에는 `release-qualification.json` **증거만 추가한 봉인 commit**을 만든다. 두 commit의 CI를 확인한 뒤 annotated `v1.0.32` tag를 최초 1회 생성·push하고 `release.yml`을 실제 receipt SHA와 두 image index digest로 dispatch한다. Docker Hub Worker/MCP, cosign 서명·attestation, GitHub Release asset/SHA256SUMS의 원격 결과를 대조한다. 실패하면 태그를 이동하지 않고 단계·원인을 보고한다.

## 실행 금지와 비용 상한

- `cardrag-worker run`/`resume`, 5,514건 discovery/PDF 재검증, Paddle OCR, OCR cache 쓰기, WebDAV candidate/stable 포인터 쓰기, 새로운 48GiB Worker volume clone, 운영 timer·MCP 재기동은 하지 않는다. 이번 release workflow도 이를 요구하지 않아야 한다.
- 공개 증거에 secret, 사설 URL, 원본 PDF/OCR, 운영 로그 원문, 개인 질의 또는 `/tmp`의 임시 스크립트·토큰을 넣지 않는다. Hash만으로 실험을 했다고 주장하지 않는다.
- 로컬 디스크 여유가 64GiB 아래로 내려가면 빌드를 멈추고 원인을 조사한다. 광범위한 `docker system prune --volumes`나 운영 볼륨 삭제는 하지 않는다. 같은 CI/OCI 빌드를 의미 없이 반복하지 않는다.
- `v1.0.30`/`v1.0.31` tag, 기존 공개 릴리스, 운영 stable 이미지·OCR 자료는 보존한다.

## 인수 기준과 보고

`FIX_03_REPORT.md`에는 final source·봉인 commit, CI URL, Worker/MCP 후보 및 공개 digest, qualification 파일 SHA, official workflow URL, Docker Hub/GitHub Release URL, 서명·SBOM·checksum 검증 결과, 실제 실행한 검증과 **미수행 runtime/품질 검증**을 분리해 적는다. 실패 또는 발행 미완료면 005 완료로 쓰지 않고 정확한 차단점만 남긴다. Executor는 이 FIX에 없는 추가 전체 런타임 실험을 스스로 발명하지 않는다.
