"""Download pinned official model snapshots into the isolated project directory."""
from pathlib import Path
import json
import urllib.request
from huggingface_hub import snapshot_download

root = Path(__file__).resolve().parents[1] / 'models'
root.mkdir(exist_ok=True)
repositories = [('jusiro2/FLAIR', '5f6bdd0a068353dc41a896ba3abdd7c0f6d35938', 'flair'),
                ('emilyalsentzer/Bio_ClinicalBERT', 'd5892b39a4adaed74b92212a44081509db72f87b', 'clinicalbert')]
manifest = {}
for repo, revision, name in repositories:
    if not revision:
        with urllib.request.urlopen('https://huggingface.co/api/models/' + repo, timeout=30) as r:
            revision = json.load(r)['sha']
    print('Downloading', repo, revision, flush=True)
    snapshot_download(repo_id=repo, revision=revision, local_dir=str(root / name),
                      allow_patterns=['config.json', 'model.safetensors', 'pytorch_model.bin', 'vocab.txt',
                                      'tokenizer*', 'special_tokens_map.json', 'README.md'], max_workers=2)
    manifest[name] = {'repo': repo, 'revision': revision}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print('Official model snapshots ready', flush=True)
