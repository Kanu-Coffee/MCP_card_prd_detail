# CardRAG 문서

현재 보호해야 할 운영 기준은 v1.0.9이며 v1.0.13은 v1.0.11 데이터 계약을 유지하는
별도 recovery candidate에서만 개발·검증합니다. 운영 컨테이너, volume, stable pointer,
`/opt/cardrag/current`와 LibreChat 경로는 명시적 cutover 승인 전까지 변경하지 않습니다.

## v1.0.13 읽는 순서

1. [프로젝트 README](../README.md)
2. [v1.0.12 SIGBUS 장애 분석과 v1.0.13 수정](V1_0_13_INCIDENT.md)
3. [v1.0.13 offline recovery, migration과 rollback](V1_0_13_MIGRATION.md)
4. [v1.0.12 embedding 재시도 장애 기록](V1_0_12_INCIDENT.md)
5. [v1.0.11 candidate migration baseline](V1_0_11_MIGRATION.md)
6. [archive와 운영 state 관리](V1_0_11_ARCHIVE_MANAGEMENT.md)
7. [v1.0.10 구조·임베딩·exact 검색 baseline](V1_0_10_STRUCTURE_EMBEDDING.md)
8. [v1.0.10 acceptance baseline](V1_0_10_ACCEPTANCE.md)
9. [v1.0.10 candidate acceptance receipt baseline](V1_0_10_CANDIDATE_ACCEPTANCE.md)
10. [Chainguard/Wolfi 컨테이너 런타임 baseline](V1_0_10_CONTAINER_RUNTIME.md)
11. [배포 overlay와 secrets](../deploy/README.md)

## 기존 운영 기록

- [v1.0.9 migration](V1_0_9_MIGRATION.md)
- [v1.0.8 OCR 종료 조사](V1_0_8_OCR_INCIDENT_2026_08_28.md)
- [simple runtime](SIMPLE_RUNTIME.md)
- [legacy OCR adoption v2](LEGACY_DATA_KIT_ADOPTION_V2.md)

Worker는 유일한 WebDAV writer이고 MCP는 검증된 generation을 로컬 read-only로
서비스합니다. Live Worker SQLite를 직접 여는 외부 monitor는 금지합니다. v1.0.13
candidate acceptance는 stable 전환이나 구버전 cleanup을 자동 승인하지 않습니다.

## 라이선스

프로젝트 자체 코드와 문서는 [Apache License 2.0](../LICENSE)으로 공개됩니다.
외부 구성요소에는 각 구성요소의 라이선스가 적용되며 자세한 내용은
[제3자 고지](../THIRD_PARTY_NOTICES.md)에 기록합니다.
