"""Archive source, local changes, uploads and a consistent database before upgrades."""
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
destination = root / '.runtime' / 'snapshots'
destination.mkdir(parents=True, exist_ok=True)
archive = destination / f'workspace-{stamp}.zip'
excluded = {'.git', '.runtime', '.venv', 'node_modules', '__pycache__', 'test-results', 'staticfiles', 'backups'}
files = [p for p in root.rglob('*') if p.is_file() and not any(s in excluded for s in p.relative_to(root).parts)
         and not p.name.startswith('db.sqlite3')]
before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
with tempfile.TemporaryDirectory() as temp:
    database = Path(temp) / 'db.sqlite3'
    source = sqlite3.connect(root / 'db.sqlite3')
    target = sqlite3.connect(database)
    source.backup(target)
    assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    source.close()
    target.close()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(root))
        bundle.write(database, 'db.sqlite3')
        bundle.writestr('snapshot-manifest.json', json.dumps({
            'created_at': stamp, 'files': before,
            'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=root, text=True),
            'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
            'database_sha256': hashlib.sha256(database.read_bytes()).hexdigest(),
        }, indent=2))
changed = [name for name, digest in before.items() if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
print(json.dumps({'archive': str(archive), 'files': len(files), 'changed_during_snapshot': changed}))
if changed:
    raise SystemExit('Files changed during backup. Inspect competing writers before continuing.')
