# 운영 복구·원격 GC·GitHub 릴리스·디스크 정리 계획

## 목적과 현재 판단 (2026-09-28 KST)

v1.0.29의 일일 운영은 유지하면서, 호스트가 갑자기 완전히 폐기되어도 **시간이 오래 걸리는 기존 OCR 결과만** GitHub 코드와 WebDAV에서 보존·재사용할 수 있는지 증명한다. 원격 GC와 GitHub 릴리스 표시를 바로잡고, 현재 운영 자료와 직전 정상 세대 1개를 제외한 과거 로컬 자료를 정리한다. 이 계획은 새 handoff 과제이며 기존 `.handoff/001_*` 문서는 변경하지 않는다.

**복구 계약:** 전체 시스템 폐기 시 OCR을 다시 수행하는 장시간 작업을 피하면 충분하다. 환경변수, 인증, PDF discovery, source lineage, 임베딩, MCP 색인은 새로 구성·계산해도 된다. 2026-09-28 03:00 실행의 `publish.json`에는 문서 5,207건 모두 OCR 참조(고유 OCR CAS 4,922개, PDF CAS 4,724개)가 있다. Paddle로 처리한 15건도 모두 최신 generation manifest에 각각 OCR CAS로 기재되어 있다. 이 15건은 인증된 WebDAV GET으로 원격 본문 전체 329,283 bytes를 읽고 각 SHA-256·크기를 대조해 **15/15 성공**했다. 다만 현재 Worker에는 generation manifest의 OCR 객체를 빈 state의 OCR ledger로 가져오는 검증된 명령이 없다. 공유 OCR cache가 `read-only`이고 Paddle 15건에는 cache binding이 없으므로, 복구 구현은 cache 조회만 믿지 말고 manifest의 문서별 OCR 참조를 사용해야 한다. WebDAV 접근 자격은 폐기 호스트 밖에서 재발급·설정할 수 있어야 한다.

## 확인한 기준 상태

| 항목 | 2026-09-28 실측 |
|---|---|
| 원격 `main` | `26a993b`; v1.0.29 코드 태그 `fdf87e6`을 포함 |
| `release/v1.0.29` | `3764845`; `main`보다 FIX_03 문서 2커밋 앞섬 |
| GitHub Releases | `v1.0.23`이 Latest; `v1.0.29` annotated tag는 있으나 GitHub Release는 없음 |
| 기존 공개 릴리스 workflow | `release-evidence/v1.0.29`와 공개 후보 GHCR package를 요구; 현재 evidence 경로가 없어 그대로 dispatch하면 통과할 수 없음 |
| 03:00 운영 실행 | `succeeded`, 5,207 문서, `missing_unjustified=0`, 새 generation `g-7ea0625531c447a8a3ae4368-7a7b0b5057d0`을 MCP가 제공 |
| 원격 GC | run-2·3·03:00 실행에서 `failed`; 03:00 `gc_deleted=0`. 격리 상태의 삭제 없는 dry-run에서 `GCError: retained generations disagree on OCR cache` 재현. 최신 generation 안에서 동일 native OCR reuse key 1개가 같은 PDF SHA이면서 서로 다른 OCR CAS 2개에 결속됨. 원 예외를 운영 로그가 공통 메시지로 축약한 것 |
| 용량 검사 | 운영 시작 하한 2 GiB, 후속 동적 검사 `예상 peak growth + 2 GiB reserve`; 03:00 동적 예측 67,250,867,848 bytes, 당시 여유 100,353,572,864 bytes로 통과. 배포 README의 “시작 하한 32 GiB”는 후보 overlay의 값과 혼동한 문서 오류 |
| 호스트 | `/` 여유 약 97 GB; `cardrag-worker-v129-state` 46.59 GB와 `cardrag-mcp-v129-candidate-state` 46.8 GB는 운영 중 |
| 과거 대형 볼륨 | `cardrag-worker-v114-candidate-state` 48.14 GB(옛 seed 기준), `cardrag-mcp-v114-candidate-hashcompat-state` 37.19 GB(옛 rollback 기준), 둘 다 컨테이너 링크 0. OCR 원격 전수 검증과 직전 세대 1개 보호 확인 뒤 정리 대상 |
| Paddle OCR 15건 | 각 로컬 native manifest의 provider `local-paddleocr` 확인; 최신 원격 manifest에 15개 고유 OCR CAS가 있으며 WebDAV 본문 전체 15/15 SHA-256·크기 일치 |
| 전체 OCR readback | 최신 generation 고유 OCR CAS **4,922/4,922**개를 WebDAV GET으로 스트리밍하고 SHA-256·크기를 전수 확인. 검사 전후 stable generation 동일 |
| 지정한 단일 롤백 세대 | 직전 세대 `g-2c03669cd7b947ccb3f2ca36-f916d1c475e0`, 5,206문서; READY SHA `02fd7346…`, manifest 결속 정상; DB/vector 원격 본문 전체 SHA-256·크기 검증 성공. 직전 세대만의 추가 OCR CAS는 **0개** |

## 범위와 제약

- 운영 timer, stable pointer, 서비스 중인 MCP, `v129` Worker/MCP 볼륨, Paddle 모델, 실제 Codex-home/secret mount를 건드리지 않는다. 복구 시험은 별도 볼륨·채널·포트에서 진행한다.
- 원격 객체가 manifest에 참조된다는 것과 WebDAV에서 본문·SHA를 읽을 수 있다는 것, **새 Worker에서 OCR을 재사용할 수 있다는 것**을 구분해 검증한다. 전체 Worker state·임베딩·설정 백업은 복구 필수조건이 아니다.
- WebDAV 접근 자격과 새 운영 설정은 시스템 폐기 후 재발급·재구성 가능해야 한다. 자격증명을 OCR 객체나 GitHub에 평문으로 넣지 않는다.
- GC 조사에서 원격 삭제를 시험하지 않는다. 실제 삭제는 stable/직전 정상 generation의 참조 보존과 grace 기간, 대상 목록을 검증한 후 기존 승인 설정에 따라 실행한다.
- 로컬 정리는 정확한 Docker 이름·이미지 digest와 참조 컨테이너를 확인한 뒤 한 대상씩 한다. 운영용 두 v129 state 볼륨과 직전 정상 WebDAV generation **1개만** 롤백 기준으로 보존한다. 옛 v114 상태·이미지·인증 흔적은 현재 사용하지 않으면 정리한다. `docker system prune --volumes`와 포괄 glob 삭제를 사용하지 않는다.
- 이미 배포한 v1.0.29 태그를 이동하거나 재작성하지 않는다. GitHub Release는 태그·소스·증빙이 일치할 때 발행한다.

## 구현 순서

### 1. 복구 계약과 원격 증거 확정

1. stable pointer → READY → manifest의 결속을 확인하고 최신 세대가 참조하는 **고유 OCR CAS 전부**를 WebDAV에서 읽기 전용으로 스트리밍해 SHA-256·크기를 검증한다. Paddle 15건을 별도로 집계하고 검사 시각·세대·개수·바이트·실패를 남긴다. 직전 정상 세대의 READY/manifest와 서빙 DB/vector도 확인한다. 원격 객체를 수동 삭제하지 않는다.
2. 새 빈 Worker state에 최신 generation manifest의 문서 ID·PDF SHA·OCR contract·OCR CAS를 결속해 다운로드·검증·재사용하는 **OCR 복원 경로**를 구현한다. 동일 PDF/reuse key라도 OCR CAS가 다를 수 있으므로 문서별 참조를 보존한다. 15건처럼 공유 cache binding이 없는 OCR도 복원해야 한다. 필요하면 `seed-state-v122`의 `source_record_missing` 의존성을 제거하거나 별도 import를 만든다.
3. 격리된 새 볼륨에서 GitHub 고정 commit/이미지와 WebDAV OCR만으로 import를 수행한다. 기존 5,207건의 OCR provider 호출이 0건인지, 특히 Paddle 15/15가 재사용되는지 확인한다. source discovery·PDF 다운로드와 임베딩·색인 재계산 시간은 허용하며 별도로 측정한다. 운영 stable pointer에는 쓰지 않는다. MCP의 빈 state 최신 generation 동기화도 확인한다.
4. OCR 보존을 위해 필요한 manifest·READY·CAS와 WebDAV의 retention/GC 정책을 문서화한다. 새 세대 발행과 GC 후에도 현재+직전 세대의 OCR이 남는지 검사한다. `docs/RECOVERY.md`와 배포 README에 OCR만 보장하는 재구축 절차와 실제 소요 시간을 적는다. 전체 환경·임베딩 복구 시간을 보장한다는 문구는 쓰지 않는다.

### 2. 원격 GC 원인 규명 및 수정

1. **재현 완료:** 별도 임시 state volume `cardrag-gc-diagnostic-20260928`에서 `gc` dry-run을 실행해 `GCError: retained generations disagree on OCR cache`를 확인했다. 이 임시 volume은 진단 후 삭제했다. 최신 봉인 manifest와 로컬 native OCR manifest를 읽기 전용으로 대조하면 하나의 reuse key(`5c0d1e39…`)가 같은 PDF SHA(`0f64a72d…`)에 대해 2026-08-30과 09-04에 생성된 서로 다른 OCR SHA(`087bf406…`, `27981c5e…`)를 참조한다. 둘 다 현재 문서가 참조하므로 삭제하면 안 된다. 전체 최신 세대에서 이런 충돌은 native key **1개**뿐이다. 이전의 WebDAV 권한/경로 추정은 이 재현에 부합하지 않는다.
2. `gc.py`의 `retained_cache_references`가 `(kind, reuse_key)`마다 OCR artifact 하나만 허용하는 가정을 수정한다. 두 문서의 OCR CAS를 모두 mark하고, 해당 원격 cache의 READY/manifest가 실제로 가리키는 artifact를 안전하게 확인·보존한다. 원격 cache가 어떤 binding을 채택했는지와 충돌 자체를 명확히 보고한다. 불일치가 복구 가능한 동일 키 중복인지, 실제 원격 무결성 오류인지를 구분하며 후자는 계속 fail-closed 처리한다. immutable generation이나 OCR 본문을 변경하지 않는다.
3. retained generation 2개와 그 PDF/OCR 참조, 포인터 변경 감지, 1일 grace, 부분 삭제 재시도 계약을 유지한다. mark 검증 실패와 첫 DELETE 실패/부분 DELETE 실패를 구분해 운영자가 재현·조치할 수 있는 구조화 지표를 남긴다.
4. 새 코드의 dry-run 결과와 삭제 후보를 검토한 후, 기존 GC 승인 하에서 한 번의 supervised apply를 수행한다. 최신/직전 세대 및 MCP 질의가 그대로 유효한지, 삭제 수와 원격 사용량이 어떻게 바뀌었는지 확인한다. 이후 다음 정기 실행의 `gc_status`도 확인한다.

관련 파일: `apps/cardrag-worker/src/cardrag_worker/gc.py`, `pipeline.py`, `cli.py`, WebDAV 클라이언트와 GC 테스트.

### 3. GitHub `v1.0.29 Latest` 정리

1. `main`에 이미 포함된 v1.0.29 코드와 release branch의 문서 2커밋을 구분해 기록한다. 문서만인 후속 커밋을 리뷰해 `main`에 반영하되 소스 태그는 유지한다.
2. 현재 `release.yml`은 tagged worktree 안의 `release-evidence/v1.0.29`를 요구하지만 현 태그에는 그 경로가 없다. 증빙이 실제로 존재하고 공개 가능한지 조사한다. 후속 evidence commit을 별도 입력으로 검증할 수 있도록 workflow를 수정하거나, 검증 가능한 별도 발행 절차를 마련한다. 증빙을 허위로 생성하거나 태그를 이동하지 않는다. private candidate package와 public image 자격 요건도 명시적으로 해결한다.
3. v1.0.29 GitHub Release를 공개할 경우 검증된 이미지 digest·SBOM·provenance·운영 상태와 공개 가능한 릴리스 노트를 결속한다. 발행 후 GitHub API의 `isLatest=true`, tag commit, assets/checksums를 재확인한다. 공개 작업 직전까지는 draft/검증 산출물을 준비하고 실제 공개 여부는 기존 릴리스 승인 절차를 따른다.

관련 파일: `.github/workflows/release.yml`, `docs/RELEASING.md`, `release-evidence/v1.0.29/`(실제 증빙 확인 뒤에만 생성), `.handoff/001_*/FIX_02_REPORT.md`.

### 4. 용량 정책과 문서

`/opt/cardrag/v1.0.29/deployment/README-deployment.md` 및 저장소 운영 문서에 운영 시작 하한 **2 GiB**와 후보 overlay **32 GiB**를 정확히 구분한다. 실제 보호는 파생 뷰·캐시 miss 기준 동적 `peak_growth + reserve` 및 진행 중 재검사라는 점을 설명한다. 시작 하한을 임의로 32 GiB로 올리는 변경은 하지 않는다. 호스트 여유 <80 GB 운영 점검 트리거와 동적 preflight 실패 알림을 분리한다.

### 5. 로컬 정리: 증거별 단계적 실행

| 단계 | 대상 | 예상 크기 | 삭제 조건 |
|---|---|---:|---|
| 즉시 정리 완료 | buildx builder `cardrag-release-v1026`의 과거 캐시 | 기존 volume 약 8.15 GB | 현재 빌드 없음 확인 후 cache만 prune; builder 구성 유지. 재빌드 시간 증가 |
| 즉시 정리 완료 | `cardrag-worker:v1.0.29-candidate-r4` 이미지 | 고유 약 3.22 GB | 참조 컨테이너 0, 동일 소스 `fdf87e6`의 운영 release 이미지 `80eb7aa2…` 확인 후 exact tag 제거 |
| 즉시 정리 완료 | `cardrag-worker-data` 볼륨 | 0 B | 링크 0, 빈 볼륨 확인 후 제거 |
| 정리 완료 | `cardrag-worker-v114-candidate-state` | 48.14 GB | 현재 세대 OCR 4,922개 원격 SHA·크기 검증, 컨테이너 링크 0, 현행 state와 분리 확인 후 삭제 |
| 정리 완료 | `cardrag-mcp-v114-candidate-hashcompat-state` | 37.19 GB | 직전 WebDAV generation의 READY·manifest·DB·vector 본문 SHA·크기 검증, 추가 OCR 0개, 컨테이너 링크 0, 운영 rollback 문서 갱신 후 삭제 |
| 정리 완료 | v1.0.28 Worker/v1.0.26 MCP 이미지, 비활성 candidate Codex-home 볼륨 | 이미지 약 3.5 GB, 인증 볼륨 각 약 1.2 MB | 운영 이미지·인증 mount/컨테이너 참조가 없어 정확한 이름으로 삭제 |
| 정리 완료 | `/opt/cardrag/v1.0.20`, `v1.0.23`, `v1.0.26` 배포 소스 복사본 | 합계 약 20 MB | `/opt/cardrag/current`와 systemd가 v1.0.29를 가리키고 해당 과거 경로의 활성 참조가 없어 정확한 경로로 삭제 |
| 유지 | `cardrag-worker-v129-state`, `cardrag-mcp-v129-candidate-state`, `cardrag-worker-paddleocr-models`, 실제 Codex-home, pinned 운영 이미지 | 운영 데이터 | 삭제 대상 아님 |

각 대상마다 이름·size·container links·필요한 digest를 기록하고, 하나씩 정리한 뒤 `df -B1 /`, `docker system df -v`, MCP readiness/실제 generation, timer 상태를 재측정한다. 다른 프로젝트의 익명 볼륨은 건드리지 않는다. 활성 `cardrag-worker-v120-recovery-auth-20260910`은 이름이 오래되어도 현재 인증 mount이므로 유지한다. 원격 직전 세대를 **유일한 롤백 근거**로 선택하며, 구형 v1.0.26/v114 rollback runbook을 더 이상 유효하다고 안내하지 않는다. 복구 시에는 timer를 멈추고 직전 세대의 READY/manifest가 결속된 포인터로 MCP만 되돌린 뒤, 새 Worker 발행 전에 데이터 정합성을 다시 판단하는 절차를 마련한다. 자동 pointer 쓰기는 이 정리 작업에서 실행하지 않는다.

롤백 절차의 구현 기준: Worker timer와 실행 중인 Worker를 정지·확인하고 현재 stable pointer의 원문과 SHA를 기록한다. 직전 generation의 READY→manifest→DB/vector와 OCR 참조를 확인한 후 그 READY/manifest SHA로 `GenerationPointer`를 구성한다. pointer가 사전 확인 값과 같을 때에만 `StablePointerPublisher.atomic_replace_bytes`로 전환하고 MCP의 readiness·실제 generation·질의를 검증한다. 새 Worker 실행은 이전 세대와 현재 Worker state의 관계를 정리할 때까지 재개하지 않는다. 이 절차는 rollback 도구·runbook으로 만들고 격리 채널에서 먼저 시험한다. 현재 운영 pointer에는 이 계획 작성 중 쓰지 않는다.

**계획 작성 중 완료한 안전 정리 기록:** `docker buildx du --builder cardrag-release-v1026`에서 reclaimable 8.4 GB와 진행 중인 빌드가 없음을 확인하고 `docker buildx prune --builder cardrag-release-v1026 --all --force`를 실행했다(`Total: 8.4GB`, 이후 builder cache 0 B). 후보 r4 이미지는 참조 컨테이너 0, 운영 이미지와 같은 Git revision `fdf87e6`을 확인한 뒤 `docker image rm cardrag-worker:v1.0.29-candidate-r4`로 제거했다. `cardrag-worker-data`는 링크 0·빈 볼륨으로 확인 후 제거했다. `/`의 실제 가용량은 시작 104,002,715,648 bytes에서 완료 후 115,624,886,272 bytes로 **11,622,170,624 bytes(약 10.82 GiB) 증가**했다. buildx builder 자체, 운영 MCP(healthy), 운영 Worker/MCP 볼륨, timer(다음 2026-09-29 03:00 KST)는 유지됐다. 후속 Executor는 이 세 삭제를 반복하지 않고 남은 복구·GC·릴리스 작업을 수행한다.

**OCR 범위 재확인과 추가 정리:** 최신 원격 generation의 OCR CAS 4,922개를 인증된 읽기 전용 GET으로 스트리밍하여 전부 SHA-256·크기 일치, stable pointer 변경 없음. Paddle 15개는 별도 15/15 확인했다. 직전 세대의 OCR CAS 중 현 세대에 없는 것은 0개다. 이 증거와 컨테이너 링크 0을 확인해 `cardrag-worker-v114-candidate-state`만 정확한 이름으로 삭제했다. `/` 가용량은 삭제 직전 115,619,205,120 bytes에서 직후 164,033,056,768 bytes로 **48,413,851,648 bytes 증가**했다. 참조 컨테이너 0인 비활성 인증 볼륨 `cardrag-worker-v114-candidate-codex-home`, `cardrag-worker-v122-candidate-codex-home`도 정확한 이름으로 삭제했다(각 약 1.2 MB). 이전 Worker rollback 이미지 `cardrag-worker:v1.0.28`(image ID `24b98522…`, 참조 컨테이너 0)도 제거했고 이때까지 `/` 가용량은 167,065,374,720 bytes가 됐다.

**직전 세대 검증과 정리 완료:** 직전 세대 `g-2c03669cd7b947ccb3f2ca36-f916d1c475e0`의 DB와 vector 원격 본문을 전체 읽어 SHA-256·크기 모두 검증했다. 전후 stable pointer는 최신 세대로 동일했다. 구형 rollback 명령을 현재 운영 배포 문서 `/opt/cardrag/v1.0.29/deployment/README-deployment.md`에서 역사적 기록으로 명시하고 직전 세대 1개를 지정한 다음, 컨테이너 링크 0의 `cardrag-mcp-v114-candidate-hashcompat-state`와 이미지 `cardrag-mcp:v1.0.26`(image ID `c8e45af0…`, 참조 컨테이너 0)을 삭제했다. `/opt/cardrag/current`와 systemd가 v1.0.29만 가리키는 것을 확인하고 과거 배포 소스 복사본 `/opt/cardrag/v1.0.20`, `v1.0.23`, `v1.0.26`도 정확한 경로로 정리했다. 이제 CardRAG 볼륨은 `cardrag-worker-v129-state`, `cardrag-mcp-v129-candidate-state`, `cardrag-worker-paddleocr-models`, 활성 Codex-home 4개뿐이다. `/` 가용량은 **204,445,978,624 bytes**, 이 과제 시작 시점 **104,002,715,648 bytes** 대비 **100,443,262,976 bytes(약 93.54 GiB) 증가**했다. MCP `/health/ready`는 `true`, timer는 활성이고 다음 예약은 2026-09-29 03:00 KST다.

## 검증과 인수 기준

- 원격 최신 세대의 pointer/READY/manifest와 고유 OCR 4,922개의 전체 본문·SHA·크기 검증 결과가 기록된다. Paddle 15건은 독립 집계하고, 직전 세대의 rollback용 READY/manifest/DB/vector와 추가 OCR 객체도 검증한다.
- Worker는 로컬 과거 볼륨 없이 OCR을 복원해 기존 5,207건에 대한 **OCR provider 호출 0건**과 Paddle 15/15 재사용을 보인다. 임베딩·색인·환경설정 재구축은 허용한다. 실제 빈 MCP 동기화와 복구 소요 시간을 기록한다.
- GC 실패 원인이 특정되고, 안전한 진단 결과 및 성공한 supervised cleanup/다음 정기 실행이 남는다. 0건 삭제가 정상인 경우와 실패를 구분한다.
- GitHub `main`·tag·Release의 관계가 일치하고 v1.0.29 공개 Release의 `Latest`와 자산 검증이 실제로 확인된다.
- 용량 문서는 실제 시작 하한과 동적 검사를 정확히 설명한다.
- 과거 롤백 자산은 현재 운영 자료 밖에서 **직전 정상 WebDAV generation 1개**만 남긴다. 과거 로컬 볼륨·이미지의 정확한 삭제 대상과 전후 여유 공간, 활성 서비스 상태를 기록한다. 조건을 충족하지 못한 대상은 이유와 함께 보존한다.
- 코드 변경이 있는 GC/OCR 복원 경로는 해당 실패·부분 삭제·OCR import 테스트와 필요한 전체 검사(ruff, mypy, 프로젝트 전체 테스트, Compose 렌더)를 수행한다. 외부 GitHub Release 작업은 기존 릴리스 검증기를 통과한다.

## Executor 보고 형식

`REPORT.md`에는 각 단계의 실제 명령·결과, 원격 OCR 5,207건/고유 CAS 4,922개와 Paddle 15건의 검증·복원 측정치, GitHub Release 상태, GC 진단·수정 근거, 삭제 대상별 precondition·전후 크기와 `df`, 실패/보류 사유, 잔여 운영 위험을 적는다. 이 계획 작성 중 이미 수행한 삭제는 다시 실행하지 않는다. 계획과 다른 구현은 OCR 보존 목적을 지키며 편차를 설명한다.
