# v1.0.29 PDF lineage·OCR 캐시 복구 및 재검증 계획

## 요약

- v1.0.28 성공 상태 `cardrag-worker-v114-candidate-state`와 generation `g-bd9a4c513041462886d347af-9b84c2e38c14`를 기준 seed로 사용한다.
- 기존 5,192건의 PDF lineage와 OCR을 모두 보존하고, 확인된 신규 PDF SHA 14건만 PaddleOCR로 처리한다.
- 현재 기준 합격 수량은 다음과 같다.

| 항목 | 수량 |
|---|---:|
| 기존 current | 5,044 |
| 기존 historical | 148 |
| 교체되어 historical이 되는 PDF | 13 |
| 새 current PDF | 14 |
| 최종 current | 5,045 |
| 최종 historical | 161 |
| 최종 corpus | 5,206 |
| 기존 OCR 재사용 | 5,192 |
| PaddleOCR 신규 처리 | 14 |
| 외부 OCR 호출 | 0 |

- 실패한 v1.0.29 상태 볼륨은 증거로 보존하고, 새 빈 후보 상태 볼륨에 seed를 먼저 적용한 뒤 실행한다.

## 구현 변경

### 상태 seed와 lineage 복원

- 새 `seed-state-v122 SOURCE_ROOT --generation-id … [--apply]` 명령을 추가한다. 기본은 dry-run이며 destination Worker lock을 획득한 경우에만 적용한다.
- source 볼륨은 read-only로만 열고 writer 종료, SQLite sidecar 부재, integrity/FK, canonical snapshot, seal/generation, 파일 SHA·크기를 검증한다. 6.7GB DB를 허용하도록 전용 상한을 16GiB로 두되 embedding·checkpoint·인증·GC·publish 상태는 이관하지 않는다.
- terminal run identity와 canonical discovery snapshot, PDF object/source/revision lineage를 시간순으로 replay한다. 기존 historical 148건과 이번에 교체되는 predecessor 13건이 revision 확장에서 유지되어야 한다.
- PDF CAS는 해시가 일치하는 객체만 원자적으로 materialize하고, destination 충돌은 덮어쓰지 않고 실패 처리한다.
- seed 결과를 `audit-reports/state-seed/<sha256>.json`에 canonical ledger로 봉인한다. 동일 seed 재적용은 생성 파일·revision 0건인 idempotent 결과여야 한다.

### OCR seed 복원

- 선택한 v1.0.28 seal의 5,192개 `ocr.md`를 전부 검증한다.
- native 3,682건은 기존 native manifest, OCR 계약/reuse key, PDF identity와 본문 SHA를 검증한다.
- adopted 1,510건은 seal의 cache kind/reuse key와 원격 READY·manifest·receipt를 read-only로 검증하고 로컬 seed control을 보존한다.
- 검증된 파일만 destination의 immutable OCR seed 영역으로 복사하고 ledger를 마지막에 commit한다. source 상태나 WebDAV OCR cache에는 쓰지 않는다.
- resolver는 provider 호출 전에 seed ledger를 조회하고 매번 source identity, PDF SHA·크기·페이지 수, manifest와 OCR SHA를 재검증한다.
- seed generation에 포함된 문서가 cache miss가 되면 Paddle로 대량 재처리하지 않고 preflight 실패로 처리한다. seed에 없던 새 PDF만 정상 miss로 인정한다.

### PDF 최신성 및 비용 안전장치

- 후보 릴리스 실행에는 `CARDRAG_PDF_CACHE_FORCE_REVALIDATE=true`를 적용하여 168시간 TTL보다 우선해 모든 current source를 조건부 요청 또는 재다운로드한다.
- acquisition 후 corpus-diff 보고서에 unchanged, same-source byte revision, successor source, 신규 상품, historical 유지, retired lineage를 구분한다. 이전 문서가 사유 없이 사라지면 OCR 전에 실패한다.
- Worker 기본 OCR을 `local-paddleocr / PaddleOCR-VL-1.6`으로 변경하고 Paddle 모델 볼륨을 기본 Compose에 포함한다.
- fallback provider/model은 비워 두고 `CARDRAG_EXTERNAL_OCR_ALLOWED=false`를 기본값으로 추가한다. Codex/OpenRouter OCR은 provider 선택과 allow flag를 동시에 명시한 경우에만 허용한다.
- OCR 계약 SHA `873a628e…`는 유지한다. 출시일 parser v3 때문에 기존 OCR을 무효화하지 않는다.

## 검증 계획

- 축소 통합 fixture에서 `기존 current + historical + 교체 revision + 신규 PDF`를 재현하여 history 손실 없이 합집합 corpus가 생성되는지 검증한다.
- native/adopted OCR seed hit, 동일 URL PDF 바이트 변경, 새 URL revision, 신규 상품, DRM 8건 유지, seed 재적용을 각각 테스트한다.
- source DB 변경, active writer, WAL/SHM, 비정규 파일·symlink·경로 이탈, seal/manifest/body 변조, destination 충돌은 fail-closed로 검증한다.
- stale PDF 테스트에서는 168시간 이내 cache라도 force revalidation이 새 ETag/Last-Modified 또는 새 본문 SHA를 발견하는지 확인한다.
- external OCR primary/fallback이 기본 설정이나 오래된 host env로 주입되면 시작이 거부되는지 테스트한다.
- 전체 테스트·타입·Compose·보안 검사를 수행하고 기존 `2196 passed` 기준에 새 테스트를 추가한다.
- 실제 seed dry-run/apply 후 현재 기준으로 다음을 확인한다.
  - seed lineage 5,192건 복원
  - current 5,045건, historical 161건, 총 5,206건
  - OCR seed hit 5,192건
  - PaddleOCR 14건
  - Codex/OpenRouter OCR 0건
- 실행 시점에 추가 PDF 변경이 있으면 동일 공식으로 새 corpus-diff를 봉인한다. seed 문서의 무사유 손실이나 외부 OCR 호출은 수량과 관계없이 실패다.

## 후보 실행 및 릴리스

- 변경 후 새 source commit에서 Worker/MCP 이미지를 다시 빌드한다. 기존 `6e1d099` 이미지 digest는 재사용하지 않고 OCI digest, SBOM, provenance를 다시 검증한다.
- 새 후보 상태 볼륨에 state/OCR seed를 먼저 적용하고 기존 Paddle 모델 볼륨을 재사용한다. stable pointer, 공유 OCR cache와 v1.0.28 source 볼륨은 모두 read-only로 유지한다.
- Worker는 고유한 retained 컨테이너 이름으로 detached 기동한다. Codex는 시작 직후 이미지 digest, Paddle 설정, 외부 OCR 금지, seed ledger, read-only 정책과 `running` 상태만 확인한다.
- 장시간 Worker를 Codex가 계속 모니터링하지 않는다. 기동 확인 후 대기하고 사용자가 완료를 알리면 종료 코드, terminal result, corpus-diff, OCR/provider 지표와 불변성 증거를 확인한다.
- Worker 성공 후 후보 MCP를 기동하여 12개 도구, 카드사별 상품출시일, 최신 상품 coverage, 날짜 미확인·과거 안내장 예외를 검증한다.
- 모든 gate 통과 후에만 v1.0.29를 main에 병합·태그하고 동일 검증 digest를 stable 및 `/opt/cardrag`에 배포한다. 운영 기본값도 PaddleOCR와 외부 OCR 금지로 유지한다.

## 가정과 고정 정책

- 14건은 모두 과거 DB에 없던 PDF SHA이므로 실제 새 OCR identity로 취급한다. 그중 신규 상품은 1건이고 13건은 기존 상품 revision이다.
- v1.0.28 성공 런의 5,192건은 삭제 대상이 아니라 재사용·역사 보존 대상이다.
- 정상 stable 실행의 PDF TTL은 기존 168시간을 유지하되 릴리스 후보 검증에는 force revalidation을 반드시 적용한다.
- 실패한 후보 상태와 컨테이너는 원인 증거가 봉인될 때까지 삭제하지 않는다.
