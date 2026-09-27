"""Create a consistent, restorable snapshot: the SQLite database plus every uploaded file, in one zip."""
import json
import sqlite3
from contextlib import closing
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


class Command(BaseCommand):
    help = 'Write altab-backup-<timestamp>.zip containing the live database and the media folder. Safe to run while the server is up.'

    def add_arguments(self, parser):
        parser.add_argument('--destination', default='backups', help='Folder for the archive (created if missing).')
        parser.add_argument('--keep', type=int, default=0, help='Delete older archives in the folder beyond this many (0 keeps all).')

    def handle(self, *args, **options):
        if connection.vendor != 'sqlite':
            raise CommandError('backup_store snapshots SQLite databases. For PostgreSQL use pg_dump plus a copy of MEDIA_ROOT.')
        destination = Path(options['destination'])
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        archive = destination / f'altab-backup-{stamp}.zip'
        media_root = Path(settings.MEDIA_ROOT)
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / 'db.sqlite3'
            # The online backup API copies a transactionally consistent image even while writers are active.
            connection.ensure_connection()
            with closing(sqlite3.connect(snapshot)) as target:
                connection.connection.backup(target)
            with closing(sqlite3.connect(snapshot)) as check:
                integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
                tables = {row[0]: check.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'store_%' ORDER BY name")}
            if integrity != 'ok':
                raise CommandError(f'Database integrity check failed: {integrity}')
            migrations = sorted(f'{app}.{name}' for app, name in MigrationRecorder(connection).applied_migrations())
            manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'django': django.get_version(), 'database': str(settings.DATABASES['default']['NAME']), 'migrations': migrations, 'tables': tables, 'media_files': 0}
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(snapshot, 'db.sqlite3')
                if media_root.exists():
                    for path in sorted(p for p in media_root.rglob('*') if p.is_file()):
                        bundle.write(path, f'media/{path.relative_to(media_root).as_posix()}')
                        manifest['media_files'] += 1
                bundle.writestr('manifest.json', json.dumps(manifest, indent=2))
        if options['keep'] > 0:
            for old in sorted(destination.glob('altab-backup-*.zip'))[:-options['keep']]:
                old.unlink()
        self.stdout.write(self.style.SUCCESS(f'Backup written: {archive} ({sum(tables.values())} store rows, {manifest["media_files"]} media files)'))
