# FIX_02 — Worker 이미지 배포 참조와 OCR 복원 출처 표기

## Reviewer 판정 (2026-09-28 KST)

`FIX_01`의 핵심 기능 수정은 확인했다. `main`과 `release/v1.0.29`에 `ffc3124`가 반영됐고, 실제 로컬 운영 Worker 이미지 안에 다중 결속 GC와 `restore-ocr-seed`가 있다. OCR 전용 원장의 corpus-diff 처리 및 pointer→READY→manifest 검증도 타깃 테스트 30건에서 통과했다. MCP readiness는 정상이고, 운영 이미지 캐시가 있는 현재 호스트에서 GC apply도 Executor 보고상 성공했다. **서빙 중단이나 롤백은 필요 없다.** 2026-09-29 03:00 정기 실행 결과는 그때 관찰하면 된다.

다만 아래 두 항목은 실제 재기동·복원 경로 및 검증 결과의 정확성과 연결되므로 handoff 완료 전에 수정한다.

### 1. 운영 env가 가리키는 신규 GHCR 다이제스트는 원격에 없다 — 필수

- `/etc/cardrag/worker.env`와 `docker compose config --images`는 `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate@sha256:25de74b83429ac588cb6e8136c7288364a1f3d9c68c19f8443178ed37ca61235`를 가리킨다. 이 값은 로컬 `cardrag-worker:v1.0.29-patch1`의 **Image ID**와 정확히 같다.
- Reviewer의 `docker buildx imagetools inspect`는 이 새 다이제스트에 `not found`(exit 1)를 반환했다. 같은 명령으로 기존 MCP `53382bc…`와 이전 Worker `80eb7aa…` 다이제스트는 원격에서 정상 조회됐다. 따라서 전체 네트워크/인증 문제로 볼 근거는 없다. 현재 로컬 이미지가 있어 Compose 실행은 가능하지만, 이미지 캐시가 사라진 호스트에서는 현 운영 참조를 pull할 수 없다.
- `FIX_01_REPORT.md`와 저장소·공개 GitHub Release는 이 값을 불변 GHCR 배포 이미지로 기재한다. `v1.0.29` 태그 소스와 핫픽스 소스를 분리한 설명은 옳지만, 핫픽스 이미지의 배포 가능성 주장은 현재 사실과 다르다.
- **조치:** 가능하면 `ffc3124` 소스의 Worker 이미지를 GHCR에 게시하고 레지스트리가 반환한 실제 OCI manifest/index 다이제스트로 운영 env, 배포 문서, 저장소 릴리스 노트, 공개 Release 본문을 맞춘다. 배포 정책상 게시하지 않는다면 운영 env를 명시적인 로컬 이미지 태그로 바꾸고 GitHub 코드에서 같은 이미지를 재빌드하는 절차와 로컬 배포 범위를 문서에 명시한다. 어느 경로든 존재하지 않는 GHCR 다이제스트를 운영 참조나 공개 배포 증거로 남기지 않는다. 기존 운영 볼륨, MCP 서비스, stable pointer는 유지한다.
- **검증:** 최종 운영 이미지의 source revision과 실제 컨테이너 코드를 확인한다. GHCR 경로라면 깨끗한 pull 또는 `imagetools inspect`와 실제 pull로 원격 가용성을 확인한다. 로컬 빌드 경로라면 Compose 렌더와 캐시가 없는 새 Docker 환경에서 GitHub 소스로 빌드·기동할 수 있는 절차를 확인한다. 이미지 바이트가 동일하면 이미 수행한 GC apply를 반복할 필요는 없고, 달라지면 동일 이미지로 GC dry-run을 수행한다.

### 2. `ocr_cache_kind is None`을 여전히 PaddleOCR 출처로 기록한다 — 필수

- `ocr_recovery.py`는 캐시 결속이 없는 문서를 `paddleocr_documents`에 더하고, 복원 원장의 `model`을 `PaddleOCR-VL-1.6`으로 쓴다. `OCRResolver._lookup_seed_entry()`는 이 모델 문자열을 보고 `provider="paddleocr"`를 반환한다. 원격 generation manifest의 캐시 결속 부재만으로는 원래 OCR 엔진을 확정할 수 없다는 `FIX_01`의 지적이 복원 결과·후속 provenance에는 아직 반영되지 않았다.
- **조치:** 캐시 결속 부재를 출처 미확인으로 표현하고, 확인 가능한 별도 근거가 없는 문서의 모델·provider·통계에 PaddleOCR을 단정하지 않는다. OCR 바이트, PDF 결속, 해시 검증, 기존 문서의 무호출 재사용은 유지한다. 사용자에게 중요한 Paddle 요청 대상 15건의 **바이트 15/15 검증** 사실은 그대로 보고하되, 그것을 generation 전체의 모델 provenance 증명으로 확대하지 않는다.
- **검증:** `ocr_cache_kind=None`인 테스트 문서가 복원 후 OCR provider를 호출하지 않으면서도 원장·결과·후속 OCRResult에서 확인되지 않은 Paddle 출처를 주장하지 않는지 확인한다.

## 보고 및 완료 기준

1. `FIX_02_REPORT.md`에 최종 운영 이미지 참조, 실제 원격 조회 또는 로컬 재빌드 검증 명령·결과, 공개 Release와 로컬 문서의 정합성을 기록한다. `v1.0.29` 태그는 이동하지 않는다.
2. `FIX_01_REPORT.md`의 “5,207건 원장 후 5,046건 수집 통합 테스트” 문구는 현재 추가된 테스트가 실제로는 **3건 원장 후 2건 수집**인 점과 다르다. 이전 보고서를 덮어쓰지 말고 `FIX_02_REPORT.md`에서 실제 검증 범위를 바로잡는다. 대규모 반복 테스트는 구체적인 위험이 발견된 경우에만 추가한다.
3. 타깃 테스트와 필요한 정적 검사를 수행한다. 2026-09-29 03:00 정기 실행 결과는 실행 전이라면 날짜가 명시된 관찰 항목으로 남긴다. 이는 현재 서빙·운영 검증을 무효화하는 배포 차단 사유가 아니다.
