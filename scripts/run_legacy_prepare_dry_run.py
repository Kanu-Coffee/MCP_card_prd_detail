#!/usr/bin/env python3
"""Emit a path-free, reproducible legacy bundle preparation report."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from cardrag.legacy import LegacyBundlePreparer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--full-write-block-reason",
        help="Optional operator-supplied reason why a full bundle write was not attempted.",
    )
    args = parser.parse_args()

    result = LegacyBundlePreparer(args.source).prepare(
        args.manifest,
        args.output,
        dry_run=True,
    )
    manifest = result.manifest
    report = {
        "schema_version": "cardrag.legacy-real-source-dry-run.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "passed",
        "source_writes": result.source_writes,
        "dry_run": result.dry_run,
        "bundle_id": manifest.bundle_id,
        "bundle_sha256": manifest.content_sha256,
        "source_manifest_sha256": manifest.source_manifest_sha256,
        "document_count": manifest.document_count,
        "adopted_count": manifest.adopted_count,
        "reocr_count": manifest.reocr_count,
        "unique_pdf_objects": manifest.unique_pdf_objects,
        "unique_ocr_objects": manifest.unique_ocr_objects,
        "unique_metadata_objects": manifest.unique_metadata_objects,
        "payload_bytes": manifest.payload_bytes,
        "full_write_performed": False,
    }
    if args.full_write_block_reason:
        report["full_write_block_reason"] = args.full_write_block_reason
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_name(f".{args.report.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
