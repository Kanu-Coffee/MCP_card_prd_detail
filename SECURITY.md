# 보안

## 취약점 제보

인증 정보나 취약점 재현 자료를 공개 이슈에 올리지 마십시오. 저장소의 GitHub Security
탭에 비공개 제보 기능이 제공되면 그 경로를 사용하십시오. 비공개 연락 경로가 보이지
않으면 민감한 내용을 제외하고 유지관리자에게 연락 방법을 요청하십시오.

## 배포 경계

MCP는 읽기 전용 WebDAV 계정을, Worker는 게시에 필요한 별도 계정을 사용할 수 있습니다.
MCP에는 원격 변경 API를 노출하지 않습니다. 외부 MCP 접근은 HTTPS와 Bearer token으로
보호하고, 공개 health 응답은 최소 상태만 제공합니다. Bearer 인증은 다중 사용자별 권한
분리가 아니므로 추가 분리가 필요하면 인증 프록시를 사용하십시오.

비밀은 호스트의 접근 제한 파일이나 비밀 관리 시스템에서 주입합니다. Compose의
`*_SECRET_FILE`은 호스트 파일 경로이고, 애플리케이션의 `*_FILE`은 컨테이너 내부 경로입니다.
WebDAV URL에 사용자 이름이나 암호를 넣지 않습니다. `.env`, `auth.json`, private key,
DB, PDF, 로그와 실행별 증빙을 Git이나 이미지 build context에 포함하지 않습니다.
`.gitignore`는 이미 추적 중인 파일이나 Git 과거 이력을 지우지 않습니다.

## OCR와 상태 보호

Worker와 MCP는 서로 다른 상태 디렉터리를 사용합니다. Codex 인증도 Worker 복구 상태와
분리합니다. OCR에 전달되는 PDF 내용은 신뢰할 수 없는 입력입니다. 고정한 Codex CLI
계약은 모델이 호출할 수 있는 shell, 파일 탐색, 웹·앱·플러그인·하위 agent 도구를 끄고
읽기 전용 sandbox와 제한된 child 환경을 사용합니다. 인증은 Codex 부모 프로세스가 읽을
수 있으므로 이 격리가 악성 실행 파일이나 동일 UID의 적대적 프로세스까지 막는 것은 아닙니다.
OCR 출력에서 표준 credential 토큰 형태를 발견하면 본문을 기록하지 않고 실패시킵니다.

Worker 컨테이너의 Codex sandbox는 Linux user/mount namespace를 요구합니다. 해당 역할만
Docker의 seccomp/AppArmor 제한을 완화하며, 전용 UID, capability 제거, 읽기 전용 rootfs,
`no-new-privileges`를 유지합니다. MCP는 이 예외를 사용하지 않습니다.
상태 파일은 symlink와 파일 교체를 검사하고, Worker lock·SQLite 연결의 생명주기를 유지합니다.
진행 중인 Worker DB를 외부 프로세스에서 읽거나 변경하지 않습니다.

## 원격 변경과 검증

Stable generation 게시, 공유 OCR cache 쓰기, 원격 GC는 각각 별도 설정으로 허용합니다.
템플릿은 이러한 변경 권한을 기본으로 끕니다. Candidate는 stable 포인터·공유 cache
쓰기·원격 GC를 허용하지 않습니다. 원격 객체와 로컬 generation은 크기·SHA-256·READY를
검증하며 오류 시 마지막 정상 데이터로 서비스합니다.

공개 릴리스는 source commit, 이미지 digest, SBOM, provenance, signature, 보안 검사와
후보 검증을 연결해야 합니다. 검사를 건너뛰거나 운영 증빙을 그대로 공개하지 마십시오.
[릴리스 절차](docs/RELEASING.md)와 [복구 절차](docs/RECOVERY.md)를 함께 참고하십시오.
