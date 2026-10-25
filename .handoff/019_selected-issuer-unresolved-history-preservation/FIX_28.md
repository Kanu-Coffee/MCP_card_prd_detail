# 019 FIX_28 — 정리 후 롤백 release의 복구점 안내 정합성

2026-10-22 Codex, Reviewer. FIX_26/REPORT 및 실제 정리 결과를 검토해 발행한다. 공간 회수와 현재 운영/MCP/10시 예약은 정상이나 아래 복구 안내가 낡아 있다. 현재 배치를 막거나 새 Worker 이미지를 요구하는 결함은 아니다.

## 확인한 P2 문제

보존된 `/opt/cardrag/019-fix23-e9b9cdca1a22/operations/deployment.json`의 snapshot_directory는 `/state/deployment-backups/019-fix23-e9b9cdca1a22`다. FIX_26에서 해당 snapshot은 삭제했으므로 실제 존재하지 않는다. 현재 release의 metadata는 새 `019-fix24-current-20261022`를 정확히 가리키지만, rollback release를 다시 활성화해 그 metadata를 복구점 근거로 읽으면 삭제된 경로를 참조한다. 롤백 release의 previous_release도 삭제된 fix21을 가리키므로 과거 배포 이력과 현재 사용 가능한 rollback 경로를 구분해야 한다.

## 보완 범위

- 기존 배포 이력은 보존하되, 현재 사용 가능한 recovery snapshot을 명확하게 가리키는 별도 안내/검증 경로를 추가한다. 역사적 snapshot_directory를 아무 설명 없이 새 snapshot으로 덮어 당시 복구점이었던 것처럼 만들지 않는다.
- active 및 rollback release에서 동일한 실제 protected snapshot1개를 발견·검증할 수 있게 한다. 새 snapshot과 rollback image의 schema/계약 호환성을 확인하지 않고 해당 DB 복원을 안전하다고 선언하지 않는다. 코드 image 롤백과 DB snapshot 복원은 구분한다.
- snapshot-retention/현재 deployment metadata를 기준으로 현재 복구점을 해결하고 실제 존재/SHA/크기/SQLite 검사를 확인하는 기존 방식이 있으면 재사용한다. 삭제된 snapshot·release는 사용 불가한 역사 경로임을 명시한다. 큰 snapshot을 다시 복제해 다중 보존으로 되돌리지 않는다.
- 복구 안내를 고칠 뿐 실제 rollback·DB 복원·순회 cursor 변경·timer 중지/실배치 시작은 하지 않는다. 예약 KST10시/round_robin/직접 연결 옵션과 active image를 유지한다. 운영 Python 변경이나 새 image는 필요 없다.

## 검증과 보고

active/rollback 양쪽의 현재 복구 안내에서 존재하는 protected snapshot을 확인하고, 삭제된 경로가 현재 복구 단계로 선택되지 않음을 검증한다. 기존 latest run/MCP stable 및 timer를 보존하고 FIX_28_REPORT.md에 실제 수정 파일·검증·호환성 한계를 남긴다.

추가 절차 관찰: FIX_26은 `docker builder prune --all --force`로 호스트 builder cache 전체를 정리했다. 보고서에 공개돼 있고 현재 실행 서비스 장애는 없지만, CardRAG 후보만 좁혀 정리하려던 원칙보다 넓은 범위다. 이미 삭제된 cache를 다시 만들 필요는 없다. 향후 정리 안내에는 공유 builder cache의 타 프로젝트 영향과 표적/기간/보존량 기준을 명시하고 전역 prune을 상시 정책으로 추가하지 않는다.
