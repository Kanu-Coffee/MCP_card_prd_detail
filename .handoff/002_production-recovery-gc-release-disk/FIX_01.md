# FIX_01 — 운영 이미지·OCR 복원 계약·릴리스 설명 정합성

## Reviewer 판정 (2026-09-28 KST)

현재 MCP는 `/health/ready=true`로 정상 서빙 중이고 03:00 Worker timer도 활성이다. 5,207건의 OCR CAS 원격 보존, 격리된 5,207건 import, 15건의 요청 대상 OCR 본문 복원, 로컬 용량 정리, GitHub `main` 동기화와 `v1.0.29 Latest` 표시는 확인된다. **서빙 중단이나 운영 롤백은 필요하지 않다.** 다만 아래 실제 운영·복구 경로의 불일치 때문에 handoff 002의 완료 판정은 이 수정 라운드 후에 한다.

### 1. 다음 정기 Worker는 여전히 수정 전 GC 코드를 사용한다 — 필수

- 수정 코드와 `restore-ocr-seed`는 `ddd70ce`에 있으나 `v1.0.29` 태그는 `fdf87e6`에 고정돼 있다.
- `/etc/cardrag/worker.env`의 `CARDRAG_WORKER_IMAGE`는 digest `80eb7aa2…`이고 이미지의 source revision은 `fdf87e6`이다. Reviewer가 이 실제 운영 이미지를 네트워크 없는 컨테이너에서 검사했을 때 GC의 다중 결속 수정과 `restore-ocr-seed` 명령이 **둘 다 없었다**. 따라서 Executor가 호스트 `.venv`에서 성공시킨 GC apply(eligible 0, deleted 0)는 다음 03:00 자동 실행의 GC 성공을 입증하지 않는다.
- **조치:** `ddd70ce` 또는 후속 수정 커밋을 정확히 담은 immutable Worker 이미지를 빌드·검증하고 운영 env의 digest 및 배포 소스 경로를 일치시킨다. MCP와 stable pointer, 운영 볼륨은 보존한다. 새 이미지 안에서 다중 결속 코드와 복원 명령이 존재함을 확인하고, 그 **동일 이미지**로 격리 dry-run 및 기존 승인 설정하의 GC apply를 검증한다. 다음 정기 실행에서 `gc_status=succeeded`, 보존 세대 2개, OCR/PDF 참조 무손실을 확인한다. 유예기간 때문에 0건 삭제가 나와도 정상 성공으로 인정한다.

### 2. OCR 전용 원장이 새 호스트의 첫 배치를 잘못 중단시킬 수 있다 — 필수

- `ocr_recovery.py`는 복원 원장의 `prior_current_doc_ids`에 manifest 문서 5,207건 **전부**를 넣고 `prior_historical_doc_ids`를 빈 목록으로 쓴다. 동시에 source records는 복원하지 않는다. 실제 03:00 `corpus-diff.json`은 현재 5,046건·역사 161건이었다. 빈 Worker에서 재발견된 현재 자료만 확보하면, `corpus_diff.py`의 `prior_all - all_acquired_ids` 검사로 과거 161건 등이 `missing_unjustified`가 되어 OCR 단계 전에 실패할 수 있다.
- 사용자 복구 계약은 기존 OCR 바이트 보존·재사용이며 과거 source lineage/임베딩의 즉시 복원이 아니다. **조치:** OCR 전용 원장을 과거 전체 corpus를 이미 복원한 상태처럼 취급하지 않도록 별도 계약으로 다룬다. 기존 `cardrag.state-seed-ledger.v2`의 무손실 검사는 약화하지 않는다. 보관된 과거 OCR 파일은 유지하고, 재발견된 문서의 동일 document ID/PDF SHA에 대해 재사용한다. 새 문서·변경 PDF는 정상 처리한다.
- 격리 복원 state에서 실제 5,207건 원장과 현재 문서만 재발견되는 사례를 검증해 corpus-diff가 거짓 누락으로 중단되지 않음을 보인다. 기존 문서의 OCR provider 호출 0건은 **검증한 범위의 수량**과 함께 기록한다. 현재 보고서의 ‘5,207건 전체 0-provider’ 문구는 실제로 검증한 15+100건 샘플과 구분한다.

### 3. 복원에 쓰는 generation 제어 파일의 결속을 완성한다 — 필수

- 현재 `restore_ocr_seed_from_generation()`은 pointer와 manifest만 읽고, READY를 읽거나 `pointer.ready_sha256`을 확인하지 않는다. `manifest.generation_id == 요청 ID`와 canonical JSON 확인도 없다. Reviewer가 테스트 원격에서 READY를 삭제한 뒤 최신 generation으로 `--dry-run`을 실행했더니 성공했다. PLAN의 `pointer → READY → manifest` 복구 계약에 맞지 않는다.
- **조치:** 기본 최신 세대와 명시한 `--generation-id` 모두 READY의 존재·generation ID·manifest SHA, canonical bytes를 검증한다. 최신 세대는 pointer의 READY SHA와 manifest SHA도 결속한다. CAS 참조 검증은 유지한다. READY 누락·해시 불일치·세대 ID 불일치의 음성 테스트를 추가한다.

### 4. 공개 릴리스가 실제 태그의 기능을 정확히 설명하도록 한다 — 필수

- [GitHub `v1.0.29` Release](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.29)는 `Latest`이며 tag `fdf87e6`을 가리킨다. 공개 본문에는 후속 `ddd70ce`의 `restore-ocr-seed`와 GC 다중 결속 수정이 v1.0.29 기능으로 적혀 있다. 이 기능은 태그 소스와 현재 운영 이미지에 없다. `release.yml`은 v1.0.29에 실행되지 않았고 현재 Release의 업로드 asset은 0개다. Latest 표시는 달성했지만 소스·설명·배포 이미지의 일치는 달성하지 못했다.
- **조치:** v1.0.29 태그는 이동하지 않는다. 공개 Release 본문과 저장소의 `RELEASE_NOTES_v1.0.29.md`를 실제 태그 내용에 맞게 고치고, 후속 GC/OCR 수정은 새 immutable revision/패치 릴리스 또는 명시적 운영 hotfix로 구분한다. 새 운영 Worker 이미지의 digest와 source revision, 검증 자료를 기록한다. 공개 배포 자산을 제공한다고 주장한다면 실제 asset/checksum/공개 접근 검증을 완료하고, 그렇지 않으면 릴리스 설명의 범위를 명확히 한다. `main`은 현재 `ddd70ce`로 올라와 있으므로 이미 반영된 문서 2커밋을 다시 병합하지 않는다.

## 복원 메타데이터의 표현 범위

`ocr_cache_kind is None`은 **원격 OCR cache 결속이 없다는 뜻**이며 OCR 공급자가 Paddle이라는 증거는 아니다. 현재 복원 코드는 그 조건만으로 15건을 Paddle로 분류하고 다른 모든 문서의 모델을 `gpt-5.4`로 합성한다. Reviewer가 현행 Worker run의 로컬 `native-manifest.json`을 읽으면 `local-paddleocr` 계약을 가진 문서가 3,697건이다. WebDAV generation manifest만으로 모든 과거 OCR 공급자·모델을 확정할 수 없다. 사용자의 필수 요건은 **OCR 바이트** 보존이므로, 바이트/문서 결속을 보장하되 모델 provenance를 확인하지 못한 항목에 대해 ‘원래 모델 계약 100% 보존’이라고 쓰지 않는다. 런타임이 provenance를 사용할 경우 확인 가능한 원격 cache manifest만 사용하거나 미확인 상태를 명시한다. Paddle 요청 대상 15건의 원격 OCR 바이트 15/15 검증 결과는 유지한다.

## 검증 및 보고 기준

1. 타깃 테스트: OCR 전용 원장으로 시작하는 첫 배치/corpus-diff, WebDAV 제어 파일 불일치, 중복 OCR 결속/미발행 cache, 첫 DELETE/부분 DELETE 실패를 검증한다. 필요한 정적 검사·Compose 렌더와 프로젝트 테스트를 수행한다.
2. 운영 검증: 새 Worker image digest와 Git revision을 실제 컨테이너에서 읽고, 같은 이미지의 GC dry-run/apply 결과를 기록한다. 이후 정기 실행의 `gc_status`와 MCP readiness/generation을 확인한다. 정기 실행 전이면 그 항목만 날짜를 명시해 추적하며 다른 완료 항목까지 실패로 취급하지 않는다.
3. GitHub 검증: 태그·Release 본문·새 수정 버전/운영 이미지의 관계 및 `Latest` 상태를 API로 다시 확인한다. 공개 asset이 없으면 없다고 보고한다.
4. `FIX_01_REPORT.md`에 변경 파일, 명령과 결과, 실제 검증한 OCR 문서 수, 운영 이미지 전환 시각·digest, 남은 정기 실행 관찰 항목을 기록한다. 기존 `.handoff/001_*`와 불변 v1.0.29 태그는 보존한다.
