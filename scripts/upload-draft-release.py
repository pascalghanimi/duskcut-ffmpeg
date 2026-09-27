"""Upload a new candidate draft; never replace assets, update or publish a release.

GitHub has no atomic draft lock. Do not manually publish/delete the draft while this
job runs. Every upload addresses the newly created release ID, not a mutable tag.
"""
import hashlib
import json
import pathlib
import re
import subprocess

REPO = 'pascalghanimi/duskcut-ffmpeg'
HASH = re.compile(r'[a-f0-9]{64}')
COMMIT = re.compile(r'[a-f0-9]{40}')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_assets(directory):
    candidate_path = directory / 'ffmpeg-release.candidate.json'
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise ValueError('Missing regular candidate manifest')
    candidate = json.loads(candidate_path.read_text())
    build_id = candidate.get('buildId', '')
    if (candidate.get('schemaVersion') != 1 or candidate.get('approvalStatus') != 'candidate-awaiting-review'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', build_id)
            or not COMMIT.fullmatch(candidate.get('buildRecipeCommit', ''))
            or not COMMIT.fullmatch(candidate.get('ffmpegCommit', ''))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,149}', candidate.get('version', ''))):
        raise ValueError('Expected an assembled, unapproved build candidate')
    files = [directory / f'duskcut-ffmpeg-{build_id}-win64.zip',
             directory / f'duskcut-ffmpeg-{build_id}-corresponding-source.tar',
             candidate_path, directory / 'SOURCE-BINDING.json', directory / 'SHA256SUMS.txt']
    if any(not p.is_file() or p.is_symlink() for p in files):
        raise ValueError('Missing regular release asset')
    if {p.name for p in directory.iterdir()} != {p.name for p in files}:
        raise ValueError('Unexpected file in release directory')
    sums = {}
    for line in files[-1].read_text().splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in sums:
            raise ValueError('Invalid or duplicate release checksum record')
        sums[match[2]] = match[1]
    if set(sums) != {p.name for p in files[:-1]}:
        raise ValueError('Incomplete release checksum inventory')
    digests = {p.name: file_hash(p) for p in files[:-1]}
    if digests != sums:
        raise ValueError('Release checksum mismatch')
    for path, key, maximum in zip(files[:2], ('archive', 'source'), (700 * 1024 ** 2, 2 ** 31 - 1)):
        record = candidate.get(key, {})
        if (record.get('sha256') != digests[path.name] or type(record.get('size')) is not int
                or not 0 < record['size'] <= maximum or record['size'] != path.stat().st_size
                or record.get('url') != f'https://github.com/{REPO}/releases/download/{build_id}/{path.name}'):
            raise ValueError('Release asset changed after assembly')
    binding = json.loads(files[3].read_text())
    if (binding.get('schemaVersion') != 1
            or binding.get('status') != 'assembled-awaiting-source-runtime-and-functional-review'
            or binding.get('buildId') != build_id
            or binding.get('buildRecipeCommit') != candidate['buildRecipeCommit']
            or binding.get('ffmpegRevision') != candidate['ffmpegCommit']
            or binding.get('versionTokenProvidedByOperator') != candidate['version']
            or not HASH.fullmatch(binding.get('buildLockSha256', ''))
            or not HASH.fullmatch(binding.get('sourceManifestSha256', ''))):
        raise ValueError('Source binding and candidate identity differ')
    rows = candidate.get('files', [])
    if not isinstance(rows, list) or not rows:
        raise ValueError('Missing runtime inventory')
    names = [row.get('path') for row in rows]
    if any(not isinstance(name, str) for name in names) or len(set(name.casefold() for name in names)) != len(names):
        raise ValueError('Duplicate or invalid runtime inventory path')
    for binary in ('ffmpeg.exe', 'ffprobe.exe'):
        records = [row for row in rows if row['path'] == 'bin/' + binary]
        expected = binding.get('binaryHashes', {}).get(binary, '')
        if len(records) != 1 or not HASH.fullmatch(expected) or records[0].get('sha256') != expected:
            raise ValueError('Binary hashes differ between runtime inventory and source binding')
    return candidate, files


def api(*args, payload=None):
    return json.loads(subprocess.check_output(['gh', 'api', *args], text=True,
                      input=json.dumps(payload) if payload is not None else None))


def assert_draft(release, release_id, candidate):
    if (type(release.get('id')) is not int or release['id'] != release_id
            or release.get('draft') is not True or release.get('tag_name') != candidate['buildId']
            or release.get('target_commitish') != candidate['buildRecipeCommit']):
        raise ValueError('New release identity changed or draft was published; stopping uploads')


def upload():
    candidate, files = validate_assets(pathlib.Path('packaged-release'))
    # Enumerate all pages. A network/auth error is fatal, never interpreted as an absent tag.
    pages = api('--paginate', '--slurp', f'repos/{REPO}/releases?per_page=100')
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError('Invalid release listing')
    if any(row.get('tag_name') == candidate['buildId'] for page in pages for row in page):
        raise ValueError('Release already exists; neither drafts nor public releases are overwritten')
    release = api('--method', 'POST', f'repos/{REPO}/releases', '--input', '-', payload={
        'tag_name': candidate['buildId'], 'target_commitish': candidate['buildRecipeCommit'],
        'name': 'DuskCut FFmpeg ' + candidate['buildId'], 'draft': True,
        'body': 'Candidate only. Runtime compatibility and source-correspondence review must pass '
                'before publication or selection by DuskCut. Do not publish while assembly is running.',
    })
    release_id = release.get('id')
    if type(release_id) is not int or release_id <= 0:
        raise ValueError('GitHub returned no release ID')
    assert_draft(release, release_id, candidate)
    for path in files:
        current = api(f'repos/{REPO}/releases/{release_id}')
        assert_draft(current, release_id, candidate)
        if any(row.get('name') == path.name for row in current.get('assets', [])):
            raise ValueError('Release asset already exists; refusing replacement')
        # The assets API rejects duplicate names; no --clobber or DELETE ever occurs.
        asset = api('--method', 'POST',
                    f'https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets?name={path.name}',
                    '--header', 'Content-Type: application/octet-stream', '--input', str(path))
        if (asset.get('name') != path.name or asset.get('size') != path.stat().st_size
                or asset.get('state') != 'uploaded'):
            raise ValueError('GitHub did not confirm the uploaded asset')
        remote_digest = asset.get('digest')
        if remote_digest is not None and remote_digest != 'sha256:' + file_hash(path):
            raise ValueError('GitHub reported an unexpected uploaded asset hash')
    assert_draft(api(f'repos/{REPO}/releases/{release_id}'), release_id, candidate)
    print('Candidate assets uploaded to a new draft; nothing was replaced or published')


if __name__ == '__main__':
    upload()
