import os
import shlex
import shutil
import re
from pathlib import Path


class RsnapshotEngine:
    name = 'rsnapshot'
    label = 'rsnapshot · rsync + liens physiques'

    def capture(self, context, destination):
        config = context.configuration
        context.run(['/usr/bin/ssh', *context.ssh_options,
                     f"{config['login']}@{config['server']['host']}", 'command -v rsync >/dev/null'])
        root = context.work_dir / f"revision-{config['revision']}"
        root.mkdir(parents=True, exist_ok=True)
        snapshot_root = root / 'cache'
        snapshot_root.mkdir(exist_ok=True)
        # rsnapshot owns a two-version working cache. TARDIS publishes immutable
        # UUID trees and applies engine-independent daily/weekly/monthly retention.
        lines = [
            'config_version\t1.2', f'snapshot_root\t{snapshot_root}/',
            'cmd_rsync\t/usr/bin/rsync', 'cmd_ssh\t/usr/bin/ssh',
            'cmd_cp\t/usr/bin/cp', 'cmd_rm\t/usr/bin/rm',
            'retain\tdaily\t2', 'sync_first\t1',
            f'lockfile\t{root}/rsnapshot.pid',
            'rsync_short_args\t-a',
            'rsync_long_args\t--delete --numeric-ids --relative --protect-args',
            'ssh_args\t' + shlex.join(context.ssh_options),
            'verbose\t3', 'loglevel\t3',
        ]
        # Excludes use a separate file so neither commas nor option-like patterns
        # can be interpreted as rsnapshot backup-point options.
        excludes = root / 'excludes.txt'
        excludes.write_text('\n'.join(config['excludes']) + '\n', encoding='utf-8')
        if config['excludes']:
            lines[10] += ' --exclude-from=' + shlex.quote(str(excludes))
        host = config['server']['host']
        if ':' in host:
            host = f'[{host}]'
        for index, folder in enumerate(config['folders']):
            source = f"{config['login']}@{host}:{folder.rstrip('/')}/"
            lines.append(f'backup\t{source}\tfolder-{index}/')
        path = root / 'rsnapshot.conf'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        # An interrupted rsnapshot may leave a stale PID lock. The single worker
        # lock guarantees that no other rsnapshot process owns this cache now.
        (root / 'rsnapshot.pid').unlink(missing_ok=True)
        base = ['/usr/bin/rsnapshot', '-c', str(path)]
        context.run(base + ['configtest'])
        context.run(base + ['sync'])
        # A non-zero sync (including rsync partial transfer) never reaches rotation.
        context.run(base + ['daily'])
        complete = snapshot_root / 'daily.0'
        if not all((complete / f'folder-{i}').is_dir() for i in range(len(config['folders']))):
            raise RuntimeError('La collecte ne contient pas tous les dossiers attendus.')
        def link_file(source, target):
            context.checkpoint()
            return os.link(source, target)
        def check_directory(directory, entries):
            context.checkpoint()
            return []
        shutil.copytree(complete, destination, copy_function=link_file, symlinks=True, ignore=check_directory)

    def cleanup(self, work_dir, keep_revisions):
        from ..storage import remove_tree
        if not work_dir.exists():
            return 0
        count = 0
        for directory in work_dir.iterdir():
            match = re.fullmatch(r'revision-(\d+)',directory.name)
            if match and int(match[1]) not in keep_revisions:
                remove_tree(directory); count += 1
        return count
