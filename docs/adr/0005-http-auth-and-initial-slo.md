# ADR-0005: HTTP MCP, 인증과 초기 SLO

- 상태: Accepted (단일-host 개발 기준선)
- 일자: 2026-08-12

## 결정

MCP는 Streamable HTTP `/mcp`로 제공하고 container `0.0.0.0:8000`, host
`127.0.0.1:8000`만 노출한다. TLS와 외부 hostname은 별도 Nginx Proxy Manager가 담당한다.
공개 probe는 `/health/live`, `/health/ready`뿐이며 metrics는 실제 loopback peer에만 허용한다.

Keycloak `cardrag` realm의 RS256 JWT를 local JWKS로 검증한다. exact issuer/audience, alg, kid,
signature, exp/nbf/iat와 tool별 `search`/`source_pdf` scope를 fail closed로 확인한다. DCR과
self-registration은 끄고 사람은 Authorization Code+PKCE, service는 Client Credentials를 쓴다.
Access token 갱신은 호환 client가 수행하고 refresh rotation/reuse/revoke/90일 idle 계약을
Keycloak 환경 검증 절차로 제공한다.

온라인 동시성은 5, 서버 request timeout은 45초로 시작한다. 품질을 위해 임의의 5초 목표를
강제하지 않으며 초기 운영 목표는 검색 P95 30초 이하, timeout률 1% 미만이다. 15분 window에서
P95 30초 또는 error/degraded 비율 5%를 넘으면 경고한다. 이 값은 장애를 bounded하게 만드는
초기 한도이지 대규모 corpus의 최종 약속이 아니다. 실제 corpus·OpenRouter·host 측정 후 새 ADR로
조정한다.

합성 admission probe 100건은 동시 실행이 정확히 5로 제한되고 오류 없이 완료되는지 검사한다.
해당 수치는 실제 PostgreSQL/embedding latency를 주장하지 않는다.

세 container target은 동일한 검증된 Python distribution을 사용하되 entrypoint가 역할별 명령만
허용한다(MCP=`cardrag-mcp`, worker=`cardrag-worker`/`codex`, admin=`cardrag`/volume init).
따라서 공개 image의 Python source는 누구나 검사할 수 있다는 전제로 secret·원문 PDF·운영 자료를
절대 image에 넣지 않는다. 이는 코드 은닉 경계가 아니라 오실행과 권한 확장을 막는 실행 경계다.

worker의 Codex 0.147 OCR은 root 소유 `/etc/codex/config.toml`의 `ocr` permission profile을
명시적으로 선택한다. 이 profile은 최소 runtime과 현재 rendered workspace만 읽고, 쓰기·workspace
밖 읽기·network를 거부한다. user config/rules는 무시하고 shell/multi-agent/view-image/web/app 계열
tool은 비활성화한다. Linux image에는 bubblewrap을 설치한다. Docker의 기본 seccomp/AppArmor가
nested unprivileged user namespace를 막으므로 worker만 해당 두 outer profile을 `unconfined`로 두되,
non-root, `cap_drop: ALL`, no-new-privileges, read-only rootfs를 유지하고 Codex의 내부
bubblewrap+seccomp profile을 CI canary로 검증한다. canary가 rendered read, outside/secret read 거부,
write 거부, network socket 거부 중 하나라도 만족하지 못하면 image를 배포하지 않는다.

filesystem은 객체와 게시 generation을 별도 named volume으로 분리한다. worker는 object/build만
쓰기 가능하고 generation은 읽기 전용이며, admin publisher만 generation을 쓸 수 있다. MCP는
object/generation을 모두 읽기 전용으로 마운트한다. PostgreSQL도 owner/admin, worker, MCP 계정을
분리하고 worker에는 pipeline 처리와 metric 누적에 필요한 표별 DML만 준다.
generation/active pointer, pipeline 운영 상태, audit append/delete와 metric delete 권한은 주지 않는다.
90일 audit·1년 metric 삭제와 generation 보존정책 정리는 owner DSN을 쓰는 별도 04:00 admin
one-shot이 함께 담당한다. 성공 최근 3개와 active/pin은 보호하고 7일 지난 failed candidate만
DB/file에서 정리한다.

공개 release는 하나의 tag가 세 target을 역할별 tag
(`${version}-{role}`, `${version}-{role}-sha-${short_sha}`)로 만들고 각각의 digest를
keyless Cosign 서명한다. 통합 manifest가 MCP/worker/admin의 digest와 검증된 signature evidence를 모두
포함해야 release가 완성된다.

## 검증

- `tests/unit/test_auth.py`
- `tests/unit/test_http_contract.py`
- `tests/unit/test_service.py`
- `tests/load/test_local_load.py`
- `tests/integration/test_database_roles.py`
- `reports/benchmarks/local-search-load.json`
- `deploy/keycloak/cardrag-realm.json`
