# cardrag-worker

공식 카드사 PDF를 수집하고 OCR·구조화·임베딩을 거쳐 불변 generation을 게시하는
단발성 Worker입니다. 스케줄러와 MCP 서버는 이 패키지에 포함되지 않습니다.

```text
수집 → PDF 검증 → OCR → 계약 구조화 → 임베딩 → 로컬 봉인 → WebDAV 게시
```

문서별 OCR 실패는 명시적으로 기록합니다. 현재 수집 대상에서 각 카드사의 성공률이 95% 이상일 때 성공한
문서를 게시할 수 있으며, 실패 상품은 `ocr_failed`로 남습니다. 이력 개정의 OCR 실패는
이 허용 범위에 포함되지 않고 실행을 실패시킵니다. 인증·데이터 무결성 등
시스템 오류는 실행을 실패시킵니다. 인식 가능한 DRM 문서는 `unsupported_drm`으로
표시하며 보호를 해제하지 않습니다.

```bash
cardrag-worker --help
cardrag-worker webdav-check
cardrag-worker run
cardrag-worker resume <run-id>
cardrag-worker resume-publication <run-id>
cardrag-worker gc                 # dry-run
```

`run`, `resume`, `webdav-check`는 실제 외부 요청을 수행할 수 있습니다.
`resume-publication`은 기존 로컬 봉인이 있는 실행의 게시만 재개합니다.
Worker는 단일 writer lock을 사용합니다. 실행 중인 상태 DB를 외부 SQLite 프로세스로
열거나 복사하지 말고 구조화된 로그로 진행 상황을 확인하십시오.

종료 신호를 받으면 진행 중인 변경과 게시 정합성 확인을 마친 뒤 lock을 해제합니다.
systemd 템플릿은 `TimeoutStopSec=infinity`, `SendSIGKILL=no`로 이를 기다립니다.
예약 실행이 기존 writer 때문에 거부된 경우 기존 실행을 종료해 해결하지 않습니다.

- [설치와 설정](../../docs/OPERATIONS.md)
- [데이터·게시 계약](../../docs/DATA_FORMATS.md)
- [상태와 복구](../../docs/RECOVERY.md)
- [OCR 인증 격리](../../SECURITY.md)

## 스테이지 부분 실행

수정 범위에 맞춰 기존 완료 실행의 산출물을 재사용할 수 있습니다. 운영 중인
Worker와 같은 상태 디렉터리, 완료 실행 ID, 로컬에 보존된 산출물이 필요합니다.
기본 `run`과 예약 배치는 전체 처리 방식을 유지합니다.

```bash
# 먼저 비용·상태 변경 없이 출처와 호환성을 확인
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-pdf --skip-ocr --dry-run

# 구조/export만 재생성; 현재 view와 정확히 일치하는 임베딩이 있어야 함
cardrag-worker run --reuse-from-run SOURCE_RUN_ID \
  --skip-pdf --skip-ocr --skip-embedding --skip-stage webdav

# 각 단계의 독립 스킵도 가능
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-pdf
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-ocr
cardrag-worker run --reuse-from-run SOURCE_RUN_ID --skip-embedding

# 중단된 부분 실행의 원래 계획을 그대로 재개
cardrag-worker run --resume PARTIAL_RUN_ID
cardrag-worker resume PARTIAL_RUN_ID
```

`--skip-stage`는 `pdf`, `ocr`, `structure`, `embedding`, `export`, `webdav` 중에서
여러 번 지정합니다. 앞단 스킵에는 `--reuse-from-run`이 필수입니다. `structure`는
구조와 view 생성을, `export`는 완성 DB/vector/manifest 재생성을 함께 생략합니다.
기존 결과의 파일·해시·입력·파생 계약을 검사하고 호환되지 않으면
`skip_artifact_missing`, `skip_artifact_incompatible`, `skip_source_unavailable`로
중단합니다. 생략한 단계를 자동 실행하거나 누락 임베딩을 0벡터로 채우지 않습니다.
새 PDF를 수집하면서 OCR만 생략할 때는 지정 source에 동일 PDF의 OCR가 있어야 합니다.
현재 view의 텍스트나 모델이 바뀌면 임베딩 생략도 거절됩니다.

PDF 스킵은 기존 목록의 재처리입니다. 카드사 discovery/다운로드/HTTP 재검증을
실행하지 않으며, 원래 수집 시점·이력·카드사 실패 상태를 유지합니다. 새로운
수집 성공이나 단종 grace로 계산하지 않고 정상 배치의 baseline을 갱신하지 않습니다.
원본 run 상태는 바꾸지 않으며 보존 중인 부분 실행의 source 참조를 정리에서 보호합니다.

부분 실행의 기본 게시 채널은 `candidate-009`입니다. `--publish-channel stable`은
기존 stable 승인과 현재 stable generation에 대응하는 source를 요구합니다.
`--skip-stage webdav`는 OCR cache 업로드와 GC까지 모든 원격 쓰기를 차단합니다.
결과는 `status=local_only`, `published=false`, `local_artifacts`로 반환하며,
기존 DB enum의 `interrupted` 상태와 별도 완료 receipt로 로컬 완료를 구분합니다.
원격 게시 성공이나 `ready_publish`로 기록하지 않습니다. 게시 대상 변경은 새 run으로
진행합니다. 부분 실행은 기존 `resume-publication`으로 게시 계획을 우회할 수 없으며
`run --resume`/`resume`로 재개합니다. 모든 단계를 생략한 로컬 실행은 결과에 `no_op=true`로 표시합니다.

각 run의 `execution-plan.json`과 `execution-result.json`에 source, 실행/재사용/생략,
외부 호출수, 소요 시간을 남깁니다. 재개하면서 원래 옵션을 바꾸면 거절합니다.
`--dry-run`은 출처와 현재 캐시를 읽어서 검사하며 provider 호출, run 생성, 게시,
정리·GC를 수행하지 않습니다. 재생성 이후에야 알 수 있는 입력 호환성은 실제 실행의
해당 단계 진입 시 다시 검사합니다. 원본 OCR/구조의 작은 checkpoint만 복사하며
운영 DB 전체를 복제하지 않습니다.

요약 응답 분류만 수정한 경우에는 MCP 변경 자체를 검증하면 됩니다. 이 옵션을
추가했다는 이유로 전체 PDF/OCR 작업을 다시 실행할 필요는 없습니다.
