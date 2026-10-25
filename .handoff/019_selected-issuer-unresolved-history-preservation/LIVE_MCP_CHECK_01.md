# 019 라이브 MCP 적용·신규 OCR 조회 확인

2026-10-21 Codex. 사용자의 운영 볼륨 여부·신규 OCR MCP 호출 테스트 요청에 따라 실제 mount/현재 pointer와 MCP tools/call을 확인했다. 제품 코드·게시 pointer·원장·원격 객체를 수정하거나 배치/provider/timer를 실행하지 않았다. 호스트 operations에 반복 조회용 도구만 추가했다.

## 실제 볼륨/서빙 경로

최신 현대 Worker 컨테이너와 실행 중 `cardrag-mcp`의 docker inspect mount를 대조했다.

- Worker 상태/산출물: `cardrag-worker-state` → `/var/lib/cardrag-worker`, RW.
- 실제 게시: 두 컨테이너 모두 **같은 named volume `cardrag-serving`** → `/var/lib/cardrag-serving`. Worker RW, MCP RO.
- 실제 MCP의 검증/서빙 복제본: `cardrag-mcp-state` → `/var/lib/cardrag-mcp`, RW. 개발용/실험용 볼륨이 아니라 실행 중인 라이브 MCP의 로컬 캐시다.
- MCP 환경은 `CARDRAG_PUBLICATION_TRANSPORT=local`. Worker state를 직접 serving DB로 사용하는 것이 아니라 성공한 통합 세대를 cardrag-serving에 게시하고 MCP가 검증 후 자기 state에 동기화한다. 실패 run은 게시되지 않는다.

Worker stable과 MCP current.json의 generation 모두 `g-10f236331a2744f78980f2b8-9d16cfdbae66`로 일치했다. 따라서 최근 현대 성공 세대가 실제 MCP까지 적용되어 있다.

## 실제 MCP 프로토콜 조회

실행 중인 `cardrag-mcp`의 HTTP `/mcp`에 Bearer 인증을 적용하여 initialize → tools/list → tools/call(`get_source_page`)을 수행했다. 토큰은 컨테이너의 secret 파일에서 메모리로만 읽고 출력/명령 인자/로그/증거 파일에 보존하지 않았다. 직접 내부 repository 함수를 호출한 테스트가 아니다.

| 최근 현대 게시 문서 | document_id | 조회 | 결과 |
|---|---|---|---|
| the Purple osée | doc_0bab913a6e71f31d40db4839b109f3291280d44e756b5ea184ef9f4977295e85 | get_source_page page1 | isError=False, 총2쪽, OCR text3605자 |
| GENESIS CARD 선택제공서비스 | doc_fccf4004609c1ba37e67c3e67a6c246953d4ea6ea79d4b47c784a1efee903311 | get_source_page page1 | isError=False, 총3쪽, OCR text4698자 |

두 문서는 중간점검의 새 current 문서 예시에 해당한다. 기존에 저장된 OCR을 읽는 테스트이며 새 OCR/provider/embedding 요청은 없다. 외부 고객망/TLS/별도 클라이언트의 네트워크 경로를 전부 검증한 것은 아니며 실제 라이브 MCP 서비스 프로토콜과 현재 데이터 적용을 확인했다.

반복용 `/opt/cardrag/current/operations/test-live-mcp.sh`와 `.py`를 준비하고 실제 실행했다. 도구는 위 두 문서의 페이지 정보/글자 수/짧은 preview, Worker stable=MCP current 여부를 출력하고 불일치·조회 오류 시 실패한다. shellcheck 통과. 향후 해당 문서가 새 세대로 교체되면 sample document ID를 새 기준으로 갱신해야 한다.

```bash
/opt/cardrag/current/operations/test-live-mcp.sh
```

## 현대/BC 후속 순서

현재 활성 release는 여전히 `/opt/cardrag/019-fix16-4108e3dac5f5`다. FIX_19 리뷰 통과는 구현 승인이고 새 수정본이 이미 운영 반영됐다는 뜻이 아니다.

현대/BC의 본 배치는 succeeded이므로 전체 수집/OCR/게시를 재실행하는 것보다 새 수정본 배포 후 필요한 metadata recovery/scoped backup 검증을 먼저 완료한다. 성공 run/retained generation을 다시 식별하고 실제 required=bound/unbound0/원격 index completion을 확인한 뒤 롯데·신한 수동 배치로 진행한다. `run-issuer.sh hyundai/bc`를 재실행하는 것만으로 아직 미배포인 scope 결함이 해결되지 않는다.

신한/롯데 proxy 기본 연결은 LIVE_REVIEW_06에서 확인했으나 전체 batch 완주는 아직 남는다. 019를 닫거나 자동 timer를 시작하지 않았다.
