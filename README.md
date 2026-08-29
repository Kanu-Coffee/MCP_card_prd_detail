# CardRAG v1.0.10 candidate

CardRAG는 카드사 상품안내장 PDF를 구조 보존형 검색 generation으로 만드는 두
프로세스 서비스입니다.

```text
finite Worker -> immutable WebDAV generation -> always-on read-only MCP
```

v1.0.10 candidate는 현재 운영 중인 v1.0.9 Worker, image, state volume, 설치 pointer,
WebDAV stable channel과 LibreChat 경로를 변경하지 않습니다. stable cutover와 구버전
정리는 candidate acceptance 보고 뒤 별도 승인이 필요합니다.

## v1.0.10 핵심

- 검증된 v1.0.9 PDF/OCR cache는 read-only seed와 기존 reuse key로 재사용합니다.
  1,536D Small cache와 별도 legacy Qwen vectors는 재사용하지 않습니다.
- OCR 원문을 contract revision별 ROOT/section/item/leaf/table/footnote 구조로 만들며,
  비공백 원문 coverage 100%와 cross-contract context 혼합 0건을 seal gate로
  검사합니다.
- `qwen/qwen3-embedding-8b`, 4,096D FP32, L2 profile을 사용합니다. 문서에는
  instruction을 붙이지 않고 질의에만 Qwen 검색 instruction을 붙입니다.
- 모든 활성 구조 view를 candidate pruning 없이 정확 내적으로 채점합니다. Lexical은
  추가 evidence shadow lane이고 v5 순위에 RRF나 reranker를 반영하지 않습니다.
- v5 vectors는 `vectors.f32` sidecar에 저장하고 SQLite row, manifest와 READY에
  hash/size/profile/count를 결속합니다.
- MCP는 `cardrag.serving-db.v4`와 `cardrag.serving-db.v5`를 dual-read하며 request
  pinning과 last-good activation으로 v4↔v5 rollback을 지원합니다.

기존 `search_evidence`, `get_evidence`, `get_product`, `get_source_pdf`,
`get_source_page`에 `search_contracts`, `get_contract_bundle`,
`list_product_revisions`가 추가됩니다.

## candidate 격리

| 항목 | 값 |
|---|---|
| branch | `codex/cardrag-v1.0.10` |
| WebDAV channel | `candidate-v1.0.10` |
| Compose project | `cardrag-v110-candidate` |
| Worker volume | `cardrag-worker-v110-state` |
| MCP volume | `cardrag-mcp-v110-state` |
| MCP bind | `127.0.0.1:18010` |

Candidate Worker는 remote GC를 하지 않습니다. v1.0.9 source volume은 운영 Worker가
terminal인 것을 확인한 뒤 seed command에서만 read-only로 mount합니다.

## 문서

- [구조·임베딩·exact 검색 계약](docs/V1_0_10_STRUCTURE_EMBEDDING.md)
- [v1.0.9 → v1.0.10 candidate migration](docs/V1_0_10_MIGRATION.md)
- [시험과 acceptance gate](docs/V1_0_10_ACCEPTANCE.md)
- [배포 파일](deploy/README.md)
- [운영 문서 색인](docs/README.md)
- [v1.0.9 migration 기록](docs/V1_0_9_MIGRATION.md)

MCP host port는 loopback에만 bind합니다. 외부 공개 시 TLS reverse proxy와 Bearer
token을 사용하고 실제 credential은 repository에 저장하지 않습니다.
