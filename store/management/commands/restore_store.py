"""Restore a backup_store archive. Stop the web server and the reservation worker first."""
import json
import shutil
import sqlite3
from contextlib import closing
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Replace the SQLite database and media folder with the contents of a backup archive. Requires --yes.'

    def add_arguments(self, parser):
        parser.add_argument('archive')
        parser.add_argument('--target', default=None, help='Database file to replace (default: the configured SQLite path).')
        parser.add_argument('--media-root', default=None, help='Media folder to replace (default: MEDIA_ROOT).')
        parser.add_argument('--yes', action='store_true', help='Confirm that existing data will be replaced.')

    def handle(self, *args, **options):
        target = Path(options['target'] or settings.DATABASES['default']['NAME'])
        media_root = Path(options['media_root'] or settings.MEDIA_ROOT)
        if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3' and not options['target']:
            raise CommandError('restore_store restores SQLite files. For PostgreSQL use pg_restore and copy the media folder.')
        if str(target).startswith('file:') or str(target) == ':memory:':
            raise CommandError('The configured database is in memory; pass --target with a file path.')
        archive = Path(options['archive'])
        if not archive.is_file():
            raise CommandError(f'Archive not found: {archive}')
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            if 'db.sqlite3' not in names or 'manifest.json' not in names:
                raise CommandError('This is not a backup_store archive (db.sqlite3 or manifest.json missing).')
            manifest = json.loads(bundle.read('manifest.json'))
            self.stdout.write(f'Archive created {manifest.get("created_at")} | migrations: {len(manifest.get("migrations", []))} | media files: {manifest.get("media_files", 0)}')
            if not options['yes']:
                self.stderr.write(self.style.ERROR(f'This will replace {target} and {media_root}. Re-run with --yes to continue.'))
                sys.exit(2)
            with tempfile.TemporaryDirectory() as folder:
                staged = Path(folder) / 'db.sqlite3'
                staged.write_bytes(bundle.read('db.sqlite3'))
                with closing(sqlite3.connect(staged)) as check:
                    integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
                    has_migrations = check.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='django_migrations'").fetchone()[0]
                if integrity != 'ok' or not has_migrations:
                    raise CommandError('The archived database failed validation; nothing was changed.')
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if target.exists():
                        target.rename(target.with_name(f'{target.name}.before-restore-{stamp}'))
                    for sidecar in [target.with_name(target.name + '-wal'), target.with_name(target.name + '-shm')]:
                        if sidecar.exists(): sidecar.unlink()
                    shutil.copyfile(staged, target)
                except PermissionError as exc:
                    raise CommandError(f'The database is in use ({exc}). Stop the server and worker, then retry.')
                if media_root.exists():
                    shutil.move(str(media_root), str(media_root.with_name(f'{media_root.name}.before-restore-{stamp}')))
                media_root.mkdir(parents=True, exist_ok=True)
                for name in sorted(names):
                    if name.startswith('media/') and not name.endswith('/'):
                        relative = Path(name[len('media/'):])
                        if relative.is_absolute() or '..' in relative.parts:
                            raise CommandError(f'Refusing unsafe path in archive: {name}')
                        out = media_root / relative
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(bundle.read(name))
        self.stdout.write(self.style.SUCCESS(f'Restored {archive.name} -> {target}. Previous data kept beside it with a .before-restore-{stamp} suffix. Run "manage.py migrate" before starting the server.'))
