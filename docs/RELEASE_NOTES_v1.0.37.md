# CardRAG v1.0.37

운영테스트 인수를 마치고 카드사별 증분 수집·일일 순회와 복구·백업 개선을 공개하는 소스 릴리스입니다.

## 변경 사항

- 카드사 선택 실행과 독립 collect/process, durable KST 날짜별 순회 및 비선택 카드사 데이터·개정 이력 보존.
- PDF CAS 재사용, 조건부 재검증·90일 분산 감사, HTTP 예산·cooldown 및 카드사 서비스 오류의 bounded retry.
- scoped 백업의 영수증·원격 index 대조, 결손·충돌 진단, 기존 shared cache 제외 및 read-only 상태 조회.
- 중단된 OCR의 안전한 재개, 재사용 산출물 복원, quota/systemic provider 오류의 로컬 PaddleOCR fallback과 진단.
- 같은 날 동일 카드사 최대3회 신규 자동실행, 4번째 차단, 회차 이력 보존 및 미완료 작업의 같은 run 재개.
- CLI 의도된 종료 예외의 단일JSON/exit code 보존, writer/backup lock 기반 복구본 및 용량 점검.
- 매일14:30 Asia/Seoul 예약, Persistent=false, 활성화 전 실제 cardrag 서비스 계정의 config-only 권한 검증.
- 패키지3종·Worker 표시 버전·lockfile을1.0.37로 동기화하고 strict 타입 주석/변수명 정리.

## 릴리스 이미지 보안 갱신

이미지 CI에서 확인한 수정 가능한 취약점에 대응해 Pillow 13.0.0, aiohttp 3.14.5,
certifi 2026.10.23, filelock 4.2.4로 lockfile을 갱신했습니다. Worker와 MCP의 OS
인증서 번들은 수정 버전 20260909-r3 이상을 요구합니다. MCP는 별도 빌드 단계에서
원래 런타임 루트의 서명된 인증서 패키지와 실제 APK 원장을 함께 갱신하여 Python 3.14와
셸 없는 최종 런타임을 유지합니다. 전체 테스트와 이미지 빌드·보안 CI로 재검증합니다.
운영 중인 평가 이미지는 이 소스 발행만으로 교체되지 않습니다.

## 운영 인수와 한계

사용자가2026-10-25 운영테스트를정상판정했다. 수동8사와후속국민·신한·삼성자동순회에서운영/MCP게시및선택범위원격백업을확인했다. 삼성10/25실행은14:30–16:03완료, OCR769/769성공, 전체문서5636건이다. 실제운영이미지는검증된019-fix33계열을유지한다.

우리카드의일시적다운로드서비스오류는정해진재시도후실패로정확히기록한다. 기존범위밖백업대기3건30862bytes와기존미지원문서는인수된정책상상태다. embedding품질warning은hard reject기준통과와구분한다. 전체게시본생성시간개선여지는남아있으며자동8일전수관측을완료했다고주장하지않는다.

[전체handoff종료](../.handoff/STATUS_v1.0.37.md), [019종료근거](../.handoff/019_selected-issuer-unresolved-history-preservation/CLOSURE.md)를참조하십시오. 테스트는종료되며매일14:30실제운영예약은유지합니다.

## 발행 범위

annotated v1.0.37 태그 및 GitHub 소스 릴리스입니다. 기존v1.0.34전용공개이미지workflow를실행하거나새Docker Hub/GHCR이미지·서명·qualification을발행하지않습니다. 운영볼륨/인증/데이터/포인터를초기화하지않습니다. 실제CI·머지·릴리스결과는별도완료보고로기록합니다.
