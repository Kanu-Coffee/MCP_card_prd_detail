import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path[:0] = ["/workspace/packages/cardrag-core/src", "/workspace/apps/cardrag-worker/src"]

from cardrag_core import NativeOCRContract
from cardrag_worker.ocr import OCRResolver, PriorLocalNativeSource
from cardrag_worker.state import WorkerState


class RejectingProvider:
    provider = "opencode"
    model = "alibaba-token-plan/qwen3.8-flash"
    reasoning_effort = "medium"

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("Forbidden: OCR provider called during offline verification")


async def main() -> None:
    root = Path("/state")
    run_id = "025ce35739944d8facfdda4c99961f0c"
    run_dir = root / "runs" / run_id
    seal = json.loads((run_dir / "sealed/publish.json").read_bytes())
    manifest = seal["manifest"]
    seed_ledger_path = next((root / "audit-reports/state-seed").glob("*.json"))
    seed_ledger = json.loads(seed_ledger_path.read_bytes())
    seed_doc_ids = {x["document_id"] for x in seed_ledger["ocr_documents"]}

    candidates = [d for d in manifest["documents"] if d["document_id"] not in seed_doc_ids]
    provider = RejectingProvider()
    rows = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        state = WorkerState(tmp / "state.sqlite3")
        resolver = OCRResolver(
            state=state,
            provider=provider,  # type: ignore[arg-type]
            webdav=None,
            chunk_pages=2,
        )
        contracts = [
            NativeOCRContract.model_validate_json(
                json.dumps(json.loads(f.read_bytes())["contract"])
            )
            for f in (run_dir / "documents").glob("*/ocr/native-manifest.json")
        ]
        resolver.set_compatible_contracts(contracts)

        for d in candidates:
            doc_id = d["document_id"]
            pdf_info = d["pdf"]
            ocr_info = d["ocr"]
            is_native = (run_dir / "documents" / doc_id / "ocr/native-manifest.json").is_file()

            # Pipeline supplies PriorLocalNativeSource with provider=None, model=None
            prior = PriorLocalNativeSource(
                runs_root=root / "runs",
                run_id=run_id,
                generation_id=manifest["generation_id"],
                corpus_sha256=manifest["corpus_sha256"],
                contract_sha256=manifest["contract_sha256"],
                document_id=doc_id,
                pdf_sha256=pdf_info["sha256"],
                pdf_size_bytes=pdf_info["size_bytes"],
                page_count=d["page_count"],
                ocr_sha256=ocr_info["sha256"],
                ocr_size_bytes=ocr_info["size_bytes"],
                cache_kind=d["ocr_cache_kind"],
                reuse_key=d["ocr_reuse_key"],
                variant_id=d["ocr_variant_id"],
                provider=None,
                model=None,
            )

            try:
                result = await resolver.resolve(
                    run_id="fix08-executor-verification",
                    document_id=doc_id,
                    pdf_path=tmp / "unused.pdf",
                    pdf_sha256=pdf_info["sha256"],
                    pdf_size_bytes=pdf_info["size_bytes"],
                    page_count=d["page_count"],
                    output_dir=tmp / "out" / doc_id,
                    prior_local_native=prior,
                )
                row = {
                    "document_id": doc_id,
                    "native": is_native,
                    "cache_hit": result.cache_reused,
                    "provider_called": result.provider_called,
                    "same_ocr": result.ocr_sha256 == ocr_info["sha256"],
                    "same_variant": result.cache_variant_id == d["ocr_variant_id"],
                    "cache_kind": result.cache_kind,
                    "provider": result.provider,
                    "model": result.model,
                }
                rows.append(row)
            except Exception as exc:
                rows.append({
                    "document_id": doc_id,
                    "native": is_native,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        state.close()

    summary = {
        "count": len(rows),
        "content_count": sum(not r.get("native") for r in rows),
        "native_count": sum(bool(r.get("native")) for r in rows),
        "calls": provider.calls,
        "hits": sum(bool(r.get("cache_hit")) for r in rows),
        "same_ocr": sum(bool(r.get("same_ocr")) for r in rows),
        "variant_matches": sum(bool(r.get("same_variant")) for r in rows),
        "exceptions": [r for r in rows if "error" in r],
        "native_results": [r for r in rows if r.get("native")],
        "content_variant_mismatches": [
            r for r in rows if not r.get("native") and not r.get("same_variant")
        ],
    }

    # Strict assertions matching FIX_08 criteria
    assert summary["count"] == 328, f"Expected 328 candidates, got {summary['count']}"
    assert summary["content_count"] == 323, f"Expected 323 content, got {summary['content_count']}"
    assert summary["native_count"] == 5, f"Expected 5 native, got {summary['native_count']}"
    assert summary["calls"] == 0, f"Expected 0 provider calls, got {summary['calls']}"
    assert summary["hits"] == 328, f"Expected 328 hits, got {summary['hits']}"
    assert summary["same_ocr"] == 328, f"Expected 328 matching OCR sha, got {summary['same_ocr']}"
    assert summary["variant_matches"] == 328, f"Expected 328 matching variants, got {summary['variant_matches']}"
    assert len(summary["exceptions"]) == 0, f"Unexpected exceptions: {summary['exceptions']}"

    for nr in summary["native_results"]:
        assert nr["cache_hit"] is True
        assert nr["provider_called"] is False
        assert nr["same_ocr"] is True
        assert nr["same_variant"] is True
        assert nr["provider"] == "opencode", f"Expected opencode, got {nr['provider']}"
        assert nr["model"] == "alibaba-token-plan/qwen3.8-flash", f"Expected qwen3.8-flash, got {nr['model']}"

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
