# 기여 안내

작은 변경은 재현 방법과 기대 동작을 설명하는 PR로 제안하십시오. 데이터 형식, 공개 도구,
카드사 수집 범위가 바뀌는 작업은 호환성 영향과 검증 계획도 함께 설명해 주십시오.

## 개발 환경

Python 3.12–3.14와 uv를 사용합니다. 저장소의 `uv.lock`을 기준으로 설치합니다.

```bash
uv sync --frozen --all-packages --all-extras
uv run --all-packages ruff check packages apps tests/runtime_v1 tools
uv run --all-packages ruff format --check packages apps tests/runtime_v1 tools
uv run --all-packages mypy packages/cardrag-core/src apps/cardrag-worker/src apps/cardrag-mcp/src
uv run --all-packages pytest
```

CI는 이 검사에 더해 Compose 렌더링, 이미지 실행 계약, workflow 정적 검사와 보안 검사를
수행합니다. Linux 파일 lock·signal·namespace 관련 테스트는 Linux 환경에서 검증하십시오.
일반 테스트는 mock provider와 임시 상태를 사용합니다. 실제 카드사·OCR·임베딩·WebDAV
검증은 비용과 원격 변경이 있으므로 별도로 구성한 후보 환경에서만 수행합니다.

## 코드 구성

| 경로 | 책임 |
|---|---|
| `packages/cardrag-core` | 해시, manifest, 경로, 비밀 파일, WebDAV 공통 계약 |
| `apps/cardrag-worker` | 수집, OCR, 구조화, 임베딩, 게시와 재개 |
| `apps/cardrag-mcp` | generation 검증, 검색, 도구와 읽기 전용 API |
| `tests/runtime_v1` | 서비스 간 계약, 배포와 공급망 검증 |
| `tools` | 오프라인 상태·archive 검증 도구 |
| `deploy` | 서비스별 Compose와 선택적 systemd 템플릿 |

기존 artifact의 schema 이름과 identity는 공개 데이터 계약입니다. 수정 전
[DATA_FORMATS](docs/DATA_FORMATS.md)를 읽고, 이전 데이터의 재사용·검증·복구 영향을
검토하십시오. 현재 Worker state DB를 직접 열어 디버깅하지 않습니다.

## 제출 전

변경한 동작을 검증하는 회귀 테스트를 추가하고 관련 문서를 함께 갱신합니다. 실제 PDF,
운영 설정, 인증, 조사·발송 이력과 원본 로그는 커밋하지 않습니다. 실패 로그를 공유할 때도
토큰과 사용자 데이터가 포함되는지 확인하십시오. 실행별 검증 보고서는 공개 fixture와
구분해 저장소 밖에서 보관합니다. 보안 문제는 [SECURITY](SECURITY.md)를 따릅니다.
