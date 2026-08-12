# ADR-0001: 저장소, 검색 엔진과 세대 경계

- 상태: Accepted
- 일자: 2026-08-12

## 결정

Durable job·catalog·감사·집계 상태의 단일 기준은 PostgreSQL 17로 한다. 검색은
PostgreSQL `tsvector` lexical branch와 pgvector HNSW ANN branch를 사용하고, 두 branch를
동일한 stable `evidence_id`로 RRF 결합한다. issuer·상품·version/as-of·section filter는
두 branch의 후보 SQL에 동일하게 적용한다. Python에서 vector BLOB 전체를 읽는 exact
scan은 사용하지 않는다.

PDF와 OCR은 SHA-256 content-addressed 외부 volume에 저장한다. 검색 시점의 문서 metadata와
evidence는 `generation_id`별 snapshot으로 고정한다. file generation은 manifest, file
checksum, 품질 보고서와 `READY` seal을 가진 불변 디렉터리이며 PostgreSQL active generation과
함께 검증한다. 요청 하나는 시작 시 세대를 한 번 고정한다.

임베딩은 v1 schema에 맞춰 1,536차원으로 고정한다. 4,096차원 레거시 vector는 비교 자료로만
두고 current coverage로 세지 않으며, v1 정책으로 다시 임베딩한다.

## 이유와 대안

이 구성은 단일 Linux host에서 운영 복잡도를 제한하면서 필터를 ANN 전에 적용하고, 레거시의
2.4 GB Python 전수 scan과 서로 다른 ID를 섞는 hybrid 결함을 제거한다. SQLite FTS5+별도 ANN은
read snapshot에는 단순하지만 mutable catalog와 별도 동기화가 필요해 v1에서 채택하지 않았다.
외부 vector database는 실제 corpus가 단일 host 한도를 넘을 때 재검토한다.

## 검증

- `tests/integration/test_pgvector_search.py`
- `tests/unit/test_hybrid.py`
- `tests/unit/test_generation.py`
- `reports/quality/fixture-gate.json`
