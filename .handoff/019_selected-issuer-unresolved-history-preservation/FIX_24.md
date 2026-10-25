# 019 FIX_24 — OCR 미분류 프로세스 종료의 안전한 진단 보완

2026-10-21 Codex, Reviewer. LIVE_REVIEW_09의 롯데 운영 실패 후 발행한다. 현재 run은 failed이며517개 OCR/174개 checkpoint가 보존돼 있다. 기존 FIX_23의 quota 전용 fallback은 실제 Paddle 전체 문서 성공을 확인했다. 이번 unknown 종료를 quota나 transient로 확정할 원본 증거는 없다. 새 계획 대신 현재019의 작은 관측성 보완으로 진행한다.

## 목표와 범위

다음 nonzero provider 종료 시 어떤 provider/요청 단계에서 어떤 분류 근거를 얻었는지 제한된 구조로 남겨, 현재 stderr 길이/해시만으로 끝나는 진단을 개선한다. 이 변경 자체로 미확정 upstream 장애를 해결했다고 주장하지 않는다.

- 관련 파일: providers.py의 Codex process 처리/분류 및 ProviderSystemicError, contracts.py의 systemic failure record, ocr.py/pipeline.py의 보고서 생성, 관련 provider/계약/실패 보고서 테스트. 실제 구조를 먼저 확인하고 필요한 최소 파일만 변경한다.
- provider 식별과 요청 phase, nonzero returncode, stdout/stderr 길이·해시 및 allowlist 기반 분류 근거를 보존한다. page/window 범위를 추가하면 기존 검증된 숫자만 사용한다. 원시 stderr/stdout, prompt/OCR 본문, URL, 임의 예외 text/args/notes/context, 설정·인증정보를 보고서나 경고에 복사하지 않는다.
- stdout의 오류 신호를 활용할 때는 공식적으로 식별 가능한 구조/오류 필드만 검토한다. 일반 OCR 본문의 단어만으로 quota/auth 등으로 재분류하지 않는다. 검증할 수 없으면 unknown을 유지한다. 성공 exit0의 출력에 실패 분류를 붙이지 않는다.
- 기존 failure record 읽기와 호환되는 선택적 진단을 우선한다. OCR contract/cache key/epoch/model/settings/성공 결과 형식/기존 seal/체크포인트를 변경하거나 DB migration·운영 데이터 수작업 교정을 요구하지 않는다.
- 원래 실패의2512바이트 stderr는 복구되지 않았다. 합성 fixture를 실제 원본 재현이라고 보고하지 않는다. 진단 향상 후에도 알 수 없는 원인은 unknown으로 정확히 보고한다.

## 유지할 정책

unknown을 일괄 fallback·재시도시키지 않는다. 인증/설정/credential/무결성/취소는 기존대로 전파한다. FIX_23의 Codex quota → 로컬 Paddle whole-document 예외, primary partial chunk 보존, fallback 실패 전파를 유지한다. 일시적 오류에 대한 추가 정책은 실제 분류 근거와 별도 검토가 필요하며 이번 범위에서 도입하지 않는다.

## 검증 및 완료 조건

1. nonzero unknown, 인식 가능한 기존 typed 오류, exit0+오해 가능한 stderr header, 임의 OCR 본문, 취소·fallback 실패를 구분하는 테스트를 수행한다. secret sentinel이 stdout/stderr/예외에 있어도 보고서·로그에 유출되지 않아야 한다.
2. 구형 failure record 읽기, 새 선택 필드 검증, provider/phase 및 chunk/page 범위의 정확성, 기존 quota fallback 허용/unknown 차단을 검증한다.
3. 기존 프로젝트 명령으로 관련 테스트·Worker/Core 전체 테스트·ruff·diff 검사를 수행하고 FIX_24_REPORT.md에 정확한 결과를 기록한다.
4. 현재 Git 변경과 기존 handoff를 보존한다. 구현 완료와 운영 배포/실제 resume 완주는 구분한다. 배포한다면 기존 운영 절차대로517건/174개 보존, 동일 run/contract·운영 volume, 실행 lock·최신 독립 복구점 및 저장공간을 확인한다. 이번 계획만으로 배치를 자동 시작하지 않는다.
