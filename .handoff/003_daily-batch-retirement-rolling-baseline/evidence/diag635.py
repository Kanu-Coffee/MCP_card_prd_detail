import hashlib, json, os, sqlite3
from pathlib import Path
from cardrag_core.ocr import NativeOCRContract, OCRInput, native_ocr_reuse_key
from cardrag_core import OCRArtifactManifest
STATE = Path('/var/lib/cardrag-worker')
RUN = 'c622d3c4b1fb4df5a74a4b139591028f'
docs = STATE/'runs'/RUN/'documents'
runs_root = STATE/'runs'
db = sqlite3.connect(f'file:{STATE}/worker-state.sqlite3?mode=ro&immutable=1', isolation_level=None)
sizes = {r[0]: r[1] for r in db.execute('select pdf_sha256, size_bytes from pdf_cache_object')}
# prior manifest index (5f74 + other runs, exclude RUN)
c_docs_by_sha = {}
sealed_keys = {}  # (path-resolve-agnostic: record doc, sha)
for rid in os.listdir(runs_root):
    if rid == RUN:
        continue
    droot = runs_root/rid/'documents'
    if not droot.is_dir():
        continue
    for n in os.listdir(droot):
        mp = droot/n/'ocr'/'native-manifest.json'
        om = droot/n/'ocr'/'ocr.md'
        if mp.is_file() and om.is_file():
            try:
                m = OCRArtifactManifest.model_validate_json(mp.read_bytes())
            except Exception:
                continue
            body = om.read_bytes()
            if hashlib.sha256(body).hexdigest() != m.output.sha256:
                continue
            key = (m.source.pdf_sha256, m.source.pdf_size_bytes, m.source.page_count)
            c_docs_by_sha.setdefault(key, []).append((rid, n, m))
targets = []
for d in sorted(os.listdir(docs)):
    od = docs/d/'ocr'
    om = od/'ocr.md'
    if not om.is_file() or (od/'native-manifest.json').is_file():
        continue
    sp = docs/d/'structure'/'structure.v2.json'
    s = json.loads(sp.read_bytes())
    targets.append((d, s['pdf_sha256'], sizes.get(s['pdf_sha256']), int(s.get('page_count') or len(s.get('pages',[])))))
poison = []
for d, pdf_sha, size, pages in targets:
    local = (docs/d/'ocr'/'ocr.md').read_bytes()
    local_sha = hashlib.sha256(local).hexdigest()
    for rid, prior_n, m in c_docs_by_sha.get((pdf_sha, size, pages), []):
        if prior_n == d:
            continue
        # fallback binding partner found
        same_doc_id = prior_n == d
        if m.output.sha256 != local_sha:
            poison.append({'doc': d, 'partner': prior_n, 'partner_run': rid, 'partner_sha': m.output.sha256[:16],
                           'local_sha': local_sha[:16], 'partner_model': m.contract.model})
        else:
            poison.append({'doc': d, 'note': 'partner matches local (OK)', 'partner': prior_n})
strict_mismatch = [p for p in poison if 'partner_sha' in p]
print('targets:', len(targets), '| fallback-partner sha-mismatch (crash candidates):', len(strict_mismatch))
for p in strict_mismatch:
    print(p['doc'][4:16], 'partner', p['partner'][4:16], 'run', p['partner_run'][:8], p['partner_model'][:16], p['partner_sha'], 'vs', p['local_sha'])
Path('/p/audit-reports/repair-diag635.json').write_text(json.dumps(poison, ensure_ascii=False, indent=1))
