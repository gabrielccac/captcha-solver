"""Compare file data, modes, ownership and links, excluding Docker mounts and apt metadata."""
import hashlib
import json
import os
import stat

EXCLUDE = ('/proc', '/sys', '/dev', '/tmp', '/var/lib/apt/lists', '/var/cache/apt',
           '/etc/hosts', '/etc/hostname', '/etc/resolv.conf', '/.dockerenv')
digest = hashlib.sha256()
count = 0
for directory, dirs, files in os.walk('/', topdown=True, followlinks=False):
    dirs[:] = sorted(d for d in dirs if os.path.join(directory, d) not in EXCLUDE)
    for name in sorted(dirs + files):
        path = os.path.join(directory, name)
        if path in EXCLUDE:
            continue
        info = os.lstat(path)
        item = [path, info.st_mode, info.st_uid, info.st_gid]
        if stat.S_ISLNK(info.st_mode):
            item.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            content = hashlib.sha256()
            with open(path, 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    content.update(block)
            item.append(content.hexdigest())
        digest.update(json.dumps(item, ensure_ascii=True).encode() + b'\n')
        count += 1
print(json.dumps(dict(sha256=digest.hexdigest(), entries=count)))
