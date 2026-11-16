import os
import shutil
import stat


def measure_storage(root):
    """Allocated blocks counted once per (device,inode), across cache and versions."""
    seen, allocated, logical, files = set(), 0, 0, 0
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        identity = (info.st_dev, info.st_ino)
        if identity not in seen:
            seen.add(identity)
            allocated += getattr(info, 'st_blocks', 0) * 512
        if stat.S_ISREG(info.st_mode):
            logical += info.st_size; files += 1
        elif stat.S_ISDIR(info.st_mode):
            try:
                pending.extend(path.iterdir())
            except FileNotFoundError:
                pass
    usage = shutil.disk_usage(root)
    return {'allocated_bytes': allocated, 'logical_bytes': logical, 'file_entries': files,
            'unique_inodes': len(seen), 'filesystem_total_bytes': usage.total,
            'filesystem_used_bytes': usage.used, 'filesystem_free_bytes': usage.free,
            'filesystem_used_percent': round(usage.used / usage.total * 100, 1)}


def remove_tree(path):
    # Only directory permissions are adjusted: chmod on a hard-linked FILE would
    # also modify another retained version. Directory trees are never hard-linked.
    if path.is_symlink():
        raise ValueError('Refus de supprimer un cache qui est un lien symbolique.')
    for current, directories, files in os.walk(path, followlinks=False):
        os.chmod(current, os.stat(current).st_mode | stat.S_IRWXU)
    shutil.rmtree(path)
