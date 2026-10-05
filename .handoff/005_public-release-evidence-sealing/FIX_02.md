# FIX_02 — 압축된 운영 상태에서 격리 후보를 준비하고 공개 릴리스 완료

## Reviewer 판정

`FIX_01_REPORT.md`의 릴리스 계약 변경과 후보 OCI 검증은 부분 수용한다. `e6ccc82`와 `435a99f`의 CI는 통과했고, 보고서도 실사·태그·공개 발행을 완료했다고 주장하지 않는다. **005 전체 인수는 보류한다.** `v1.0.32` annotated tag, Docker Hub 이미지, GitHub Release, 후보 실구동과 봉인된 readiness 증거가 아직 없기 때문이다. 현재 공개 최신 Release는 `v1.0.29`다. 이번 FIX는 사용자 선택 B(연구 gold 없이 운영 기능으로 공개 릴리스)와 `FIX_01`의 발행 목표를 계속 수행한다. 새벽 03:00 예약 배치 2회를 다시 요구하지 않는다.

차단 원인은 운영 DB 자체의 결함으로 단정하지 않는다. 운영 상태는 rolling compaction 이후 snapshot의 문서별 discovery 기록을 보존하지 않는데, `seed-state-v122`는 모든 문서의 원본 `SourceRecord`를 snapshot에서 찾도록 구현되어 있다. 읽기 전용 실측에서 2026-10-06 운영 acquisition의 고유 source ID 5,501개는 모두 durable `pdf_cache_source`에 존재하고 문서별 snapshot에는 없었다. 따라서 보고된 `source_record_missing`은 구조적으로 재현된다. 누락 필드를 추측해 SourceRecord나 snapshot 행을 만들거나 시드 검사를 약화해서 통과시키지 않는다. `seed-embedding-cache-v122`는 별도 경로이므로 동일 오류라고 추정하지 않는다.

현재 운영 Worker는 2026-10-06 05:36:43 KST에 exit 0으로 종료했고 timer는 active다. 상태 볼륨은 약 47.8GiB, 파일시스템 여유는 약 104GiB다. 이 수치는 격리 복제의 실행 가능성만 보여 주며, 복제 전 다시 확인한다. `dockerhub-public` 환경의 `DOCKERHUB_USERNAME` 및 `DOCKERHUB_TOKEN` **이름**은 Reviewer의 GitHub API 조회에서 확인됐다. 비밀 값이나 Docker Hub 실제 게시 권한은 확인하지 않았으며 공식 publish job에서 검증한다. `main`/`origin/main`은 `d21e77b`, 작업 트리는 clean, CI run `37386799175`는 success였다.

## 이번 수정: 검증된 오프라인 상태 복제로 후보 초기화

`FIX_01`의 빈 볼륨 + `seed-state-v122`/`seed-embedding-cache-v122` 순서와 `docs/RELEASING.md`의 수동 SQLite 복사 금지를 **이번 1.0.32 격리 후보 초기화에 한해서** 아래 절차로 대체한다. 이는 무검증 파일 복사를 허용하는 결정이 아니다. 저장소에 이미 있는 [`docs/RECOVERY.md`](../../docs/RECOVERY.md)의 writer 종료 후 전체 상태 복사와 `tools/cardrag_offline_volume_verify.py state` 전수 파일 hash·SQLite integrity 검증을 적용한다. 상태 전체를 일치 검증하면 임베딩 캐시를 별도 수동 이식하거나 가짜 seed ledger를 만들 필요가 없다.

1. 후보 준비 전 운영 Worker의 terminal 상태, DB의 WAL/SHM/journal 유무, 운영 timer·MCP·WebDAV stable/candidate 포인터, OCR cache 기준값, 볼륨 크기와 디스크 여유를 기록한다. 복제 중 원본에 새 writer가 들어오지 못하는 정비 구간을 확보하고, 필요한 경우 timer를 잠시 멈춘 뒤 동일 턴에 되돌려 다음 03:00 예약이 유지되는지 확인한다. 실행 중 DB 일반 파일 복사는 금지한다. 운영 원본은 read-only로 연결하고 삭제·수정하지 않는다.
2. 운영 Worker **상태 볼륨 전체**를 비어 있는 격리 `cardrag-worker-v122-candidate-state` 볼륨으로 복제한다. 목적지에 남은 이전 내용이 있다면 덮어쓰지 말고 별도 실패로 다룬다. 운영 원본과 기존 다른 후보 볼륨을 수정하지 않는다. `worker-state.sqlite3`만 떼어 복사하거나 WAL/SHM을 임의로 생략하지 않는다. 원본과 목적지의 파일 목록·크기·SHA-256, 소유권·권한과 DB integrity를 `tools/cardrag_offline_volume_verify.py state`로 확인한다. 검사 실패나 중간 원본 변경이 있으면 목적지를 폐기하고 원본을 보존한다. 실행 전·후 여유 공간을 확인하고 최소 32GiB 운영 여유를 유지한다.
3. 정확한 후보 이미지에서 복제 DB의 schema·최근 terminal run·generation, acquisition/manifest/READY 참조 및 PDF CAS·OCR 재사용·embedding cache가 읽히는지 확인한다. 후보 Worker와 MCP는 각각 격리된 상태·인증 볼륨과 candidate WebDAV channel만 사용하게 한다. MCP 상태 목적지는 `cardrag-mcp-v122-candidate-state`다. stable 채널 쓰기, 공유 OCR cache 쓰기 및 원격 GC는 계속 금지한다. 운영 인증 본문이나 상태 원문을 공개 증거에 싣지 않는다. clone에 남은 운영 run 기록을 1.0.32 후보 실행의 증거로 재사용하지 않는다.
4. 후보 Worker **신규 실행 1회**의 terminal 결과, generation READY와 candidate pointer, OCR 재사용/신규 공급자 호출, MCP 12도구 discovery·실호출, rollback 5단계와 baseline 복귀를 `FIX_01`대로 실측한다. 복제된 baseline과 새 후보 실행을 run ID·source commit·이미지 digest·generation으로 구분한다. `no_change`라면 그 결과가 readiness 모델의 신규 후보 generation·게시 근거를 실제로 충족하는지 확인하고, 충족하지 못하면 증거를 만들지 말고 원인을 기록한다. 운영 03:00 결과를 후보 실행으로 대체하지 않는다.

## 코드·문서와 발행 순서

`docs/RELEASING.md`의 후보 초기화 문단을 위 한정된 검증 복제 경로와 일치시키고, 공개 검증 진입점/dispatch 입력을 실제 구현인 `cardrag_mcp.release_readiness`·`release_readiness_sha256`로 고친다. `cardrag_core.candidate_acceptance` 및 연구 검증기 자체는 삭제하지 않는다. 그 밖의 코드 변경이 필요하면 변경 범위와 이유를 보고하고 관련 테스트를 수행한다. **문서 수정도 source commit을 바꾸므로**, 기존 `435a99f` 후보 이미지는 최종 source 증거로 사용하지 않는다. 새 source commit의 원격 Git context로 Worker/MCP를 다시 빌드하고 OCI provenance/SBOM·strict scan·CI·정확한 digest를 재검증한다.

검증된 복제로도 후보를 안전하게 실행할 수 없다면 SourceRecord를 허위 복원하거나 `FIX_01`의 readiness 검사를 낮추지 않는다. 실패 조건·실측 결과를 `FIX_02_REPORT.md`에 남기고 공개 태그를 만들지 않는다. 성공하면 `FIX_01`의 순서대로 공개 가능성을 심사한 13개 allowlist 증거를 preflight한 뒤 **증거만 추가한 봉인 commit**, annotated `v1.0.32` tag, 공식 `release.yml` dispatch를 진행한다. Docker Hub와 GitHub Release의 digest·서명·provenance·SBOM·asset checksum을 원격에서 대조한다. 기존 `v1.0.30`/`v1.0.31` tag는 이동·삭제하지 않고 운영 stable 이미지는 자동 전환하지 않는다.

## 인수 기준과 보고

- 오프라인 복제는 writer 없는 원본에서 만들어지고 전수 파일 hash·SQLite integrity 및 schema/참조 검사가 통과한다. 운영 timer·stable 포인터·공유 OCR cache는 작업 후 기준값과 일치한다.
- 최종 source commit에 결속된 새 후보 두 이미지와 신규 후보 실행·MCP 실호출·rollback 증거가 `release-readiness` 검증기를 통과한다. 운영 실적이나 복제된 과거 run은 신규 후보 실사로 세지 않는다.
- 봉인 commit과 tag, 공식 workflow 성공, Docker Hub Worker/MCP 및 GitHub Release가 동일 source·digest·SHA256SUMS에 결속된다. 품질 gold·legacy 비교 미수행 표시는 유지한다.
- `FIX_02_REPORT.md`에 복제 전제·검증 명령과 결과, source/봉인 commit, CI/workflow URL, 후보/공개 image digest, 증거 파일과 hash, 운영 불변 확인, 실패 시 정확한 차단점과 되돌림을 적는다. 발행이 끝나지 않았다면 005 완료로 보고하지 않는다.
