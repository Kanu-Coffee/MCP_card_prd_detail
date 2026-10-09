"""Validation of all 8 Reviewer Fix 02 edge cases."""
import asyncio
import json
import runpy
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from cardrag_core import sha256_bytes
from cardrag_mcp.config import Settings
from cardrag_mcp.main import create_app
from cardrag_mcp.transport import LocalArtifactReader
from cardrag_worker.backup import BackupLedger
from cardrag_worker.local_publisher import LocalServingTransport

fixture = runpy.run_path('apps/cardrag-worker/tests/test_local_serving_and_backup.py')['create_sample_generation']

class Client:
    def __init__(self, delay=0):
        self.storage = {}
        self.puts = 0
        self.delay = delay
    async def put(self, path, body):
        await asyncio.sleep(self.delay)
        self.puts += 1
        self.storage[path] = body
    async def get(self, path):
        await asyncio.sleep(self.delay)
        return self.storage[path]
    async def close(self):
        pass

def queue(ledger, path, remote):
    b = path.read_bytes()
    with ledger._get_connection() as c:
        c.execute('INSERT INTO backup_pending VALUES(?,?,?,?,?,?,?,?,?)',
                  (remote, 'ocr_cas', sha256_bytes(b), len(b), str(path), remote, 'text/markdown', time.time(), 'pending'))

async def main(root):
    s = SimpleNamespace(backup_mode='hybrid', backup_every_runs=7, backup_new_ocr_count=30,
                        backup_new_bytes=1024**3, backup_max_pending_age_hours=168)
    state = root/'state'
    state.mkdir()
    l = BackupLedger(state/'backup-ledger.sqlite3')
    l.record_run_success('same-run', state, s)
    l.record_run_success('same-run', state, s)
    output = {'same_run_counted_twice': l.get_status(s)['runs_since_last_backup']}
    source = state/'ocr.md'
    source.write_bytes(b'OCR result')
    queue(l, source, 'v1/objects/test')
    client = Client()
    backed = await l.flush(s, client, force=True)
    fresh = BackupLedger(root/'fresh-host'/'backup-ledger.sqlite3')
    output['old_host_backup'] = backed['status']
    output['fresh_host_restore'] = await fresh.restore(s, root/'fresh-restored', client)
    missing = state/'vanishing.md'
    missing.write_bytes(b'expensive unbacked OCR')
    queue(l, missing, 'v1/objects/missing')
    missing.unlink()
    output['missing_pending_source'] = await l.flush(s, client, force=True)
    source.write_bytes(b'slow OCR result')
    queue(l, source, 'v1/objects/slow')
    slow = Client(delay=0.06)
    start = time.monotonic()
    output['budget_result'] = await l.flush(s, slow, force=True, timeout_seconds=0.01)
    output['budget_elapsed_seconds'] = round(time.monotonic()-start, 3)
    output['budget_requested_seconds'] = 0.01
    source.write_bytes(b'below threshold')
    queue(l, source, 'v1/objects/below')
    s.backup_every_runs = 999
    output['hybrid_trigger_before_cli_style_flush'] = l.get_status(s)['should_trigger']
    output['unforced_flush'] = await l.flush(s, client, force=False)
    for gen in ('gen-one', 'gen-two', 'gen-three'):
        m, _, _, db, pdf = fixture(gen)
        dbfile = root/'index.sqlite3'
        pdffile = root/'test.pdf'
        dbfile.write_bytes(db)
        pdffile.write_bytes(pdf)
        t = LocalServingTransport(root/'serving')
        await t.publish(generation_id=gen, database=dbfile, manifest=m.model_dump(mode='json'),
                        unique_objects=[(pdffile,'application/pdf',m.documents[0].pdf.sha256,len(pdf))])
    output['serving_generations_after_three_publications'] = sorted(p.name for p in (root/'serving'/'v1'/'generations').iterdir())
    m, _, _, db, pdf = fixture('gen-bad')
    dbfile.write_bytes(b'wrong database content')
    pdffile.write_bytes(pdf)
    try:
        await t.publish(generation_id='gen-bad', database=dbfile, manifest=m.model_dump(mode='json'),
                        unique_objects=[(pdffile,'application/pdf',m.documents[0].pdf.sha256,len(pdf))])
        output['publisher_accepted_mismatched_database'] = True
    except Exception as exc:
        output['publisher_accepted_mismatched_database'] = False
        output['publisher_rejected_mismatched_database'] = str(exc)
    try:
        Settings(environment='test', mcp_bearer_token='test-static-bearer-token-000000000000', webdav_base_url='')
        output['compose_empty_webdav_url_rejected'] = False
    except Exception as exc:
        output['compose_empty_webdav_url_rejected'] = type(exc).__name__
    return output

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='cardrag-review-013-') as tmp:
        res = asyncio.run(main(Path(tmp)))
        print(json.dumps(res, indent=2))
        evidence_file = Path('.handoff/013_local-serving-incremental-webdav-backup/evidence/fix02-repro-verification.json')
        evidence_file.write_text(json.dumps(res, indent=2))
