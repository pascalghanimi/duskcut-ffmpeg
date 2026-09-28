"""Acquire a successful build and replay its exact published input manifest.

Runs only on an isolated GitHub runner. No private editor checkout is used.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess

REPO = 'pascalghanimi/duskcut-ffmpeg'


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def validate_manifest(manifest):
    if (manifest.get('schemaVersion') != 1 or manifest.get('extractTo') != '.'
            or not re.fullmatch(r'sources-[a-z0-9.-]+', manifest.get('releaseTag', ''))
            or not re.fullmatch(r'[a-f0-9]{64}', manifest.get('sourceManifestSha256', ''))
            or not isinstance(manifest.get('assets'), list) or not manifest['assets']):
        raise ValueError('Invalid pinned source release manifest')
    supplement = manifest.get('supplementalManifestSha256')
    if supplement is not None and not re.fullmatch(r'[a-f0-9]{64}', str(supplement)):
        raise ValueError('Invalid supplemental source manifest hash')
    names = set()
    for asset in manifest['assets']:
        name = asset.get('file', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+\.tar(?:\.gz|\.xz)?', name)
                or name.casefold() in names or type(asset.get('bytes')) is not int
                or not 0 < asset['bytes'] < 2 ** 31
                or not re.fullmatch(r'[a-f0-9]{64}', asset.get('sha256', ''))
                or asset.get('url') != f'https://github.com/{REPO}/releases/download/{manifest["releaseTag"]}/{name}'):
            raise ValueError('Invalid or duplicate pinned source asset')
        names.add(name.casefold())


def copy_runtime_review(artifact):
    result = json.loads((pathlib.Path(artifact) / 'BUILD-RESULT.json').read_text())
    build_id = result.get('buildId', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', build_id):
        raise ValueError('Unsafe build identity for runtime review selection')
    source = pathlib.Path('public-source/runtime-review') / build_id
    target = pathlib.Path('runtime-review')
    if target.exists() or source.is_symlink() or not source.resolve().is_relative_to(pathlib.Path.cwd().resolve()):
        raise ValueError('Unsafe or existing runtime review directory')
    manifest_path = source / 'runtime-review.json'
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 1024 ** 2:
        raise ValueError('Missing reviewed runtime evidence for this build')
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('schemaVersion') != 1 or manifest.get('buildId') != build_id
            or manifest.get('buildLockSha256') != result.get('lockSha256')
            or manifest.get('binaryHashes') != result.get('binaryHashes')):
        raise ValueError('Tracked runtime review is bound to another build')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > 20:
        raise ValueError('Invalid tracked runtime review inventory')
    files = {'runtime-review.json': manifest_path}
    total = 0
    for row in rows:
        name = row.get('file', '')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.(?:txt|md|json)', name)
                or name.casefold() in {key.casefold() for key in files}
                or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= 1024 ** 2
                or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', ''))):
            raise ValueError('Unsafe or duplicate tracked runtime review file')
        path = source / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
            raise ValueError('Tracked runtime review file size or type differs')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                raise ValueError('Tracked runtime review file SHA-256 differs')
        files[name] = path
        total += row['bytes']
    if total > 4 * 1024 ** 2 or {entry.name for entry in source.iterdir()} != set(files):
        raise ValueError('Unexpected or oversized tracked runtime review content')
    target.mkdir(exist_ok=False)
    for name, path in files.items():
        shutil.copyfile(path, target / name)


def prepare():
    run_id = os.environ.get('BUILD_RUN', '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id):
        raise ValueError('Expected a numeric build run ID')
    metadata = json.loads(command('gh', 'api', f'repos/{REPO}/actions/runs/{run_id}'))
    if (str(metadata.get('id')) != run_id
            or metadata.get('status') != 'completed' or metadata.get('conclusion') != 'success'
            or metadata.get('path') != '.github/workflows/build.yml'
            or metadata.get('head_branch') != 'main'
            or metadata.get('event') != 'workflow_dispatch'
            or metadata.get('repository', {}).get('full_name') != REPO):
        raise ValueError('Expected a successful manual controlled-build run on main')
    revision = metadata.get('head_sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Build recipe revision missing')
    subprocess.run(['git', 'merge-base', '--is-ancestor', revision, 'HEAD'], check=True)
    artifacts = json.loads(command('gh', 'api', f'repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100'))
    if artifacts.get('total_count') != len(artifacts.get('artifacts', [])):
        raise ValueError('Incomplete build artifact listing')
    matches = [a for a in artifacts['artifacts'] if not a['expired'] and
               re.fullmatch(r'duskcut-ffmpeg-[a-zA-Z0-9_.-]+-' + run_id, a['name'])]
    if len(matches) != 1:
        raise ValueError('Expected exactly one complete build artifact')
    run = matches[0].get('workflow_run', {})
    if str(run.get('id')) != run_id or run.get('head_sha') != revision:
        raise ValueError('Artifact metadata belongs to a different workflow run')
    for output in ('build-artifact', 'build-release-assets.json', 'source-staging', 'downloads', 'runtime-review'):
        if pathlib.Path(output).exists():
            raise ValueError('Acquisition output already exists: ' + output)
    root = pathlib.Path('build-artifact')
    root.mkdir(exist_ok=False)
    subprocess.run(['gh', 'run', 'download', run_id, '--repo', REPO, '--name',
                    matches[0]['name'], '--dir', str(root)], check=True)
    if (root / 'BUILD-RECIPE-COMMIT.txt').read_text().strip() != revision:
        raise ValueError('Artifact recipe identity does not match workflow revision')
    # The review is intentionally post-build and comes from this checked-out public
    # recipe revision, not the historical compilation snapshot. The packager binds
    # its original build/lock/binary/compiler evidence again before creating assets.
    copy_runtime_review(root)
    manifest = pathlib.Path('build-release-assets.json')
    # git show yields bytes from the exact recipe commit, without shell interpolation.
    manifest_data = subprocess.check_output(['git', 'show', revision + ':release-assets.json'])
    parsed_manifest = json.loads(manifest_data)
    validate_manifest(parsed_manifest)
    manifest.write_bytes(manifest_data)
    subprocess.run(['node', 'scripts/fetch-inputs.mjs', str(manifest), 'downloads'], check=True)
    subprocess.run(['python3', 'scripts/extract-inputs.py', str(manifest), 'downloads',
                    'source-staging'], check=True)
    staging = pathlib.Path('source-staging')
    shutil.copyfile(manifest, staging / 'release-assets.json')
    # Source parts are included unmodified alongside their extracted manifest/files.
    for asset in parsed_manifest['assets']:
        shutil.copyfile(pathlib.Path('downloads') / asset['file'], staging / asset['file'])
    print('Verified build artifact and exact source inputs ready for assembly')


if __name__ == '__main__':
    prepare()
