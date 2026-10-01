import asyncio, hashlib, json, os, sys
from pathlib import Path
from cardrag_core.ocr import NativeOCRContract, OCRInput, native_ocr_reuse_key
from cardrag_core.paths import ocr_ready_path, ocr_manifest_path
from cardrag_worker.webdav import WebDAVClient
import sqlite3

STATE = Path('/var/lib/cardrag-worker')
RUN = 'c622d3c4b1fb4df5a74a4b139591028f'
docs = STATE/'runs'/RUN/'documents'

targets = {}
missing_structure = []
for d in sorted(os.listdir(docs)):
    ocr_dir = docs/d/'ocr'
    om = ocr_dir/'ocr.md'
    if not om.is_file():
        continue
    if (ocr_dir/'native-manifest.json').is_file():
        continue
    sp = docs/d/'structure'/'structure.v2.json'
    if not sp.is_file():
        missing_structure.append(d)
        continue
    s = json.loads(sp.read_bytes())
    targets[d] = (s['pdf_sha256'], int(s.get('page_count') or len(s.get('pages') or [])))
print('targets(no-manifest with ocr.md+structure):', len(targets), '| missing structure:', len(missing_structure))

db = sqlite3.connect(f'file:{STATE}/worker-state.sqlite3?mode=ro&immutable=1', isolation_level=None)
sizes = {r[0]: r[1] for r in db.execute('select pdf_sha256, size_bytes from pdf_cache_object')}

contract_jsons = {}
cur_contract_json = None
for mp in Path(STATE/'runs').glob('*/documents/*/ocr/native-manifest.json'):
    try:
        m = json.loads(mp.read_bytes())
    except Exception:
        continue
    c = m.get('contract')
    if not isinstance(c, dict):
        continue
    if m.get('created_at','') >= '2026-10-01' and c.get('model') == 'qwen3.8-flash':
        cur_contract_json = c
    contract_jsons[json.dumps(c, sort_keys=True)] = c
if cur_contract_json is None:
    sys.exit('current contract not found')
cur = NativeOCRContract.model_validate(cur_contract_json)
contracts = {cur.contract_sha256: cur}
for cj in contract_jsons.values():
    try:
        c = NativeOCRContract.model_validate(cj)
    except Exception:
        continue
    contracts[c.contract_sha256] = c
env_models = [x.strip() for x in os.environ.get('CARDRAG_OCR_COMPATIBLE_MODELS','').split(',') if x.strip()]
for m in env_models:
    if m == cur.model:
        continue
    effort = 'high' if ('sol' in m or '5.4' in m) else cur.reasoning_effort
    synth = cur.model_copy(update={'model': m, 'reasoning_effort': effort})
    if synth.contract_sha256 != cur.contract_sha256:
        contracts[synth.contract_sha256] = synth
print('contracts in scope:', [(c.model, c.provider, c.segmentation_strategy_id.split('.')[-1]) for c in contracts.values()])
probe = [c for h,c in contracts.items() if h != cur.contract_sha256]

client = WebDAVClient.from_env()
sem = asyncio.Semaphore(12)
mismatches = []
hits = 0

async def check(doc, key, contract, ready_p, man_p):
    global hits
    async with sem:
        try:
            ready = await client.get_bytes(ready_p, max_bytes=65536)
            if ready is None:
                return
            manifest = await client.get_bytes(man_p, max_bytes=2_000_000)
        except Exception as exc:
            print('ERR', doc, key[:10], type(exc).__name__, str(exc)[:80])
            return
    if manifest is None:
        return
    hits += 1
    try:
        m = json.loads(manifest)
        remote_sha = m['output']['sha256']
    except Exception:
        remote_sha = 'PARSE-FAIL'
    local_sha = targets_sha[doc]
    if remote_sha != local_sha:
        mismatches.append({'doc': doc, 'key': key, 'model': contract.model, 'provider': contract.provider,
                           'remote_sha': remote_sha, 'local_sha': local_sha})

targets_sha = {}
tasks = []
for doc, (pdf_sha, pages) in targets.items():
    size = sizes.get(pdf_sha)
    if size is None:
        missing_structure.append(doc+'|nosize')
        continue
    body = (docs/doc/'ocr'/'ocr.md').read_bytes()
    targets_sha[doc] = hashlib.sha256(body).hexdigest()
    src = OCRInput(pdf_sha256=pdf_sha, pdf_size_bytes=size, page_count=pages)
    for contract in [cur, *probe]:
        k = native_ocr_reuse_key(contract, src)
        tasks.append(check(doc, k, contract, ocr_ready_path(k), ocr_manifest_path(k)))

async def main():
    await asyncio.gather(*tasks)
    await client.core.aclose() if hasattr(client.core,'aclose') else None

asyncio.run(main())
print('remote ready-hits:', hits, 'MISMATCHES:', len(mismatches))
Path('/work/scan-report.json').write_text(json.dumps({'mismatches': mismatches, 'missing': missing_structure}, ensure_ascii=False, indent=1))
for m in mismatches[:30]:
    print(m['doc'][4:16], m['model'][:18], m['remote_sha'][:12], 'vs', m['local_sha'][:12])
