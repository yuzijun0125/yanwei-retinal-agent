"""Apply the verified patch only to the exact inspected runtime revision."""
from pathlib import Path
import hashlib
import shutil

target = Path('/opt/backend/.venv/lib/python3.11/site-packages/nexent/core/agents/core_agent.py')
folder = Path(__file__).resolve().parent
before = (folder / 'core_agent.runtime.before.py').read_text(encoding='utf-8')
after = (folder / 'core_agent.runtime.after.py').read_text(encoding='utf-8')
current = target.read_text(encoding='utf-8')
if current == after:
    print('already applied')
elif hashlib.sha256(current.encode()).hexdigest() in ('5c21b4ac70e5db68c066ea47348ba0a003fb2928febcc4981d136794d64a0484', 'a7fe698a9da9540b3018c0041a086010d86562c97b28eed22c2239ec3a4d48eb'):
    backup = target.with_suffix('.py.yanwei-dsml-backup')
    assert backup.read_text(encoding='utf-8') == before
    compile(after, str(target), 'exec')
    target.write_text(after, encoding='utf-8')
    print('updated own compatibility patch', hashlib.sha256(after.encode()).hexdigest())
elif current != before:
    raise SystemExit('Runtime revision changed; refusing to overwrite unrelated changes')
else:
    compile(after, str(target), 'exec')
    backup = target.with_suffix('.py.yanwei-dsml-backup')
    if backup.exists():
        raise SystemExit('Backup already exists; inspect before applying')
    shutil.copy2(target, backup)
    target.write_text(after, encoding='utf-8')
    print('applied', hashlib.sha256(after.encode()).hexdigest())
