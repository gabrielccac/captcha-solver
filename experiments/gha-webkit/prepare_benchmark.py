"""Repackage one immutable runtime; never install/upgrade browser dependencies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
PACKAGE = 'ghcr.io/gabrielccac/captcha-solver-webkit-smoke'
BASE = PACKAGE + '@sha256:9baa1d1d88618dc3d862b03250ca21498ee9e92f3b361d28439680db1149a16c'
REPACK_URL = 'https://github.com/orf/docker-repack/releases/download/v0.5.0/docker-repack-Linux-x86_64.tar.gz'
REPACK_SHA = '98cb58ff36354b1ddc525df0ee09a392bf1408842678b9cf1cc843a018ccd780'
CONFIG_KEYS = ('Env', 'User', 'WorkingDir', 'Entrypoint', 'Cmd', 'Volumes', 'ExposedPorts', 'Healthcheck', 'StopSignal')


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs).stdout


def config(image):
    return json.loads(run('docker', 'image', 'inspect', image, capture=True))[0]['Config']


def manifest(image):
    value = json.loads(run('docker', 'buildx', 'imagetools', 'inspect', '--raw', image, capture=True))
    if 'manifests' in value:
        selected = [m for m in value['manifests']
                    if m.get('platform', {}).get('architecture') == 'amd64'
                    and m.get('platform', {}).get('os') == 'linux']
        assert len(selected) == 1
        return manifest(PACKAGE + '@' + selected[0]['digest'])
    return value


def fingerprint(image, detailed=False):
    return json.loads(run('docker', 'run', '--rm', '-i', '--network', 'none',
        '--read-only', '--user', '0:0', '--entrypoint', 'python', image, '-',
        *(['--records'] if detailed else []),
        input=(ROOT / 'runtime_fingerprint.py').read_text(), capture=True))


def prepare_images(work, original):
    # COPY the final filesystem eliminates whiteout/dead bytes; metadata is restored explicitly.
    assert not any(original.get(k) for k in ('Volumes', 'ExposedPorts', 'Healthcheck', 'StopSignal', 'OnBuild'))
    lines = [f'FROM {BASE} AS clean', 'USER 0:0',
             'RUN rm -rf /var/lib/apt/lists/* /var/cache/apt/*',
             'FROM scratch', 'COPY --from=clean / /']
    lines += ['ENV ' + key + '=' + json.dumps(value)
              for item in original['Env'] for key, value in [item.split('=', 1)]]
    lines += ['WORKDIR ' + original['WorkingDir'], 'USER ' + original['User'],
              'ENTRYPOINT ' + json.dumps(original['Entrypoint'])]
    if original.get('Cmd') is not None:
        lines += ['CMD ' + json.dumps(original['Cmd'])]
    (work / 'Dockerfile.clean').write_text('\n'.join(lines) + '\n')
    (work / 'Dockerfile.zstd').write_text(f'FROM {BASE}\n')
    run('docker', 'buildx', 'create', '--name', 'webkit-benchmark', '--driver', 'docker-container', '--use')
    images = {'baseline': BASE}
    for variant, compression in [('clean', 'gzip'), ('zstd', 'zstd')]:
        tag = PACKAGE + ':' + os.environ['GITHUB_SHA'] + '-' + variant
        meta = work / (variant + '.json')
        run('docker', 'buildx', 'build', '--platform', 'linux/amd64', '--provenance=false',
            '--file', str(work / ('Dockerfile.' + variant)), '--metadata-file', str(meta),
            '--output', f'type=image,name={tag},push=true,compression={compression},compression-level=6,force-compression=true,oci-mediatypes=true',
            str(work))
        images[variant] = PACKAGE + '@' + json.loads(meta.read_text())['containerimage.digest']

    # The third-party binary gets only local OCI data, not registry credentials.
    archive = work / 'repack.tar.gz'
    urllib.request.urlretrieve(REPACK_URL, archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == REPACK_SHA
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if Path(m.name).name == 'docker-repack' and m.isfile()]
        assert len(members) == 1
        binary = work / 'docker-repack'
        binary.write_bytes(tar.extractfile(members[0]).read())
        binary.chmod(0o755)
    auth = str(Path(os.environ['DOCKER_CONFIG']) / 'config.json')
    run('skopeo', 'copy', '--authfile', auth, 'docker://' + BASE, 'oci:' + str(work / 'source') + ':base')
    isolated = {k: v for k, v in os.environ.items()
                if k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR')}
    isolated.update(HOME=str(work / 'empty-home'), DOCKER_CONFIG=str(work / 'empty-auth'))
    run(str(binary), 'oci://' + str(work / 'source'), 'oci://' + str(work / 'repacked'),
        '--platform', 'linux/amd64', '--target-size', '50MB', '--compression-level', '14',
        '--concurrency', '4', env=isolated)
    tag = PACKAGE + ':' + os.environ['GITHUB_SHA'] + '-repack'
    run('skopeo', 'copy', '--authfile', auth, '--preserve-digests',
        'oci:' + str(work / 'repacked'), 'docker://' + tag)
    details = json.loads(run('skopeo', 'inspect', '--authfile', auth, 'docker://' + tag, capture=True))
    images['repack'] = PACKAGE + '@' + details['Digest']
    return images


def main():
    work = Path('/tmp/webkit-image-benchmark')
    work.mkdir(exist_ok=False)
    run('docker', 'pull', BASE)
    original = config(BASE)
    reference = fingerprint(BASE)
    pinned = ROOT / 'benchmark-images.json'
    images = json.loads(pinned.read_text()) if pinned.exists() else prepare_images(work, original)
    assert images['baseline'] == BASE
    metadata = {}

    for variant, image in images.items():
        run('docker', 'pull', image)
        actual = config(image)
        assert {k: original.get(k) for k in CONFIG_KEYS} == {k: actual.get(k) for k in CONFIG_KEYS}, variant + '_config_changed'
        if fingerprint(image) != reference:
            before = {r[0]: r for r in fingerprint(BASE, True)['records']}
            after = {r[0]: r for r in fingerprint(image, True)['records']}
            differences = [dict(path=k, before=before.get(k), after=after.get(k))
                           for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)]
            Path('image-benchmark-differences.json').write_text(json.dumps(differences, indent=2))
            print('RUNTIME_DIFFERENCES ' + json.dumps(dict(variant=variant, count=len(differences), sample=differences[:20])), flush=True)
            raise AssertionError(variant + '_runtime_changed')
        layers = manifest(image)['layers']
        metadata[variant] = dict(image=image, compressed_bytes=sum(x['size'] for x in layers),
                                 layer_count=len(layers), media_types=sorted({x['mediaType'] for x in layers}))
    report = dict(images=metadata, runtime_fingerprint=reference,
                  repack_version='0.5.0', repack_archive_sha256=REPACK_SHA,
                  baseline=BASE, excluded_cleanup=['/var/lib/apt/lists', '/var/cache/apt'])
    Path('image-benchmark-build.json').write_text(json.dumps(report, indent=2))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('images=' + json.dumps(images) + '\n')
        stream.write('fingerprint=' + reference['sha256'] + '\n')
    print('BENCHMARK_IMAGES ' + json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
