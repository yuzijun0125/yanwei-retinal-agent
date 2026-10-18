"""Encode the real case images once, with exact hashes and pinned model provenance."""
from pathlib import Path
import hashlib
import json
import os
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from yanwei import mcp_tools as m, multimodal as mm

rt = m.Runtime()
folder = mm.index_folder()
folder.mkdir(parents=True, exist_ok=True)
t0 = time.perf_counter()
enc = mm.encoder()
ids, vectors, records, failures = [], [], [], []
for image_id, record in rt.ds.images.items():
    try:
        path = mm.resolve_image(rt.data_dir, record)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        vectors.append(enc.image(str(path)))
        ids.append(image_id)
        records.append({'image_id': image_id, 'sha256': digest,
                        'metadata_hash_matches': digest == record.get('sha256')})
        print(f'{len(ids)}/{len(rt.ds.images)} {image_id}', flush=True)
    except Exception as exc:
        failures.append({'image_id': image_id, 'error': type(exc).__name__})
if not ids:
    raise SystemExit('No real images encoded; index not published')
temporary = folder / 'index.tmp'
with temporary.open('wb') as f:
    np.savez_compressed(f, image_ids=np.array(ids), vectors=np.stack(vectors))
os.replace(temporary, folder / 'index.npz')
manifest = {'model': 'jusiro2/FLAIR', 'model_revision': mm.settings()['model_revision'],
            'image_count': len(ids), 'dimensions': int(vectors[0].size), 'records': records,
            'failures': failures, 'embedding_scope': 'whole_image', 'clinical_validity': 'UNVALIDATED',
            'wall_seconds': round(time.perf_counter() - t0, 2)}
(folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k:v for k,v in manifest.items() if k != 'records'}, ensure_ascii=False), flush=True)
