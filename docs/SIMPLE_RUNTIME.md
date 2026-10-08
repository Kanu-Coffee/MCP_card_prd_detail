# 운영 문서 안내

systemd Worker 서비스가 참조하는 운영 문서 진입점입니다.

- [설치·환경 설정·예약 운영](OPERATIONS.md)
- [릴리스와 운영 전환](RELEASING.md)
- [WebDAV 자료 복구](RECOVERY.md)
- [v1.0.34 발행·운영 검증 보고서](../.handoff/012_v1034-release-cutover/REPORT.md)

실제 설치 경로는 `/opt/cardrag/current`입니다. MCP와 Worker의 상태 볼륨을 공유하지 않으며, 한 번 종료된 Worker의 성공 여부는 terminal 결과와 게시 generation으로 확인합니다.
