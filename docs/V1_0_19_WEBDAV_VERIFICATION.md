# v1.0.19 WebDAV 주기 검증과 대용량 단일 read-back

v1.0.19는 기존 원격 객체의 검증 성공 이력을 Worker SQLite에 보관하고, 전체 재검증을
**14회 실행·7일·고유 신규 CAS 10GiB 중 먼저 도달하는 조건**으로 수행한다. 신규
`index.sqlite3`·`vectors.f32`는 최종 경로 GET 1회로 SHA-256와 크기를 확인한다.
신규 PDF·OCR CAS와 작은 제어 파일의 임시·최종 검증은 유지한다.

초기 [검토 문서](WEBDAV_VERIFICATION_OPTIMIZATION_REVIEW.md)의 7회 제안을 이 규격의
14회로 변경하고, 대용량 단일 검증과 미게시 파일 복구까지 이번 버전에 포함했다.
변경이 있으면 기존처럼 게시하며, 여러 회차의 데이터 변경을 모아 게시를 지연하지 않는다.

## 설정

Worker와 `resume-publication`은 다음 설정을 읽는다. Compose 환경변수 전달과
`.env.example`, `deploy/simple.env.example`에도 같은 기본값을 제공한다.

| 환경변수 | 기본값 | 동작 |
|---|---|---|
| `CARDRAG_WEBDAV_VERIFICATION_MODE` | `periodic` | `periodic`: 이력 재사용, `strict`: 매 실행 전체 검사 |
| `CARDRAG_WEBDAV_FULL_VERIFY_EVERY_RUNS` | `14` | 전체 검사 후 13회 성공하면 다음 실행에서 검사 |
| `CARDRAG_WEBDAV_FULL_VERIFY_MAX_AGE_DAYS` | `7` | 마지막 전체 검사 성공부터 168시간 |
| `CARDRAG_WEBDAV_FULL_VERIFY_NEW_CAS_GIB` | `10` | 고유 신규 CAS 누적 10,737,418,240바이트 |
| `CARDRAG_WEBDAV_GENERATION_READBACK_MODE` | `final` | `final`: DB/vector 최종 GET 1회, `double`: 이중 GET |
| `CARDRAG_WEBDAV_FORCE_FULL_VERIFY` | `false` | 해당 실행에서 전체 검사 강제 |

회수·일수·GiB는 각각 1–1,000,000 범위의 정수만 허용한다. 잘못된 설정은 시작 시
거부한다. 정책은 corpus/검색 계약 해시나 generation v5 형식을 변경하지 않는다.
Core의 기본 publisher, 독립 preflight와 MCP의 엄격한 검증 동작도 유지한다.
NAS 복원이나 수동 원격 파일 변경 후에는 다음 실행에 `CARDRAG_WEBDAV_FORCE_FULL_VERIFY=true`를
적용하고 완료 후 되돌린다. 포인터·크기·ETag가 모두 같은 외부 복원은 자동 식별할 수 없다.

## 검사 회수와 누적량

- 성공 실행과 `no_change`를 회수에 포함한다. 실패는 회수를 증가시키지 않으며 검사
  기한도 초기화하지 않는다. 같은 run의 재시작·완료 처리는 중복 계산하지 않는다.
- 전체 검사가 성공한 실행은 다음 검사까지의 회수에 포함하지 않는다. 새 게시나 HEAD
  성공만으로 전체 검사 시각을 갱신하지 않는다.
- run 완료 직후 프로세스가 종료되어 회수 기록이 빠진 경우, 다음 실행이 완료된 run과
  정책 원장을 대조해 복구한다. 부분 검사 실패는 `pending`으로 남아 다음 검사를 강제한다.
- 시간은 UTC 기준이다. 시계가 뒤로 이동해 증거가 미래 시각이 되면 재검증한다. 매일
  한 번 실행하면 일반적으로 14회보다 7일 조건이 먼저 도달한다. 배치 중단 중에는 검사하지
  않으며 다음 실행에서 만기 검사를 수행한다.
- 신규 CAS만 용량에 포함한다. DB/vector, 임시 파일, 중복 문서 참조와 재시도 전송량은
  제외한다. 업로드 의도를 먼저 기록하고 실제 원격 바이트 검증 후 한 번만 합산한다.
- NAS 전체 사용률을 검증 조건으로 사용하지 않는다. GC 활성화·보존·grace 정책도
  변경하지 않는다.

## 기존 객체 재사용

`worker-state.sqlite3`에 추가형 `webdav_verification` 테이블을 만든다. 객체 검증 증거,
전체 검사 상태, run 회수, CAS 업로드 원장, generation 복구 journal을 구분해 저장한다.
기존 `publish` 레코드를 검증 증거로 일괄 승격하지 않으며 최초 실행은 전체 검사로
기준을 만든다. 처음 게시하는 빈 채널은 모든 후보 객체의 검증으로 기준을 만든다.

객체 증거는 endpoint·사용자·namespace·정책, 원격 경로, SHA-256·크기에 묶는다.
전체 GET이 성공한 응답의 ETag와 검증 시각을 저장한다. 일반 실행에서는 작은
pointer·READY·manifest의 해시·상호 참조를 확인한 뒤 기존 객체를 HEAD로 확인한다.
크기·ETag가 같고 증거가 유효하면 본문을 받지 않는다. ETag 변화·크기 변화·증거 부재와
오래된 객체 증거는 전체 GET으로 확인한다. HEAD의 성공으로 `verified_at`을 갱신하지 않는다.

ETag가 없거나 약해도 주기 정책을 사용할 수 있다. 따라서 **같은 크기이고 ETag에
드러나지 않는 저장장치 손상은 다음 전체 검사까지 탐지가 지연될 수 있다.** 이 지연을
허용하지 않는 환경은 `strict`를 사용한다. 일반 ETag를 SHA-256와 직접 비교하지 않는다.

현재 세대 확인과 CAS 재사용은 같은 검증 이력을 사용한다. 같은 실행의 동일 객체는
진행 중인 검증도 공유한다. 로컬 seal의 문서별 참조 수 검증을 완료한 후 원격 CAS
작업만 해시·크기별로 합친다. 전체 검사와 HEAD 작업은 4개 worker, CAS 게시 작업은
기존 16개 상한을 유지한다. Core publisher의 콜백은 원래 이벤트 루프에서 상태를 기록하고,
네트워크는 기존 작업 스레드에서 처리해 중첩 executor 대기를 피한다.

전체 검사 대상은 현재 활성 세대의 DB/vector와 고유 참조 CAS이다. 새 후보가 있으면
후보의 검증 증거도 결합하며 해당 검사 실행에서 이미 전체 검증한 파일은 다시 받지 않는다.
모든 과거 세대와 미참조 파일을 매번 전수 스캔하지 않는다. `no_change` 완료와 제어 파일
게시 직전에 만기를 다시 확인한다. 포인터 변경·소실 또는 검증 실패는 새 게시/완료를 막는다.
취소·게시 실패 reconciliation과 aggregation acceptance는 엄격한 새 검증을 수행한다.

## DB/vector 단일 검증과 복구

```text
로컬 SHA-256·파일 식별자 검사
→ PUT(고유 임시 경로)
→ pre_commit 파일 식별자 검사
→ MOVE(Overwrite:F)
→ 최종 GET·SHA-256·크기 검사
→ 검사 기한 확인
→ manifest → 검사 기한 확인 → READY → pointer
```

`final`은 generation의 `index.sqlite3`·`vectors.f32`에만 적용한다. 임시 GET을 생략하므로
정상 경로에서 대용량 파일마다 read-back이 한 번이다. 신규 CAS와 제어 파일은 이중 검증을
유지한다. 기존 목적지와 MOVE 409/412 충돌은 검증 없이 신뢰하지 않는다.

검증 전에 임시 파일을 최종 경로로 옮기므로 손상 파일이 불변 경로를 차지할 수 있다.
이를 위해 seal별 시도 수·MOVE 생성 결과·격리 경로를 journal에 기록한다.

1. 원격 본문의 해시·크기 불일치만 복구 대상으로 삼는다. 로컬 소스 변동이나 통신 오류를
   원격 손상으로 간주하지 않는다.
2. 이번 시도의 MOVE 201 생성이 확인되고, 해당 세대의 manifest/READY가 없으며 어떤
   channel pointer도 참조하지 않는 파일만 자동 격리한다. 소유권이 불명확하면 중단한다.
3. `v1/.incoming/publish/<uuid>.tmp`로 `MOVE(Overwrite:F)`하여 격리한다. 즉시 삭제하지
   않으며 기존 GC 활성화·grace 정책을 따른다.
4. 같은 sealed 파일로 재업로드 한 번을 허용한다. 파일·seal별 총 시도 한도는 2회이고
   재시작이나 검증 회수 설정 변경으로 초기화되지 않는다. 두 번째 손상도 격리한 후 중단한다.
5. 격리 MOVE 직후 중단되면 journal과 원본/격리 경로 존재를 대조한다. 양쪽이 모두 있거나
   모두 없는 모호한 상태는 중단한다. 재시도 한도가 끝났어도 미완료 격리는 먼저 정리한다.

기존/충돌 목적지, 이미 제어 파일이 있는 세대, 활성 세대는 자동 덮어쓰기·격리하지 않는다.
새 generation이 완전히 검증되기 전에는 pointer를 바꾸지 않으며 MCP는 기존 세대로 서비스한다.

## 계측과 인수

`runs/<run-id>/reports/performance.json`의 기존 PUT/GET 계측을 유지하고 다음 정책
계측을 `webdav.policy_*`로 추가한다.

- `current`, `full_audit`, `existing`, `existing_generation`, `temporary`, `final`,
  `collision`, `head`별 요청 수·누적 시간. 본문 검증에는 소비한 본문 바이트도 기록한다.
- `same_run_reused`, `receipt_reused`, `avoided_get_bytes`.
- `full_audits_started/completed`, `trigger_baseline/age/runs/bytes/incomplete/strict/forced`.
- `runs_since_full`, `new_cas_bytes`, `full_verified_at`.
- `generation_quarantined`, `generation_retries`, `current_wall_seconds`, `gate_wall_seconds`.

`metrics.settings.webdav_verification`에는 적용 정책을 기록한다. 누적 요청 시간은 병렬
실행 시간이 겹치므로 실제 전체 경과 시간과 합산하지 않는다. 최초 전체 검사, 일반 실행,
`no_change`, 만기 실행을 분리해 비교한다. 부수적인 HEAD/MOVE/제어 파일 비용도 남는다.

로컬 테스트는 주기 경계·검증 이력 재사용·손상·중단·재시작·격리·포인터 보호와 실제 v5
번들 게시에서 DB/vector 각각 GET 1회, 다음 실행의 기존 데이터 GET 0회를 확인한다.
전체 pytest, mypy, Ruff, lock·Compose 설정 및 변경 파일 비밀 값 검사를 함께 수행한다.
실제 NAS의 지연·대역폭, 대표 빌드 2회 연속 75분 이하, 컨테이너 피크 6GiB 이하와
평시 검증 본문 약 70% 절감은 운영 인수 측정 대상이며 로컬 테스트의 달성 성과로 보고하지 않는다.

2026-09-06 로컬 검증 결과: 전체 테스트 **1,797개 통과(43.03초)**, mypy 소스 76개,
Ruff lint·format, 오프라인 lock 검사, Compose 기본값·명시적 설정 전달 검증을 통과했다.
기존 OCR fallback 장애 주입 테스트의 예상 RuntimeWarning 6건이 출력됐다.
변경 파일 Gitleaks 검사에서 비밀 값은 발견되지 않았다. 운영 배포와 NAS 인수 측정은 수행하지 않았다.

## 전환과 롤백

격리된 candidate 환경에서 최초 전체 검사, 평시 실행, 만기·장애 복구를 확인한 뒤 운영
전환한다. 아래 두 설정으로 기존 검증 강도를 복원할 수 있다.

```dotenv
CARDRAG_WEBDAV_VERIFICATION_MODE=strict
CARDRAG_WEBDAV_GENERATION_READBACK_MODE=double
```

정책 설정을 바꿔도 generation/Serving DB/vector 형식과 기존 state 테이블을 변경하지
않는다. 새 검증 테이블을 삭제할 필요는 없다. 증거가 없으면 전체 검사로 다시 시작하며
복구 journal은 검사 임계치 설정과 독립적으로 유지한다.

기존 공개 release workflow는 v1.0.14 전용 증거 검증으로 고정되어 있다. 이번 구현은
그 제한을 완화하거나 원격 배포·이미지 게시·stable 전환을 실행하지 않는다.
