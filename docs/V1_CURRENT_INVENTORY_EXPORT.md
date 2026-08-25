# v0.2.1 OCR을 v1 Worker로 이관하기

이 절차는 보존 중인 v0.2.1 PostgreSQL/CAS와 검증된 raw legacy data-kit을
읽어 v1 Worker의 adopted OCR cache 입력을 만든다. current exporter는
`REPEATABLE READ READ ONLY` PostgreSQL 트랜잭션을 사용하고, data-kit
exporter는 SQLite를 `mode=ro&immutable=1` 및 `query_only`로 연다. 어느 쪽도
원본 DB/CAS/data-kit에 쓰지 않는다.

Current export는 v0.2.1 환경변수가 그대로 설정된 보존 checkout/runtime에서
실행한다. Raw data-kit export에는 PostgreSQL/CAS 환경변수가 필요 없다. 출력
디렉터리는 미리 만들고 각 원본 root 밖에 두어야 하며, 두 명령 모두 기존 출력
파일을 덮어쓰지 않는다.

## 1. 현재 published 자료 export

현재 활성 generation의 최신 상품 PDF/OCR을 우선 자료로 export한다.

```bash
uv run --package cardrag-legacy cardrag-legacy legacy export-current-inventory \
  --output /archive/cardrag-cutover/current-published.jsonl
```

다음 조건을 모두 만족해야 JSONL이 생성된다.

- `active_generation`이 `published`이고 `generation_documents.is_latest=true`
  건수가 generation의 최신 문서 수와 일치한다.
- issuer/product 조합과 문서 ID가 유일하고 처리 identity가 완전하다.
- PDF/OCR object key가 DB SHA-256의 정확한
  `sha256/<prefix>/<digest>` CAS 경로다.
- 실제 object가 storage root 아래의 symlink 없는 일반 파일이다.
- PDF 크기와 OCR 페이지 수가 DB ledger와 일치한다.

JSONL에는 절대 PDF/OCR 경로가 기록된다. Docker에서 Worker adoption을 실행할
때 해당 경로가 가리키는 보존 storage root를 **동일한 절대경로**에 read-only로
mount해야 한다.

## 2. raw legacy data-kit inventory export

현재 published 자료에 없는 상품은 raw data-kit으로 보완할 수 있다. old
PostgreSQL import/finalize나 1.26 GiB normalized bundle 생성은 필요하지 않다.

```bash
uv run --package cardrag-legacy cardrag-legacy legacy export-data-kit-inventory \
  --source /srv/cardrag-legacy/cardrag-conveyor-data \
  --output /archive/cardrag-cutover/legacy-data-kit.jsonl \
  --rejected-output /archive/cardrag-cutover/legacy-data-kit-rejected.jsonl
```

Exporter는 다음을 검증한다.

- 전체 tree에 symlink/special file이 없고 control/상대경로가 source 밖으로
  이탈하지 않는다.
- `DATA_PACK_MANIFEST.json`의 1,592건과
  `cardrag_master_manifest.v2` 및 `inventory.sqlite3`의 모든 필드가 일치한다.
- `inventory.sqlite3`와 `ocr_inventory.sqlite3`가 byte-identical이고 SQLite
  integrity/schema/index-state coverage가 유효하다.
- DB가 `done`, 빈 error, `is_latest=1`로 고정한 1,567건에서 각 상품별 latest가
  정확히 하나다. 같은 날짜의 v10 OCR이 불완전해 v9를 유지한 알려진 예외를
  버전 문자열만으로 뒤집지 않는다.
- 선택 PDF를 실제 SHA-256으로 확인하고 PDFium으로 모든 페이지를 열며,
  metadata identity와 OCR SHA/문자수/UTF-8/`## Page 1..N`/canonical join을
  확인한다. PDF는 path cache를 사용해 불필요하게 두 번 hash하지 않는다.

구조나 SQLite↔master ledger가 다르면 출력 없이 전체 실패한다. 문서 하나의 OCR
품질이 v1 strict contract를 만족하지 않으면 원문을 고치지 않고 해당 행만
`legacy-data-kit-rejected.jsonl`에 기록한다. 2026-07-12 보존본 실측 결과는
accepted 727건(KB 677, 우리 50), rejected 840건(`ocr_noncanonical` 831,
`ocr_short_page` 9)이며 Worker 재검증도 727 accepted, conflict/error 0이다.

9개의 short-page 판정 중 3개 문서는 marker만 있고 실제 본문이 빈 페이지를
포함한다. 따라서 `minimum_chars_per_page=1`로 일괄 완화하면 빈 페이지까지
채택하게 되어 현재는 20자 기준을 유지한다. 또한 이 release에는 서명된 전체
파일 checksum manifest가 없다. Export의 data-pack/master/SQLite SHA는 정확한
archive snapshot identity이지만 원 제작자 서명이나 외부 authenticity 증명은
아니다. source를 read-only로 보존하고 export SHA를 별도 승인 기록에 남긴다.

이미 sealed `legacy prepare` bundle과 succeeded import가 존재하는 운영자는
`export-adoption-ledger` 및 `--legacy-bundle/--legacy-ledger` 경로를 대안으로
사용할 수 있다.

## 3. Worker 검증 및 게시

먼저 `--publish` 없이 실행해 receipts와 conflict/error report를 만든다.

```bash
uv run --package cardrag-worker cardrag-worker adopt \
  --current-inventory /archive/cardrag-cutover/current-published.jsonl \
  --legacy-inventory /archive/cardrag-cutover/legacy-data-kit.jsonl \
  --receipts /archive/cardrag-cutover/adoption-receipts.jsonl \
  --conflicts /archive/cardrag-cutover/adoption-conflicts.json
```

Worker는 PDF를 실제로 열고 SHA-256·크기·페이지 수를 확인한 뒤 OCR의
SHA-256, UTF-8, 정확한 `## Page 1..N` marker와 canonical page join을 다시
검증한다. current published inventory가 같은 issuer/product의 서로 다른 legacy
후보와 충돌하면 current가 우선하며 해당 결정은 `blocking=false` report 항목으로
남는다. 같은 source 내부에서 identity가 모호한 충돌과 검증 오류는 blocking이다.

보고서를 검토한 뒤 같은 명령에 `--publish`를 추가한다. Inventory/control 자체가
잘못됐거나 identity가 모호한 blocking 충돌이 남아 있으면 Worker는 전체 게시를
거부한다. 개별 문서의 validation error는 보고서에 남기고 해당 문서만 제외하므로,
서로 독립적으로 검증된 다른 OCR 게시를 막지 않는다. Raw 후보 단위 오류는 앞 단계
rejected ledger로 분리되어 있다. 통과한 OCR만 `origin=legacy_adoption` cache로
WebDAV에 게시되며, 제외된 자료는 첫 정기 Worker run에서 새 OCR 대상으로 남는다.

Docker mount와 secret overlay를 포함한 전체 명령은
[`SIMPLE_RUNTIME.md`](SIMPLE_RUNTIME.md#existing-ocr-adoption)에 있다. 원본
v0.2.1 DB/CAS, raw data-kit, 사용했다면 sealed bundle, 그리고 전체 역사 데이터는
7회 연속 Worker 성공과 별도 승인 전까지 read-only로 보존한다.
