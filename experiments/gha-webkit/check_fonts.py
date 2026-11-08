"""Restore and verify the exact font files used by the local browser experiment."""
import argparse
import hashlib
import json
from pathlib import Path

EXPECTED = '20feac6451a727f0a0deb823f474a848f6b28cbec01882d82d00b225fb6c1891'


def verify(remove_extra=False):
    root = Path('/usr/share/fonts')
    manifest = json.loads(Path(__file__).with_name('fonts.json').read_text())
    digest = hashlib.sha256()
    seen = set()
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        name = str(path.relative_to(root))
        if name not in manifest:
            if remove_extra:
                path.unlink()
                continue
            raise RuntimeError('unexpected_font')
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == manifest[name], 'font_content_mismatch'
        seen.add(name)
        digest.update(name.encode())
        digest.update(body)
    assert seen == set(manifest), 'missing_font'
    assert digest.hexdigest() == EXPECTED, 'font_set_mismatch'
    return digest.hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--remove-extra', action='store_true')
    print(verify(parser.parse_args().remove_extra))
