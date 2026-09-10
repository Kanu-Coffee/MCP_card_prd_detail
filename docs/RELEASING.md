# 릴리스와 stable 전환

소스 준비, 공개 이미지 발행, 운영 전환은 각각 다른 단계입니다. 브랜치 업데이트나 CI 통과만으로
실행 중인 Worker가 새 버전으로 바뀌지 않습니다. 릴리스 workflow도 호스트의 설치 경로,
WebDAV stable 포인터, systemd 예약이나 클라이언트 설정을 변경하지 않습니다.
현재 릴리스 검증기는 소프트웨어 **1.0.22**의 후보 증빙을 검증합니다.

## 실행 중인 Worker 보호

전환 전까지 기존 Worker의 이미지, 컨테이너, 상태·인증 볼륨, env, 설치 symlink와 예약을
유지합니다. 실행 중인 SQLite를 외부 프로세스에서 열거나 상태를 복사하지 않습니다.
다른 writer 때문에 거부된 예약 실행만 보고 기존 작업을 중단하지 않습니다.

먼저 컨테이너와 systemd 상태, 해당 실행의 구조화된 로그로 실제 writer를 식별합니다.
Worker 종료 코드와 terminal 결과, 게시 완료 여부를 확인합니다. `succeeded`와 `no_change`,
`failed`와 `interrupted`는 구분하며, 컨테이너가 사라졌다는 사실만으로 게시 성공을 추정하지
않습니다. DB 검증·snapshot은 writer가 완전히 종료하고 새 writer가 진입하지 않는 정비
구간에서 [복구 절차](RECOVERY.md)에 따라 수행합니다.

사전 준비 중에는 로컬 소스 정리, 단위 테스트, Compose 렌더링, 증빙 검증기 점검과
설정 초안 작성만으로 진행할 수 있습니다. image build나 후보 평가도 실제 운영 자원을
공유하면 영향을 줄 수 있으므로 별도 자원에서 수행하거나 실행 종료 후 진행합니다.

## 검증할 소스 고정

1. Worker, MCP, core의 패키지 버전과 lockfile을 맞추고 변경 내용을 검토합니다.
2. lint·type·runtime·Compose·이미지·보안 CI가 통과한 정확한 commit을 선택합니다.
3. 그 commit에서 Worker/MCP 후보 이미지를 빌드하고 OCI index·platform manifest·config
   digest, SBOM, provenance를 기록합니다. 빌드 후 검증 없이 다른 이미지를 대체하지 않습니다.
4. 후보 검증이 끝나면 소스를 다시 수정하지 않습니다. 코드·의존성·Dockerfile·배포 계약을
   수정하면 새 commit과 이미지로 필요한 검증을 다시 수행합니다.

배포 설정과 운영 검증의 원본은 저장소 밖에 보관합니다. 공개 가능한 증빙을 별도로 생성한
뒤 source commit 다음의 **증빙만 추가하는 commit**에 `release-evidence/v1.0.22/`로
봉인합니다. Workflow는 두 commit 사이에서 이 경로 밖의 변경을 거부합니다.
문서 정리나 버전 변경도 이 이후에 섞지 않습니다.

## 격리 후보의 계약

| 항목 | 후보 설정 |
|---|---|
| Compose project | `cardrag-v122-candidate` |
| Worker 상태 | `cardrag-worker-v122-candidate-state` |
| Codex 인증 | `cardrag-worker-v122-candidate-codex-home` |
| MCP 상태 | `cardrag-mcp-v122-candidate-state` |
| MCP host port | `127.0.0.1:18022` |
| WebDAV channel | `candidate-v1.0.11` |

`candidate-v1.0.11`은 호환성을 유지하는 채널 이름이며 설치할 패키지 버전이 아닙니다.
후보는 선택한 canonical WebDAV root의 별도 포인터를 사용하며 stable에 게시하지 않습니다.
공유 OCR cache는 읽기 전용이고 원격 GC도 금지됩니다. 읽기 seed가 필요한 경우에도
source writer가 종료하고 검증된 호환 상태만 read-only로 연결합니다.

후보 overlay는 `CARDRAG_CANDIDATE_WORKER_IMAGE_DIGEST`,
`CARDRAG_CANDIDATE_MCP_IMAGE_DIGEST`의 immutable index digest를 요구하며 local build
fallback을 제거합니다. `CARDRAG_CANDIDATE_IMAGE_REPOSITORY`로 사용할 GHCR 저장소를
선택할 수 있습니다. 실행 중인 다른 후보가 같은 채널을 사용한다면 병행 게시하지 않습니다.

후보 빌드는 로컬 작업 디렉터리 대신 `https://github.com/<owner>/<repo>.git#<commit>`의
정확한 Git context를 사용합니다. `linux/amd64`, 대상 `worker` 또는 `mcp`, SBOM과
`mode=max` provenance가 필요합니다. 검증 계약은 BuildKit 0.32.2와 Syft 1.51.0 및
고정한 `docker/buildkit-syft-scanner` digest를 요구합니다. Scanner digest와
OCI index·linux/amd64 image·별도 attestation manifest 형식도 아래 검증기를 따릅니다. Tag는
`candidate-v1.0.22-<role>-<40자리 commit>` 형식을 사용합니다.
`APP_VERSION=1.0.22`, `VCS_REF=<commit>`,
`SOURCE_URL=https://github.com/<owner>/<repo>`를 명시적으로 전달하십시오.
`CODEX_VERSION`, `CODEX_SHA256`, `PYTHON_DEV_IMAGE`, `PYTHON_RUNTIME_IMAGE`, `UV_IMAGE`도
Dockerfile의 고정값과 같은 build 인자로 전달해야 합니다. 허용되는 정확한 전체 인자는
[provenance 검증기](../.github/scripts/validate-candidate-provenance.jq)의
`expected_build_args`에 정의되어 있습니다. 임의 build 인자·다른 context·의도하지 않은 secret
mount는 검증에서 거부합니다. 일반 개발용 `docker compose build` 결과는 이 release
provenance 계약을 충족했다고 간주하지 않습니다.

## 필요한 증빙

[평가 안내](EVALUATION.md)의 전체 gold·capture·집계·답변 검증을 수행합니다. 품질 비교
schema의 `v109_baseline` 같은 이름은 frozen 평가 입력의 식별자입니다. 이전 운영자만
보유한 자료나 과거 실사 보고서를 현재 검증 대신 사용하지 않습니다. 해당 비교 입력이
없으면 검증 미완료로 남으며 release gate를 우회하지 않습니다.

후보 receipt는 다음 자료를 실제 source commit·generation·이미지와 결속합니다.

- 유효 설정, 별도 상태·인증 경로, namespace와 변경 권한
- terminal Worker 실행, 정확한 OCR·구조·임베딩·출판 지표와 원격 객체 검증
- 기본 MCP 도구 12개의 discovery와 실제 호출 요청·응답, exact 검색 coverage
- 현재 baseline 보존, 후보의 게시·재시작·baseline 복귀와 재활성화 검증
- 공유 OCR cache와 stable 포인터의 불변성, 허용되지 않은 쓰기·삭제 0건
- generation manifest, 집계 프로파일, gold와 원문 근거를 묶는 전체 평가 파일

공개 릴리스는 `cardrag_mcp.candidate_smoke`를 진입점으로 사용합니다. 이 검증기는
`cardrag_core.candidate_acceptance`의 canonical JSON·파일 크기·SHA-256·
source·image·generation 결속 검증과 실제 MCP 응답 모델 검증을 함께 수행합니다. 실행 환경에 맞는 값을 직접 측정해야 하며
`passed=true`를 작성하거나 다른 버전의 증빙을 이름만 바꿔 넣어 대체할 수 없습니다.
각 도구 응답은 MCP/JSON-RPC 봉투를 제거한 정규화 payload로 보존하며 request와 response의
canonical hash를 함께 기록합니다. 오류 응답·스키마 불일치·다른 generation은 거부합니다.
호출 원문에 인증 헤더, 사설 URL, 조사자의 입력, 비공개 데이터나 공개 권한 없는 PDF/OCR
본문이 포함되지 않았는지 **봉인 전에** 확인합니다. 공개하면 안 되는 입력은 공개 가능한
평가 corpus로 교체하고 검증을 다시 수행합니다. 봉인 후 내용을 지우고 기존 해시를 재사용하지
않습니다. Workflow는 봉인된 portable evidence도 release asset으로 공개합니다.

사용할 MCP 클라이언트에서도 실제 `tools/list`와 호출을 검증합니다. 카드사 별칭·잘못된
카드사명, 최근 출시·날짜 미확인, 기간·복수 카드사, 일괄 요약, generation 변경 오류와
근거 출처를 확인하십시오. 클라이언트의 health만으로 도구 동작을 합격 처리하지 않습니다.
테스트 질의가 실제 예약·메일 발송·외부 사용자 메시지를 실행하지 않도록 별도 테스트 경로를
사용합니다. 설치별 클라이언트 설정은 공개 release artifact에 넣지 않습니다.

## 공개 이미지 발행

`.github/workflows/release.yml`은 수동 `workflow_dispatch` 전용이며 **이미 존재하는
annotated `v1.0.22` tag**에서 실행합니다. Tag 생성·공개 이미지 업로드는 검증을 마친 뒤의
명시적인 릴리스 작업입니다. 일반 PR·브랜치 push는 발행하지 않습니다.

Fork의 유지관리자는 다음을 자신의 저장소에 설정합니다. 후보 GHCR package owner는
반드시 해당 GitHub 저장소의 owner와 같아야 합니다. Fork에서 upstream 후보 namespace를
그대로 쓰면 검증을 통과하지 않습니다. 공개 Docker Hub 대상에도 별도의 게시 권한이 필요합니다.

| GitHub 설정 | 용도 |
|---|---|
| Variable `CARDRAG_PUBLIC_IMAGE_REPOSITORY` | Docker Hub의 `owner/repository` |
| Variable `CARDRAG_CANDIDATE_IMAGE_REPOSITORY` | 공개 `ghcr.io/owner/package` 후보 저장소 |
| Environment `dockerhub-public` | 공개 발행 권한·보호 규칙 |
| Secret `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | 해당 공개 저장소 게시 인증 |

후보 패키지의 public visibility와 namespace 소유권도 검증합니다. fork의 OCI source,
provenance와 repository 설정은 실제 빌드 원본과 일치해야 합니다.

Dispatch의 필수 입력은 `version=1.0.22`, `candidate_source_commit`,
`acceptance_report_sha256`, `aggregation_profile_sha256`, `capture_set_receipt_sha256`,
`candidate_acceptance_sha256`, `candidate_worker_image_digest`,
`candidate_mcp_image_digest`입니다. 모두 검증한 실제 파일·이미지에서 얻습니다.

Workflow는 source·annotated tag·CI·receipt·portable evidence, 공개 후보 package,
OCI/SBOM/provenance와 strict 보안 검사를 확인한 뒤 **동일 digest**를 공개 저장소로 복사합니다.
서명·attestation·release asset checksum과 원격 자산을 다시 검증합니다. 누락된 증빙,
다른 source·image·version 또는 충돌하는 immutable tag는 실패로 처리합니다.

## 운영 stable 전환

자연 종료한 Worker의 terminal 결과를 확인하고 운영자가 정한 전환 구간에 들어갑니다.
기존 서비스를 중단해 억지로 사전 조건을 만들지 않습니다.

1. 종료된 상태의 검증된 백업, 기존 image digest·env·symlink·timer·channel identity를
   저장소 밖에 보존합니다. 새 예약이 시작되지 않도록 전환 구간의 실행 진입을 관리합니다.
2. 검증된 release를 새 설치 디렉터리에 준비합니다. 기존 볼륨 재사용 또는 별도 offline
   copy 중 선택한 방식을 명시하고 Worker·MCP·인증의 목적지를 각각 확인합니다.
3. 새 MCP가 기존 generation을 제공하는지 먼저 확인합니다. 요청·출처·인증과
   클라이언트 호출을 검증하고 오류 시 준비한 이전 MCP로 복구합니다.
4. Worker와 Codex 인증의 설정·볼륨·권한을 확인한 뒤 새 Worker 설치 경로와 실행 설정을
   반영합니다. Stable 게시 허용은 이 시점의 결정이며 cache 쓰기·GC 허용과 별개입니다.
5. 다음 실행의 결과와 새 generation을 확인한 뒤 예약 상태를 확정합니다. 기존 복구 자산은
   새 실행과 rollback 검증이 완료될 때까지 보존합니다.

새 Compose 기본 볼륨을 지정했다고 기존 데이터가 이동하는 것은 아닙니다. Candidate
state를 live 상태에서 복사하거나 인증과 Worker DB를 같은 볼륨에 합치지 않습니다.
정리할 Git 브랜치는 보존 branch의 ancestry를 확인하고, 미포함 commit은 별도 보존 후
처리합니다. 이미 발행한 immutable tag와 image digest는 복구·출처 검증에 필요하므로
브랜치 정리와 분리해 관리합니다.
