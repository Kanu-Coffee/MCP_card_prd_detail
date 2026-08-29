# Legacy data-kit OCR adoption v2

`tools/legacy_data_kit_adoption_v2.py` is a one-time, offline operator tool. It
does not run in either runtime image and never publishes to WebDAV. The source
data-kit is validated and read only; `--output` must name a new directory
outside the source.

Run it from a development checkout with the Worker environment available:

```bash
uv run python tools/legacy_data_kit_adoption_v2.py \
  --source /absolute/path/to/cardrag-conveyor-data \
  --output /absolute/path/to/new-adoption-v2-export
```

The export contains `inventory.jsonl`, `normalization-receipts.jsonl`,
`rejected.jsonl`, `export-manifest.json`, and content-addressed normalized OCR
objects. Every file is created with mode `0600`; an existing output path is a
hard failure.

Pass the **export directory**, never its bare `inventory.jsonl`, to the Worker.
The export and the original `source_root` recorded in `export-manifest.json`
must both be mounted read-only at the same absolute paths inside the container.
For the approved v1.0.4 migration input, first pin the sealed manifest identity
and counts before changing file metadata or mounting it:

```bash
export CARDRAG_LEGACY_ADOPTION_EXPORT_DIR=/home/lee/cardrag-archive/cardrag-data-kit-adoption-v2-runtime-verification-20260826
jq -e '
  .schema_version == "cardrag.data-kit-adoption-export.v2" and
  .policy_version == "cardrag.legacy-ocr-adoption.v2" and
  .source_bundle_id == "data-kit-v2-c666a2708e13" and
  .source_bundle_sha256 == "c666a2708e13b8755f52ea91993b36977b3f35355431e9c5de00f20ea5f4ccb6" and
  .selected_documents == 1567 and .accepted_documents == 1558 and
  .rejected_documents == 9 and .exact_documents == 727 and
  .normalized_documents == 831
' "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR/export-manifest.json"
```

Then set the recorded source path only for the adoption command:

```bash
export CARDRAG_LEGACY_SOURCE_ROOT=/home/lee/cardrag-archive/v0.2.1/cardrag-conveyor-hatch-20260712T091451_KST/data-kit/cardrag-conveyor-data

sudo find "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR" -xdev -type d \
  -exec chgrp cardrag {} + -exec chmod 0550 {} +
sudo find "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR" -xdev -type f \
  -exec chgrp cardrag {} + -exec chmod 0440 {} +

adoption_compose=(
  docker compose --env-file /etc/cardrag/worker.env
  -f deploy/worker/compose.yaml
  -f deploy/worker/compose.secrets.yaml
  -f deploy/worker/compose.adoption.yaml
)

"${adoption_compose[@]}" config --quiet
dry_run=$("${adoption_compose[@]}" run --rm worker adopt-legacy \
  "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR" \
  --receipts /var/lib/cardrag-worker/adoption-v2-dry-run-receipts.jsonl \
  --conflicts /var/lib/cardrag-worker/adoption-v2-dry-run-conflicts.json)
printf '%s\n' "$dry_run" | jq -e \
  '.accepted == 1558 and .conflicts == 0 and .errors == 0 and .published == 0'
```

Before enabling publication, prove that this is still a new CardRAG v1 namespace:

```bash
guard=$("${adoption_compose[@]}" run --rm worker adoption-guard)
printf '%s\n' "$guard" | jq -e \
  '.stable_pointer_absent == true and .status == "clear"'
```

`adoption-guard` performs only a read-only existence check for
`v1/channels/stable.json`; it never reads or prints that object's content and
never writes to WebDAV. If the pointer exists, or its absence cannot be proved,
the command exits nonzero. Stop the migration without publishing anything.
The `--publish` path repeats this guard immediately before its first write.

Only after both exact gates succeed, repeat validation with publication enabled.
Publication is a separate remote-mutation approval. The normal and candidate
Compose contracts deliberately set `CARDRAG_OCR_CACHE_MODE=read-only` and
`CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=false`, so the command below must remain
blocked until an operator has separately approved this one-time adoption write.
Stable generation-pointer approval does not grant OCR cache publication, and
OCR cache publication does not grant a stable cutover. After that explicit
approval, first render and inspect the exact write capability without printing
credentials:

```bash
CARDRAG_CHANNEL=stable \
CARDRAG_OCR_CACHE_MODE=read-write \
CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true \
  "${adoption_compose[@]}" config --format json | jq -e '
    .services.worker.environment.CARDRAG_CHANNEL == "stable" and
    .services.worker.environment.CARDRAG_OCR_CACHE_MODE == "read-write" and
    .services.worker.environment.CARDRAG_OCR_CACHE_PUBLICATION_APPROVED == "true" and
    .services.worker.environment.CARDRAG_STABLE_PUBLICATION_APPROVED == "false"'
```

Then run the separately approved publication:

```bash
published=$(CARDRAG_CHANNEL=stable \
  CARDRAG_OCR_CACHE_MODE=read-write \
  CARDRAG_OCR_CACHE_PUBLICATION_APPROVED=true \
  "${adoption_compose[@]}" run --rm worker adopt-legacy \
  "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR" \
  --receipts /var/lib/cardrag-worker/adoption-v2-published-receipts.jsonl \
  --conflicts /var/lib/cardrag-worker/adoption-v2-published-conflicts.json \
  --publish)
printf '%s\n' "$published" | jq -e \
  '.accepted == 1558 and .conflicts == 0 and .errors == 0 and .published == 1558'
```

Then perform a read-only audit from the same sealed export and source mounts:

```bash
audit=$("${adoption_compose[@]}" run --rm worker adoption-audit \
  "$CARDRAG_LEGACY_ADOPTION_EXPORT_DIR")
printf '%s\n' "$audit" | jq -e \
  '.expected == 1558 and .audited == 1558 and .status == "verified"'
```

The audit revalidates the sealed export and then reads every expected adopted
v2 `manifest.json`, `READY.json`, and OCR CAS object. Each remote body must
match the exact canonical control bytes or normalized OCR bytes, SHA-256, and
size derived from that export. A missing, extra-byte, substituted, truncated,
or non-canonical object fails the audit; it performs no WebDAV writes.

Do not add the adoption overlay to the timer or normal Worker run. The source
archive must remain unchanged. Before any publication, the Worker verifies canonical
control files and JSONL hashes, rehashes all four original data-kit controls,
queries the immutable inventory SQLite, and binds every accepted row back to
its SQLite, master-manifest, and OCR-metadata record. It then verifies the PDF,
original OCR, and normalized OCR bytes again. A bare v2 JSONL file, incomplete
document set, substituted PDF, changed source file, or partial candidate error
blocks publication.

Policy `cardrag.legacy-ocr-adoption.v2` permits only two profiles:

- `exact`: reuse the metadata-bound original OCR bytes without changes.
- `strip-exact-generated-prefix-v1`: for Woori only, remove exactly the
  24-byte UTF-8 prefix `# OCR 처리 완료본\n\n` immediately before
  `## Page 1`.

Both the original `source_ocr_sha256` and resulting
`normalized_ocr_sha256` are preserved. The source bundle ID binds the original
DATA_PACK, master manifest, both SQLite inventories, the policy, and the sorted
per-document transformation plan. Other prefixes, other transforms, malformed
page markers, pages shorter than 20 characters, links, and special files are
rejected rather than repaired.
