# CardRAG v1.0.9 후보

CardRAG는 카드사 상품설명서 PDF를 검색 가능한 형태로 만드는 두 프로세스
서비스입니다.

```text
one-shot Worker -> immutable WebDAV artifacts -> always-on MCP
```

- Worker는 현재 상품 PDF를 수집하고 OCR·임베딩한 뒤 SQLite 검색 세대와 원본
  객체를 게시합니다.
- MCP는 세대 전체를 내려받아 검증한 뒤 로컬에서 원자적으로 활성화합니다.
- Worker만 WebDAV에 쓰며 MCP 요청 처리는 검증된 로컬 파일만 사용합니다.
- 최신 파일이 승인된 DRM 컨테이너이면 과거 PDF로 대체하지 않고
  `unsupported_drm`으로 명시합니다.

현재 정상 운영 버전은 v1.0.8입니다. 현재 `/opt/cardrag/current`가 가리키는
`/opt/cardrag/v1.0.8`, 운영 컨테이너·상태 볼륨 및 `stable.json`을 변경하지 않는
별도 후보 환경에서만 v1.0.9을 개발·검증합니다. 실제 전환은 실행 중인 v1.0.8
Worker가 정상 종료한 뒤 진행합니다.

## v1.0.9 개선 사항

- PDF를 SHA-256 로컬 CAS에 한 번만 보관합니다. SQLite metadata dictionary는
  카드사, 상품코드, 문서종류, 공시 URL, source version, 게시물 ID와 PDF revision
  이력을 결속합니다. 새 안내장 identity와 동일 URL의 변경 바이트를 추적합니다.
  이전 revision의 metadata/hash는 감사용으로 남고, 실제 PDF bytes는 현재 실행과
  최근 게시 세대 2개가 참조하는 범위에서만 재사용합니다.
- 캐시 원본 확인 기본 주기는 168시간입니다. ETag/Last-Modified가 있으면 조건부
  요청으로 확인하고, validator가 없는 서버도 주기 만료 뒤 전체 바이트를 다시 받아
  같은 URL의 조용한 변경을 제한된 시간 안에 발견합니다.
- 성공 또는 `no_change` 실행 뒤 현재 실행과 최근 게시 세대 2개가 참조하지 않는 PDF
  CAS 바이트를 정리합니다. revision dictionary는 유지하며 실패 실행에서는 정리하지
  않습니다.
- v1.0.8 실행 디렉터리의 PDF는 read-only seed 절차에서 source snapshot과 정확히
  결속되고 전체 PDF 검증을 통과한 항목만 cache로 이관합니다.
- OCR 문서 실패는 나머지 문서를 중단시키지 않습니다. 카드사별 성공률이 각각
  95% 이상이면 성공 문서만 임베딩해 `cardrag.generation.v4`/
  `cardrag.serving-db.v4`를 게시하고, 실패 상품은 `ocr_failed` 상태와 제한된
  사유로 명시합니다. 임베딩·DB·인증·설정 등 전역 오류는 계속 fail-closed입니다.
- 게시 실행 결과와 WebDAV/MCP generation 및 PDF publication seal 보호 개수를 2개로
  줄였습니다. 미완료 진단 실행은 별도로 최대 2개를 유지합니다.
- 삼성카드를 공식 상품공시 API 기반으로 추가했습니다. 안내장 첨부를 우선하며
  모호한 복수 첨부는 거부하고, 중복 공식 상품코드는 결정적 variant ID로
  구분합니다. 기준 화면은 [삼성카드 상품별 약관·설명서 공시](https://www.samsungcard.com/company/IR/announce/product-conditions/UHPPCI0261M0.jsp)입니다.
- 새 실행은 이전 프로세스가 남긴 `running` 상태를 `interrupted`로 종결하고,
  discovery/OCR 단계별 진행 상황과 cache hit/miss를 기록합니다.
- 2026-08-28 v1.0.8 OCR 종료에서 확인된 native `READY.json` 게시 경계 오류를
  반영합니다. 자세한 근거와 재발 방지 합격 기준은
  [사건 조사 기록](docs/V1_0_8_OCR_INCIDENT_2026_08_28.md)에 있습니다.

기본 활성 카드사는 `woori,kb,shinhan,samsung`입니다. native OCR processor
contract는 기존 `cardrag-worker/1.0.4`를 유지하므로 검증 완료된 OCR cache는
재사용됩니다.

## 후보 격리

후보 Compose overlay는 다음을 강제합니다.

- 별도 WebDAV base URL과 `candidate-v1.0.9.json` 포인터
- 별도 Worker/MCP 상태 볼륨
- 후보 Worker 원격 GC 비활성
- 후보 MCP 기본 바인딩 `127.0.0.1:18009`

구체적인 cache seed, 합격 기준, MCP-first 전환 및 구버전 정리는
[v1.0.9 후보 검증·전환 절차](docs/V1_0_9_MIGRATION.md)를 따릅니다.

## 운영 이미지

후보가 아직 release tag로 승인되기 전에는 로컬 build 또는 commit SHA로 고정한
후보 이미지만 사용합니다. 현 운영의 immutable 이미지는 계속 다음과 같습니다.

```text
ymtop59/mcp-card-prd-detail:1.0.8-worker
ymtop59/mcp-card-prd-detail:1.0.8-mcp
```

v1.0.9 release tag는 전체 CI·후보 검증·운영 전환 승인 뒤에만 게시합니다. v4를
읽는 MCP를 먼저 배치해 기존 v2/v3 세대를 정상 제공하는지 확인한 다음 v1.0.9
Worker를 stable 채널에서 실행해야 합니다.

## 문서와 배포 파일

- [v1.0.9 전환 절차](docs/V1_0_9_MIGRATION.md)
- [v1.0.8 OCR 종료 조사](docs/V1_0_8_OCR_INCIDENT_2026_08_28.md)
- [현재 v1.0.8 실운영 가이드](docs/SIMPLE_RUNTIME.md)
- [배포 파일 안내](deploy/README.md)
- [Worker Compose](deploy/worker/compose.yaml)
- [MCP Compose](deploy/mcp/compose.yaml)
- [환경변수 예제](deploy/simple.env.example)
- [운영 문서 색인](docs/README.md)

MCP는 기본적으로 호스트의 loopback에만 게시됩니다. 외부 공개 시 TLS reverse
proxy를 사용하고 `/health/live`, `/health/ready`를 제외한 모든 요청에 Bearer
token을 전달해야 합니다.
