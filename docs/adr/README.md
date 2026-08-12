# Architecture decision records

이 디렉터리는 구현 과정에서 Codex에 위임되어 채택된 기술 결정을 기록한다. 상태가 `Accepted`인
ADR은 현재 v1 구현의 기준이며, 실제 카드사·외부 모델·운영 호스트 검증으로 근거가
바뀌면 기존 파일을 덮어쓰지 않고 새 ADR로 대체한다.

| ADR | 결정 |
|---|---|
| [0001](0001-storage-search-and-generations.md) | PostgreSQL/pgvector와 불변 file generation |
| [0002](0002-identities-and-lineage.md) | issuer 필수 content identity와 provenance |
| [0003](0003-models-structure-and-quality-gates.md) | OCR·구조·embedding 기준과 정량 gate |
| [0004](0004-durable-jobs-and-publication.md) | lease/fencing job과 generation 게시 |
| [0005](0005-http-auth-and-initial-slo.md) | HTTP MCP, Keycloak, 동시성·timeout·초기 SLO |
