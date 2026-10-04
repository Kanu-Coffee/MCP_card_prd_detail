# FIX_03 — 운영 인수 승인, 새 버전 발행 및 보고 상태 마무리

작성: Reviewer, 2026-10-04 16:16 KST. 대상: `PLAN.md`, `REPORT.md`, `FIX_01.md`/`FIX_01_REPORT.md`, `FIX_02.md`/`FIX_02_REPORT.md`, 원격 `main` `f4df758` 및 실제 운영 상태. 기존 handoff 문서는 수정하지 않는다. Executor는 수행 후 `FIX_03_REPORT.md`를 새로 작성한다.

## 판정

**004의 기능·운영 인수는 승인한다.** 최종 이미지 `sha256:285326…`의 서로 다른 timer 기동 배치 `707da1b3…`(09:00)와 `d91f20a0…`(11:47 catch-up)이 각각 exit 0, DB `succeeded`, publish `ready`로 완료됐다. 최신 세대 `g-d91f20a0…`가 MCP에 무중단 활성화됐고 `/health/ready=true`다. 타이머는 임시 drop-in 없이 매일 03:00 설정이며 실제 다음 발화는 2026-10-05 03:00 KST다. Reviewer가 16:10에 본 `/` 가용 공간은 **114 GiB**로 80 GiB 하한을 넘는다. 원격 GC off와 기존 WebDAV OCR/Paddle 15건 검증은 앞선 판정대로 허용한다. 이 항목 때문에 OCR, 2회 배치, MCP 전환을 다시 수행하지 않는다.

원격 `main`은 `f4df758`로 fast-forward됐고, **해당 SHA의 [GitHub CI run 37184360691](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37184360691)이 성공**했다. 따라서 `FIX_02`의 Git 병합·CI 조건도 충족됐다.

**004의 최종 문서상 완료 판정은 보류한다.** `PLAN.md` §5와 `FIX_02.md`는 검증 뒤 새 버전 발행을 요구한다. 16:16 현재 원격 최신 GitHub Release와 태그는 여전히 `v1.0.29`; 세 패키지의 `pyproject.toml`도 `1.0.29`다. `FIX_02_REPORT.md`는 수용 기준을 모두 완료했다고 쓰지만 신규 태그·Release·배포 이미지 발행 증거가 없다. `.github/workflows/release.yml`은 `test "$version" = "1.0.29"`와 v1.0.29 전용 봉인 증거를 요구하므로, v1.0.30 태그만 즉석에서 만드는 방식도 기존 릴리스 계약을 충족하지 못한다. 기존 정상 운영을 막지는 않되 이 누락을 최종 완료로 간주하지 않는다.

## 필요한 조치

1. **새 릴리스 절차를 완료한다.** 현재 후보 이미지의 `org.opencontainers.image.version=v1.0.30-candidate`와 최신 공개 v1.0.29를 고려하여 다음 버전 `v1.0.30`을 기준으로 세 패키지 버전·잠금 파일·증거를 준비한다. 기존 v1.0.29 태그/Release를 이동하거나 삭제하지 않는다. 저장소의 릴리스 워크플로가 v1.0.29 전용인 이유와 필수 증거 계약을 읽고, v1.0.30에 필요한 봉인 평가·후보 수용·OCI 출처·CI 결속을 유지하는 변경을 검증한다. 보안·출처 게이트를 생략한 수동 GitHub Release로 대체하지 않는다. 새 릴리스 커밋 CI 성공, 태그가 그 커밋을 가리키는지, GitHub Release와 이미지 다이제스트/게시 결과가 맞는지 확인한다. 이 작업은 이미 수용한 운영 Worker/MCP를 자동 교체하는 요구가 아니다.
2. **실행 이미지와 잠금 파일의 차이를 정확히 보고한다.** 최종 무인 검증 Worker 이미지는 `1ba366d`에서 빌드됐다. 이후 `uv.lock`은 PyJWT를 2.13.0에서 2.15.1로 올렸지만 현재 실행 중 MCP 컨테이너의 설치 버전은 2.13.0이다. `FIX_02_REPORT.md` §7의 “이후 변경은 CI 환경 무결성만”이라는 설명은 의존성 변경 범위를 축소한다. 현재 MCP의 HTTP 인증은 `apps/cardrag-mcp/src/cardrag_mcp/app.py`에서 정적 Bearer 값을 `secrets.compare_digest`로 검사하며 JWT 검증 경로는 확인되지 않아, 이 차이를 운영 인수 차단 사유로 보지 않는다. 새 릴리스 이미지의 실제 의존성 버전과 운영 중인 이전 이미지의 관계를 `FIX_03_REPORT.md`에 명시한다.
3. `FIX_03_REPORT.md`에 두 정상 배치·main CI는 이미 수용됐음을 간결히 인용하고, 이번 라운드의 신규 버전, 태그 객체/대상 커밋, CI와 릴리스 워크플로 결과, 공개 Release URL, Worker/MCP 이미지 다이제스트, 운영 이미지와의 관계를 기록한다. 릴리스가 실패했으면 완료로 쓰지 말고 실패 단계와 복구 가능한 다음 조치를 기록한다. 디스크 수치는 측정 시각과 함께 쓴다.

## 수용 기준

- 기존 v1.0.29 공개 이력은 보존되고 v1.0.30의 버전·태그·봉인 증거·릴리스 게이트·GitHub Release/이미지 결과가 서로 일치한다.
- 릴리스 대상 커밋의 GitHub CI가 통과하며 새 릴리스 결과를 `FIX_03_REPORT.md`에서 재현 가능하게 대조할 수 있다.
- 수용된 운영 timer/MCP/WebDAV 상태는 유지된다. 새로운 릴리스 작업을 이유로 두 무인 배치나 OCR 복원성을 다시 요구하지 않는다.
