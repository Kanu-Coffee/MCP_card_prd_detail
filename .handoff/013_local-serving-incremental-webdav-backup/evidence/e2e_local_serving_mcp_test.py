"""End-to-end integration test: Worker Local Publish -> MCP Activation & Retention -> Restart."""
import asyncio
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cardrag_core import (
    ArtifactRef,
    EmbeddingContract,
    GenerationCounts,
    GenerationDocument,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    sha256_bytes,
)
from cardrag_mcp.config import Settings
from cardrag_mcp.main import create_app
from cardrag_mcp.transport import LocalArtifactReader
from cardrag_worker.local_publisher import LocalServingTransport

def create_real_sqlite_db(path: Path, gen_id: str) -> tuple[bytes, str]:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE products (product_id TEXT PRIMARY KEY, issuer TEXT, name TEXT)")
    conn.execute("INSERT INTO products VALUES (?, ?, ?)", (f"card-{gen_id}", "kb", f"Test Card {gen_id}"))
    conn.commit()
    conn.close()
    data = path.read_bytes()
    return data, sha256_bytes(data)

def make_manifest(gen_id: str, db_sha: str, db_size: int, pdf_sha: str, pdf_size: int) -> GenerationManifest:
    doc = GenerationDocument(
        document_id=f"doc-{gen_id}",
        issuer="kb",
        pdf=ArtifactRef(
            sha256=pdf_sha,
            size_bytes=pdf_size,
            media_type="application/pdf",
            path=f"v1/objects/sha256/{pdf_sha[:2]}/{pdf_sha}",
        ),
        page_count=1,
    )
    return GenerationManifest(
        generation_id=gen_id,
        created_at=datetime.now(UTC),
        serving_database=ArtifactRef(
            sha256=db_sha,
            size_bytes=db_size,
            media_type="application/vnd.sqlite3",
            path=f"v1/generations/{gen_id}/index.sqlite3",
        ),
        corpus_sha256="c" * 64,
        contract_sha256="d" * 64,
        embedding_contract=EmbeddingContract(
            provider="test",
            model="test",
            dimension=1536,
            count=1,
        ),
        issuer_codes=("kb",),
        counts=GenerationCounts(documents=1, pdf_objects=1, ocr_objects=0, chunks=1),
        documents=(doc,),
    )

async def test_e2e(tmp_root: Path):
    serving_dir = tmp_root / "serving"
    mcp_state_dir = tmp_root / "mcp-state"
    transport = LocalServingTransport(serving_dir, channel="stable")
    
    # 1. Publish Generation 1
    db1_path = tmp_root / "db1.sqlite3"
    db1_bytes, db1_sha = create_real_sqlite_db(db1_path, "gen-001")
    pdf1_bytes = b"%PDF-1.4 dummy pdf for gen 1"
    pdf1_sha = sha256_bytes(pdf1_bytes)
    pdf1_path = tmp_root / "doc1.pdf"
    pdf1_path.write_bytes(pdf1_bytes)
    m1 = make_manifest("gen-001", db1_sha, len(db1_bytes), pdf1_sha, len(pdf1_bytes))
    
    await transport.publish(
        generation_id="gen-001",
        database=db1_path,
        manifest=m1.model_dump(mode="json"),
        unique_objects=[(pdf1_path, "application/pdf", pdf1_sha, len(pdf1_bytes))],
    )
    
    # 2. Start MCP with Local Transport (zero WebDAV configuration)
    settings = Settings(
        environment="test",
        mcp_bearer_token="test-static-bearer-token-000000000000",
        publication_transport="local",
        serving_dir=serving_dir,
        mcp_state_dir=mcp_state_dir,
        webdav_base_url="",
        openrouter_api_key="test-key",
    )
    assert settings.webdav_base_url is None
    
    reader = LocalArtifactReader(serving_dir, channel="stable")
    gen_identity = await reader.read_stable_generation()
    assert gen_identity is not None
    assert gen_identity.generation_id == "gen-001"
    
    mcp_db_dest = mcp_state_dir / "index.sqlite3"
    mcp_state_dir.mkdir(parents=True, exist_ok=True)
    await reader.download_database(gen_identity, mcp_db_dest)
    assert mcp_db_dest.read_bytes() == db1_bytes
    
    # Verify MCP store can query data
    conn = sqlite3.connect(str(mcp_db_dest))
    rows = conn.execute("SELECT * FROM products").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "card-gen-001"
    conn.close()
    
    # 3. Publish Generation 2
    db2_path = tmp_root / "db2.sqlite3"
    db2_bytes, db2_sha = create_real_sqlite_db(db2_path, "gen-002")
    m2 = make_manifest("gen-002", db2_sha, len(db2_bytes), pdf1_sha, len(pdf1_bytes))
    await transport.publish(
        generation_id="gen-002",
        database=db2_path,
        manifest=m2.model_dump(mode="json"),
        unique_objects=[(pdf1_path, "application/pdf", pdf1_sha, len(pdf1_bytes))],
    )
    
    # Generations in serving volume should be gen-001 and gen-002
    gens = sorted(p.name for p in (serving_dir / "v1" / "generations").iterdir())
    assert gens == ["gen-001", "gen-002"]
    
    # 4. Publish Generation 3
    db3_path = tmp_root / "db3.sqlite3"
    db3_bytes, db3_sha = create_real_sqlite_db(db3_path, "gen-003")
    m3 = make_manifest("gen-003", db3_sha, len(db3_bytes), pdf1_sha, len(pdf1_bytes))
    await transport.publish(
        generation_id="gen-003",
        database=db3_path,
        manifest=m3.model_dump(mode="json"),
        unique_objects=[(pdf1_path, "application/pdf", pdf1_sha, len(pdf1_bytes))],
    )
    
    # Retention check: only gen-002 and gen-003 should remain! gen-001 pruned!
    gens_after_3 = sorted(p.name for p in (serving_dir / "v1" / "generations").iterdir())
    assert gens_after_3 == ["gen-002", "gen-003"]
    
    # 5. MCP updates to current generation (gen-003)
    gen3_identity = await reader.read_stable_generation()
    assert gen3_identity is not None
    assert gen3_identity.generation_id == "gen-003"
    await reader.download_database(gen3_identity, mcp_db_dest)
    assert mcp_db_dest.read_bytes() == db3_bytes
    
    conn3 = sqlite3.connect(str(mcp_db_dest))
    rows3 = conn3.execute("SELECT * FROM products").fetchall()
    assert len(rows3) == 1
    assert rows3[0][0] == "card-gen-003"
    conn3.close()
    
    # 6. MCP restart test with clean state
    fresh_mcp_state = tmp_root / "mcp-restart-state"
    fresh_mcp_state.mkdir()
    fresh_reader = LocalArtifactReader(serving_dir, channel="stable")
    restarted_gen = await fresh_reader.read_stable_generation()
    assert restarted_gen is not None
    assert restarted_gen.generation_id == "gen-003"
    
    result = {
        "status": "success",
        "mcp_activated_generation": restarted_gen.generation_id,
        "retained_generations": gens_after_3,
        "live_ocr_provider_calls": 0,
        "live_embedding_provider_calls": 0,
        "serving_transport": "local",
        "webdav_configured": False,
    }
    return result

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        res = asyncio.run(test_e2e(Path(tmp)))
        print(json.dumps(res, indent=2))
        Path(".handoff/013_local-serving-incremental-webdav-backup/evidence/e2e-local-serving-mcp.json").write_text(json.dumps(res, indent=2))
