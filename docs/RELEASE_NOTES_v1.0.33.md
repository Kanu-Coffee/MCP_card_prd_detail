# CardRAG v1.0.33

## 변경 사항

- 카드사별 PDF discovery를 제한 병렬 실행하고 HTTP client·cookie·rate limit을 카드사별로 격리한다. 기본 동시 실행 4개, discovery 전체 timeout 300초다.
- 원천 연결/목록/다운로드 실패 카드사는 경고와 구조화된 보고서를 남기고 넘어간다. 정상 카드사 수집이 끝난 뒤 OCR로 진행한다. 일부 실패는 `degraded`, 모두 실패 또는 공용 storage 오류는 실패로 처리한다.
- 실패 카드사의 이전 stable 문서·PDF/OCR·개정 관계·검색 DB를 보존하고 단종 판단을 동결한다. 재처리 대상은 복구 때까지 미완료로 유지한다.
- PDF 내용 기준의 불변 OCR variant 캐시를 도입한다. 문서별 기존 OCR SHA pin을 유지하며 provider/model 변경만으로 동일 PDF를 재OCR하지 않는다. 이전 캐시의 검증·이전·선택 복구 및 수동 재처리를 지원한다.
- OpenCode OCR provider와 CLI 1.18.34를 제공한다. opt-in overlay의 기본 모델은 `alibaba-token-plan/qwen3.8-flash`, effort `medium`이며 전용 agent의 도구/권한을 거부한다. API key와 external OCR 승인이 필요하다.

## 검증과 운영 상태

2026-10-07 운영 구현 `6b42a1a55899899be8fdab5d3b3555ef7b5390da`에서 7개 카드사 수집과 신한 기존 931개 보존을 확인했다. 5,513개 문서/615,840개 검색 view generation이 게시되어 MCP가 서비스한다. 재개 run의 OCR 호출은 0건이었다. 용량 gate에서 중단된 첫 run은 미사용 이미지/빌드 캐시 정리 후 정상 재개됐다. 이 증거는 최종 공개 후보 이미지와 다른 source의 운영 참고 증거다.

OpenCode 운영 설정은 승인 후 적용했고 합성 1페이지 실제 OCR을 1회 검증했다. 다음 예약 배치부터 신규 PDF에 사용하며 기존 OCR은 재사용한다. 신한 원천 접속 장애는 계속되어 최신화는 미완료다. 장애 격리로 다른 카드사 처리는 지속한다.

공개 릴리스는 exact Git source/CI, OCI digest·provenance·SBOM, 보안 검사와 서명·자산 checksum으로 검증한다. 후보 전체 Worker run/12도구 실호출/gold 품질 평가/공개 이미지 운영 cutover를 수행한 것으로 주장하지 않는다. 기존 운영 이미지 교체를 이 릴리스가 자동 수행하지 않는다.

## 설치·전환

Worker/core/MCP 버전은 모두 1.0.33이다. 공개 이미지 역할 태그는 `1.0.33-worker`, `1.0.33-mcp`이며 immutable digest는 GitHub Release 자산을 기준으로 사용한다. [릴리스 절차](RELEASING.md), [운영 설정](OPERATIONS.md), [복구](RECOVERY.md)를 참고한다.

기존 OCR 이전은 검증된 mapping을 사용하고, 같은 stable에 migration을 반복 apply하거나 epoch를 올리지 않는다. OpenCode는 `deploy/worker/compose.opencode.yaml` opt-in으로 활성화한다. 기존 cache가 있는 문서의 재OCR·대형 state clone·원격 GC는 전환 필수 작업이 아니다.
