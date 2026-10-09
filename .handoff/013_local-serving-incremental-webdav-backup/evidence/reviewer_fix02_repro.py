"""Offline review using the real WebDAV facade and local updater, not production."""
import asyncio
import json
import runpy
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cardrag_core import ArtifactRef, EmbeddingContract, GenerationCounts, GenerationDocument, GenerationManifest, sha256_bytes
from cardrag_mcp.observability import Metrics
from cardrag_mcp.store import GenerationStore
from cardrag_mcp.transport import LocalArtifactReader
from cardrag_mcp.updater import WebDAVUpdater
from cardrag_worker.backup import BackupLedger
from cardrag_worker.local_publisher import LocalServingTransport
from cardrag_worker.webdav import WebDAVClient

class Core:
    def __init__(self):
        self.storage = {}
        self.body_reads = 0
    def exists(self, path): return str(path) in self.storage
    def ensure_collection(self, path): pass
    def put(self, path, content, **kw): self.storage[str(path)] = content
    def verify(self, path, *, expected_sha256, expected_size_bytes):
        self.body_reads += 1
        content = self.storage[str(path)]
        if len(content) != expected_size_bytes or sha256_bytes(content) != expected_sha256:
            raise RuntimeError('immutable destination content does not match requested SHA')
    def move(self, source, dest, **kw): self.storage[str(dest)] = self.storage.pop(str(source))
    def delete(self, path, **kw): self.storage.pop(str(path), None)
    def get(self, path, **kw):
        self.body_reads += 1
        return SimpleNamespace(content=self.storage[str(path)])

async def main(root):
    out = {}
    state = root/'worker'
    state.mkdir()
    s = SimpleNamespace(backup_mode='immediate', backup_every_runs=7, backup_new_ocr_count=30,
        backup_new_bytes=1024**3, backup_max_pending_age_hours=168, state_dir=state,
        webdav_base_url='https://backup.invalid/')
    ledger = BackupLedger(state/'backup-ledger.sqlite3')
    core = Core()
    client = WebDAVClient(core)
    async def batch(label):
        source = state/f'{label}.md'
        source.write_bytes(label.encode())
        remote = f'v1/objects/sha256/{sha256_bytes(label.encode())[:2]}/{sha256_bytes(label.encode())}'
        with ledger._get_connection() as c:
            c.execute('INSERT INTO backup_pending VALUES(?,?,?,?,?,?,?,?,?)',
                (label, 'ocr_cas', sha256_bytes(label.encode()), len(label), str(source), remote, 'text/markdown', time.time(), 'pending'))
        return await ledger.flush(s, client, force=True)
    out['first_backup'] = await batch('first')
    out['first_remote_index_items'] = len(json.loads(core.storage['v1/backup/index.json'])['items'])
    out['second_backup'] = await batch('second')
    out['second_remote_index_items'] = len(json.loads(core.storage['v1/backup/index.json'])['items'])
    fresh = BackupLedger(root/'fresh'/'backup-ledger.sqlite3')
    out['fresh_restore_after_second_backup'] = await fresh.restore(s, root/'restore', client)
    lost = state/'missing.md'
    with ledger._get_connection() as c:
        c.execute('INSERT INTO backup_pending VALUES(?,?,?,?,?,?,?,?,?)',
            ('missing', 'ocr_cas', 'a'*64, 5, str(lost), 'v1/objects/missing', 'text/markdown', time.time(), 'pending'))
    out['missing_source_flush'] = await ledger.flush(s, client, force=True)
    out['missing_source_status'] = ledger.get_status(s)
    # Cross-root receipts must not suppress the same source at a new destination.
    s.webdav_base_url = 'https://different-backup.invalid/'
    run = state/'runs'/'new-root-run'/'sealed'
    run.mkdir(parents=True)
    first = state/'first.md'
    (run/'publish.json').write_text(json.dumps({'objects':[{'path':str(first),'sha256':sha256_bytes(first.read_bytes()),'size_bytes':len(first.read_bytes()),'media_type':'text/markdown'}]}))
    ledger.record_run_success('new-root-run', state, s)
    with ledger._get_connection() as c:
        out['new_remote_root_enqueued_objects'] = c.execute("SELECT COUNT(*) FROM backup_pending WHERE item_id != 'missing'").fetchone()[0]
    # Actual activation with the repository's valid legacy serving DB fixture.
    fixtures = runpy.run_path('apps/cardrag-mcp/tests/conftest.py')
    fixture = fixtures['create_database'](root/'source.sqlite3', 'gen-valid', two_documents=False)
    docs = []
    objs = []
    for (docid, sha, size, pdf), (_, issuer, pages) in zip(fixture.documents, fixture.document_contracts):
        ref = ArtifactRef.for_cas(sha256=sha, size_bytes=size, media_type='application/pdf')
        docs.append(GenerationDocument(document_id=docid, issuer=issuer, pdf=ref, page_count=pages))
        f = root/f'{sha}.pdf'
        f.write_bytes(pdf)
        objs.append((f,'application/pdf',sha,size))
    db = fixture.database.read_bytes()
    m = GenerationManifest(generation_id=fixture.generation_id, created_at=datetime.now(UTC),
        serving_database=ArtifactRef(path=f'v1/generations/{fixture.generation_id}/index.sqlite3',sha256=sha256_bytes(db),size_bytes=len(db),media_type='application/vnd.sqlite3'),
        corpus_sha256=fixture.corpus_sha256, contract_sha256=fixture.contract_sha256,
        embedding_contract=EmbeddingContract(provider='openrouter',model='openai/text-embedding-3-small',dimension=1536,count=2),
        issuer_codes=fixture.issuer_codes, counts=GenerationCounts(documents=1,pdf_objects=1,ocr_objects=0,chunks=2),documents=tuple(docs))
    t = LocalServingTransport(root/'serving')
    await t.publish(generation_id=m.generation_id,database=fixture.database,manifest=m.model_dump(mode='json'),unique_objects=objs)
    store = GenerationStore(root/'mcp-state',maximum_vector_bytes=1024*1024)
    updater = WebDAVUpdater(LocalArtifactReader(root/'serving'),store,Metrics.create())
    try:
        out['actual_updater_activated'] = await updater.poll_once()
        out['actual_active_generation'] = store.active_generation_id
    except Exception as exc:
        out['actual_updater_error'] = str(exc)
    # Restore a real native OCR bundle, then require an actual resolver cache hit.
    sys.path.insert(0, 'apps/cardrag-worker/tests')
    ocr_helpers = runpy.run_path('apps/cardrag-worker/tests/test_ocr.py')
    native_state = root/'native-old'
    native_state.mkdir()
    provider = ocr_helpers['FakeProvider']()
    resolver, old_state = ocr_helpers['make_resolver'](native_state, provider, None, cache_mode='read-only')
    docdir = native_state/'runs'/'native-run'/'documents'/'doc1'/'ocr'
    ocr_helpers['write_document_local_native'](resolver, docdir)
    old_state.close()
    native_settings = SimpleNamespace(**vars(s))
    native_settings.state_dir = native_state
    native_settings.webdav_base_url = 'https://native-backup.invalid/'
    native_ledger = BackupLedger(native_state/'backup-ledger.sqlite3')
    native_ledger.record_run_success('native-run', native_state, native_settings)
    native_client = WebDAVClient(Core())
    out['native_backup'] = await native_ledger.flush(native_settings, native_client, force=True)
    restored_state = root/'native-new'
    restored_ledger = BackupLedger(restored_state/'backup-ledger.sqlite3')
    out['native_restore'] = await restored_ledger.restore(native_settings, restored_state, native_client)
    new_provider = ocr_helpers['FakeProvider']()
    new_resolver, new_state = ocr_helpers['make_resolver'](restored_state, new_provider, None, cache_mode='read-only')
    new_resolver._require_cache_hit = True
    try:
        runid = new_state.start_run(run_id='restored-run')
        result = await new_resolver.resolve(run_id=runid, document_id='doc1', pdf_path=root/'pdf',
            pdf_sha256=ocr_helpers['PDF_SHA'], pdf_size_bytes=3, page_count=1,
            output_dir=restored_state/'runs'/runid/'documents'/'doc1'/'ocr')
        out['restored_native_resolver_result'] = 'cache hit'
    except Exception as exc:
        out['restored_native_resolver_result'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        new_state.close()
    out['native_validation_live_provider_calls'] = len(new_provider.calls)
    print(json.dumps(out, indent=2))

with tempfile.TemporaryDirectory(prefix='cardrag-review-fix02-') as tmp:
    asyncio.run(main(Path(tmp)))
