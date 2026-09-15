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
