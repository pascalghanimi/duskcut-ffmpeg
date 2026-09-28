"""Assemble reviewable FFmpeg runtime/source assets from a completed pinned build.

No executable is run and no release is published or approved by this helper.
"""
import argparse
import hashlib
import io
import json
import pathlib
import posixpath
import re
import subprocess
import tarfile
import zipfile

REPOSITORY = 'https://github.com/pascalghanimi/duskcut-ffmpeg'
MAX_ASSET_BYTES = 2 ** 31 - 1
MAX_RUNTIME_BYTES = 700 * 1024 ** 2
MAX_TEXT_BYTES = 256 * 1024 ** 2
DOCS = ('LICENSE', 'README.md', 'SOURCES.md', 'THIRD-PARTY-NOTICES.txt')
CONTROLS = ('container-generate.sh', 'container-compile.sh', 'safe_extract.py',
            'capture_environment.py', 'prepare_recipe.py', 'profile.json')
# These are produced unconditionally by the pinned container scripts and orchestrator.
# A ZIP of executables plus an incomplete hand-selected log must not pass for build evidence.
CONFIGURATION = (
    'Dockerfile.duskcut', 'generated-Dockerfile-original', 'duskcut-recipe.patch',
    'profile.json', 'config.h', 'config_components.h', 'config.asm', 'config.mak',
    'config.log', 'build-environment.json', 'compiler-version.txt',
    'compiler-cxx-version.txt', 'linker-version.txt', 'compiler-image-packages.txt',
    'dependency-prefix-sha256.txt', 'compiler-runtime-archive-sha256.txt',
    'ffmpeg-pe-imports.txt', 'ffprobe-pe-imports.txt', 'binary-sha256.txt',
)
BUILD_RECORDS = (
    'work/toolchain-build-environment.json', 'work/toolchain-image.json',
    'work/docker-version.json', 'work/dependency-image.json', 'work/compile-result.json',
    'work/selected-source-cache-files.txt', 'work/generate.log',
    'work/dependencies.log', 'work/ffmpeg-build.log',
)
# The successful recipe-generation phase may produce no stdout/stderr. Keep its log present,
# while requiring substantive configuration, compiler output and compile logs to contain data.
EMPTY_LOGS_ALLOWED = frozenset(('work/generate.log',))
HASH = re.compile(r'[a-f0-9]{64}')
COMMIT = re.compile(r'[a-f0-9]{40}')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,149}')
SECRET = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
                    rb'|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
                    rb'|[?&](?:X-Amz-Signature|sig|signature)=[A-Za-z0-9%+/]{16,})', re.I)
# Reviewed public security test in build commit 323eb1d63d630b4bd4e8ae4c89650266534731e8.
# Contains deliberate placeholder strings and a bare PRIVATE KEY header, no key data.
# Only this exact path AND every original byte are exempt; no generic test-file skip.
AUDITED_TEST_FIXTURES = {
    'build/capture_environment_test.py': 'c46a784627045481a4698bac7f67b31cd0ddfae5a0fedef9e5c2ca87042f30e2',
}


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_file(path):
    hasher = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(data)
    return hasher.hexdigest()


def safe_name(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value:
        raise ValueError('Unsafe relative path')
    parts = value.rstrip('/').split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith(('.', ' '))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.match(r'^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', part, re.I)):
            raise ValueError('Unsafe relative path: ' + value)
    return '/'.join(parts)


def regular(root, relative):
    name = safe_name(relative)
    root = pathlib.Path(root).resolve()
    path = root.joinpath(*name.split('/'))
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Missing or unsafe regular file: ' + name)
    return path


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()


def identity(path):
    return {'size': pathlib.Path(path).stat().st_size, 'sha256': digest_file(path)}


def verify_identity(path, size, digest):
    if (type(size) is not int or size < 1 or not HASH.fullmatch(str(digest))
            or pathlib.Path(path).stat().st_size != size or digest_file(path) != digest):
        raise ValueError('Input size or SHA-256 mismatch: ' + pathlib.Path(path).name)


def archive_files(archive):
    files, known = {}, {}
    total = 0
    for member in archive.getmembers():
        name = safe_name(member.name[2:] if member.name.startswith('./') else member.name)
        if not (member.isfile() or member.isdir()):
            raise ValueError('Archive must contain regular files/directories: ' + name)
        parts = name.split('/')
        for count in range(1, len(parts) + 1):
            parent = '/'.join(parts[:count])
            kind = 'file' if count == len(parts) and member.isfile() else 'dir'
            previous = known.get(parent.casefold())
            if previous and (previous != (parent, kind) or kind == 'file'):
                raise ValueError('Duplicate or conflicting archive path: ' + parent)
            known[parent.casefold()] = (parent, kind)
        if member.isfile():
            if member.size < 0 or member.size > MAX_ASSET_BYTES:
                raise ValueError('Oversized archive member')
            files[name] = member
            total += member.size
            if total > 4 * MAX_ASSET_BYTES:
                raise ValueError('Oversized expanded archive')
    return files


def member_bytes(archive, members, name, maximum=MAX_TEXT_BYTES):
    member = members.get(name)
    if not member or member.size > maximum:
        raise ValueError('Missing or oversized evidence: ' + name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError('Evidence is not regular data')
    data = stream.read(maximum + 1)
    if len(data) != member.size:
        raise ValueError('Incomplete archive member')
    return data


def repository_snapshot(repository, revision):
    if not COMMIT.fullmatch(revision):
        raise ValueError('The exact build-recipe commit is required')
    raw = subprocess.check_output(['git', '-C', str(repository), 'ls-tree', '-rz', '--full-tree', revision])
    files = {}
    for record in raw.split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        mode, kind, _ = metadata.decode().split()
        name = raw_name.decode('utf-8')
        include = (name in ('README.md', 'LICENSE', '.gitattributes', '.gitignore', 'release-assets.json')
                   or name.startswith(('build/', 'scripts/', '.github/workflows/', 'public-source/')))
        if not include:
            continue
        safe_name(name)
        if (mode not in ('100644', '100755') or kind != 'blob'
                or any(part in ('.git', 'node_modules', '__pycache__') for part in name.split('/'))
                or name.endswith(('.exe', '.zip', '.tar', '.xz', '.gz', '.pyc'))
                or not (pathlib.PurePosixPath(name).suffix.lower() in ('.md', '.txt', '.json', '.mjs', '.js', '.py', '.sh', '.yml', '.yaml')
                        or name in ('LICENSE', '.gitattributes', '.gitignore'))):
            raise ValueError('Unexpected tracked build-recipe file: ' + name)
        data = subprocess.check_output(['git', '-C', str(repository), 'show', revision + ':' + name])
        audited_fixture = AUDITED_TEST_FIXTURES.get(name) == digest_bytes(data)
        if len(data) > 8 * 1024 ** 2 or (SECRET.search(data) and not audited_fixture):
            raise ValueError('Unreviewed or oversized repository content: ' + name)
        files[name] = data
    required = ['build/controlled-build.mjs', 'build/create-lock.mjs', 'scripts/extract-inputs.py']
    if not all(name in files for name in required):
        raise ValueError('Build commit does not contain the required replay scripts')
    return files


def source_records(inputs, manifest, supplemental_hash=None):
    """Normalize both source inventories without allowing a supplement to replace base data."""
    records = [{**row, 'file': 'sources/' + row['file']} for row in manifest['files'] + manifest['notices']]
    supplemental_path = pathlib.Path(inputs) / 'sources/supplemental-manifest.json'
    if supplemental_hash is None:
        if supplemental_path.exists():
            raise ValueError('Unbound supplemental source manifest')
        return records, None, None
    supplemental_path = regular(inputs, 'sources/supplemental-manifest.json')
    if not HASH.fullmatch(str(supplemental_hash)) or digest_file(supplemental_path) != supplemental_hash:
        raise ValueError('Supplemental source manifest hash mismatch')
    supplement = read_json(supplemental_path)
    if (supplement.get('schemaVersion') != 1 or not isinstance(supplement.get('files'), list)
            or not supplement['files'] or not isinstance(supplement.get('notices'), list)):
        raise ValueError('Invalid supplemental source manifest')
    names = {row['file'].casefold() for row in records}
    names.update(('source-manifest.json', 'sources/source-manifest.json', 'sources/readme.md',
                  'sources/supplemental-manifest.json'))
    names.update('sources/distribution/' + name.casefold() for name in DOCS)
    ids = {row['id'] for row in records if 'id' in row}
    for row in supplement['files'] + supplement['notices']:
        name = safe_name(row.get('file'))
        if (not name.startswith('sources/') or name.casefold() in names
                or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in ids
                or row.get('role') not in ('source', 'license-evidence', 'review-evidence')):
            raise ValueError('Duplicate, replacing or invalid supplemental source')
        names.add(name.casefold())
        ids.add(row['id'])
        verify_identity(regular(inputs, name), row.get('bytes'), row.get('sha256'))
        records.append(row)
    return records, supplemental_path, supplement


def verify_build(artifact, inputs, repository, version):
    if not TOKEN.fullmatch(version):
        raise ValueError('Pass the exact version token observed on Windows')
    result_path = regular(artifact, 'BUILD-RESULT.json')
    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)
    if result.get('schemaVersion') != 1 or result.get('status') != 'built-not-approved-for-distribution':
        raise ValueError('A completed controlled BUILD-RESULT is required')
    for key in ('ffmpegRevision', 'recipeRevision'):
        if not COMMIT.fullmatch(result.get(key, '')):
            raise ValueError('Build result has no complete source revision')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', result.get('buildId', '')):
        raise ValueError('Invalid build ID')
    binaries = {}
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        path = regular(artifact, 'work/binary/bin/' + name)
        if path.stat().st_size > 300 * 1024 ** 2 or digest_file(path) != result.get('binaryHashes', {}).get(name):
            raise ValueError('Binary does not match BUILD-RESULT: ' + name)
        with path.open('rb') as stream:
            if stream.read(2) != b'MZ':
                raise ValueError('Expected a Windows executable: ' + name)
        binaries['bin/' + name] = path
    revision = regular(artifact, 'BUILD-RECIPE-COMMIT.txt').read_text().strip()
    if regular(artifact, 'BUILD-RECIPE-STATUS.txt').read_text().strip():
        raise ValueError('The build recorded uncommitted recipe changes')
    snapshot = repository_snapshot(repository, revision)
    manifest_path = regular(inputs, 'source-manifest.json')
    manifest = read_json(manifest_path)
    if (manifest.get('ffmpegRevision') != result['ffmpegRevision']
            or manifest.get('recipeRevision') != result['recipeRevision']):
        raise ValueError('Source manifest does not match build source revisions')
    evidence_path = regular(artifact, 'ffmpeg-build-evidence.tar.xz')
    sums = regular(artifact, 'SHA256SUMS.txt').read_text().splitlines()
    expected = [line.split()[0] for line in sums if line.split()[-1:] == ['ffmpeg-build-evidence.tar.xz']]
    if len(expected) != 1 or digest_file(evidence_path) != expected[0]:
        raise ValueError('Build evidence archive hash mismatch')
    with tarfile.open(evidence_path, 'r:*') as evidence:
        members = archive_files(evidence)
        for required in tuple('work/configuration/' + name for name in CONFIGURATION) + BUILD_RECORDS:
            member = members.get(required)
            if member is None or (member.size == 0 and required not in EMPTY_LOGS_ALLOWED):
                raise ValueError('Missing or empty required build evidence: ' + required)
        for name, member in members.items():
            allowed = (name in ('BUILD-RESULT.json', 'inputs/build-lock.json')
                       or name.startswith(('work/control/', 'work/configuration/'))
                       or name in ('work/toolchain-build-environment.json', 'work/toolchain-image.json',
                                   'work/docker-version.json', 'work/dependency-image.json', 'work/compile-result.json',
                                   'work/selected-source-cache-files.txt', 'work/generate.log',
                                   'work/dependencies.log', 'work/ffmpeg-build.log'))
            if not allowed:
                raise ValueError('Unexpected build evidence member: ' + name)
            stream = evidence.extractfile(member)
            tail = b''
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                if SECRET.search(tail + block):
                    raise ValueError('Credential-like text in build evidence: ' + name)
                tail = block[-1024:]
        if member_bytes(evidence, members, 'BUILD-RESULT.json') != result_bytes:
            raise ValueError('Standalone and archived BUILD-RESULT differ')
        lock_bytes = member_bytes(evidence, members, 'inputs/build-lock.json')
        lock = json.loads(lock_bytes)
        if digest_bytes(lock_bytes) != result.get('lockSha256'):
            raise ValueError('Build lock hash does not match result')
        if lock.get('sourceManifest', {}).get('sha256') != digest_file(manifest_path):
            raise ValueError('Build lock does not bind these source inputs')
        extra = lock.get('supplementalSourceManifest')
        if extra is not None and (not isinstance(extra, dict)
                                  or extra.get('file') != 'sources/supplemental-manifest.json'):
            raise ValueError('Unexpected supplemental manifest path')
        records, supplemental_path, supplement = source_records(inputs, manifest, extra.get('sha256') if extra else None)
        if supplement:
            dlg, locked_dlg = supplement.get('freetypeDlg', {}), lock.get('freetypeDlg', {})
            rows_by_id = {row.get('id'): row for row in supplement['files'] + supplement['notices']}
            if (dlg.get('id') != 'freetype-dlg' or not COMMIT.fullmatch(dlg.get('revision', ''))
                    or not COMMIT.fullmatch(dlg.get('parentRevision', ''))
                    or locked_dlg.get('blobId') != dlg['id']
                    or locked_dlg.get('noticeBlobId') != 'freetype-dlg-license'
                    or any(locked_dlg.get(key) != dlg[key] for key in ('revision', 'parentRevision'))
                    or rows_by_id.get('freetype-dlg', {}).get('role') != 'source'
                    or rows_by_id.get('freetype-dlg-license', {}).get('role') != 'license-evidence'
                    or any(rows_by_id.get(key, {}).get('revision') != dlg['revision']
                           for key in ('freetype-dlg', 'freetype-dlg-license'))):
                raise ValueError('Supplemental source identity differs from executed lock')
        if lock.get('buildId') != result['buildId'] or lock.get('schemaVersion') != 2:
            raise ValueError('Build lock identity mismatch')
        if json.loads(member_bytes(evidence, members, 'work/control/lock.json')) != lock:
            raise ValueError('Executed control lock differs from input lock')
        for control in CONTROLS:
            if member_bytes(evidence, members, 'work/control/' + control) != snapshot.get('build/' + control):
                raise ValueError('Executed build control differs from recorded commit: ' + control)
        profile = member_bytes(evidence, members, 'work/control/profile.json')
        if digest_bytes(profile) != lock.get('profile', {}).get('sha256'):
            raise ValueError('Executed profile differs from locked profile')
        if member_bytes(evidence, members, 'work/configuration/profile.json') != profile:
            raise ValueError('Generated configuration profile differs from executed profile')
        compiled = json.loads(member_bytes(evidence, members, 'work/compile-result.json'))
        if any(compiled.get(key) != result[key] for key in ('buildId', 'ffmpegRevision', 'recipeRevision')):
            raise ValueError('Compile-stage result differs from final build result')
        pinned = {(row['file'], row['bytes'], row['sha256']) for row in records}
        for blob in result.get('sourceInputs', []):
            if (blob['file'], blob['bytes'], blob['sha256']) not in pinned:
                raise ValueError('Build used a source outside the packaged manifest')
        binding = lambda rows: sorted((row['id'], row['file'], row['bytes'], row['sha256']) for row in rows)
        if not result.get('sourceInputs') or binding(result['sourceInputs']) != binding(lock['blobs']):
            raise ValueError('Build result source inputs differ from executed lock')
    for row in records:
        path = regular(inputs, row['file'])
        verify_identity(path, row['bytes'], row['sha256'])
    return result, revision, snapshot, binaries, evidence_path, manifest_path, supplemental_path, supplement


def verify_input_parts(inputs, manifest_path):
    release_path = regular(inputs, 'release-assets.json')
    release = read_json(release_path)
    if release.get('schemaVersion') != 1 or release.get('sourceManifestSha256') != digest_file(manifest_path):
        raise ValueError('Source release manifest mismatch')
    if (release.get('extractTo') != '.'
            or not re.fullmatch(r'sources-[a-z0-9.-]+', release.get('releaseTag', ''))
            or not isinstance(release.get('assets'), list) or not release['assets']):
        raise ValueError('Source release metadata cannot be replayed by the extractor')
    parts, names, archived_names = [], set(), set()
    required = {'source-manifest.json', 'sources/source-manifest.json', 'sources/README.md'}
    source_manifest = read_json(manifest_path)
    records, supplemental_path, _ = source_records(inputs, source_manifest, release.get('supplementalManifestSha256'))
    required.update(row['file'] for row in records)
    if supplemental_path:
        required.add('sources/supplemental-manifest.json')
    required.update('sources/distribution/' + name for name in DOCS)
    for row in release.get('assets', []):
        name = safe_name(row['file'])
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.tar(?:\.gz|\.xz)?', name) or name.casefold() in names:
            raise ValueError('Duplicate or unexpected source asset path')
        expected_url = REPOSITORY + '/releases/download/' + release['releaseTag'] + '/' + name
        if row.get('url') != expected_url:
            raise ValueError('Source asset URL is missing or cannot be replayed by the extractor')
        names.add(name.casefold())
        path = regular(inputs, name)
        verify_identity(path, row['bytes'], row['sha256'])
        if path.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError('Source input part exceeds 2 GiB')
        with tarfile.open(path, 'r:*') as archive:
            members = archive_files(archive)
            for member_name, member in members.items():
                if member_name not in required or member_name.casefold() in archived_names:
                    raise ValueError('Unexpected or duplicate source-part member: ' + member_name)
                archived_names.add(member_name.casefold())
                local = regular(inputs, member_name)
                hasher = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        hasher.update(block)
                if local.stat().st_size != member.size or digest_file(local) != hasher.hexdigest():
                    raise ValueError('Staged source differs from immutable input part: ' + member_name)
        parts.append(path)
    if not parts:
        raise ValueError('No original source parts supplied')
    if archived_names != {name.casefold() for name in required}:
        raise ValueError('Original source parts do not contain the full manifest and notices')
    return release_path, parts


def verify_runtime_review(review_directory, result, revision, evidence_path):
    """Bind supplementary post-build runtime-license evidence, never amend locked inputs."""
    with tarfile.open(evidence_path, 'r:*') as evidence:
        members = archive_files(evidence)
        compile_log = member_bytes(evidence, members, 'work/ffmpeg-build.log')
        # Preserve the actual linker path. Lexical '..' normalization can change
        # which file is selected when an earlier directory is a symlink. The audit
        # resolves and hashes this exact path inside the original pinned image.
        linked_paths = {line.strip() for line in compile_log.decode('utf-8', errors='replace').splitlines()
                        if re.fullmatch(r'/opt/ct-ng/[^\s]+\.a', line.strip())}
        needs_review = b'libatomic.a' in compile_log or b'-latomic' in compile_log
        if review_directory is None:
            if needs_review:
                raise ValueError('Linked libatomic requires bound post-build runtime review evidence')
            return None
        if not needs_review:
            raise ValueError('Runtime review supplied without a corresponding libatomic link record')
        lock = json.loads(member_bytes(evidence, members, 'inputs/build-lock.json'))
        toolchain = json.loads(member_bytes(evidence, members, 'work/toolchain-image.json'))
        compiler_version = member_bytes(evidence, members, 'work/configuration/compiler-version.txt').decode().splitlines()[0]
    root = pathlib.Path(review_directory)
    manifest_path = regular(root, 'runtime-review.json')
    if manifest_path.stat().st_size > 1024 ** 2:
        raise ValueError('Oversized runtime review manifest')
    manifest = read_json(manifest_path)
    if (manifest.get('schemaVersion') != 1 or manifest.get('buildId') != result['buildId']
            or manifest.get('buildLockSha256') != result['lockSha256']
            or manifest.get('binaryHashes') != result['binaryHashes']
            or manifest.get('compilerImage') != lock.get('toolchain', {}).get('image')
            or manifest.get('compilerImage') != toolchain.get('reference')
            or not re.fullmatch(r'ghcr\.io/[a-z0-9/_.-]+@sha256:[a-f0-9]{64}', manifest.get('compilerImage', ''))):
        raise ValueError('Runtime review is not bound to this build, binaries and compiler image')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > 20:
        raise ValueError('Invalid runtime review file inventory')
    paths, records, seen_ids, total = {'runtime-review.json': manifest_path}, {}, set(), 0
    for row in rows:
        name = safe_name(row.get('file'))
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.(?:txt|md|json)', name)
                or name.casefold() in {path.casefold() for path in paths}
                or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in seen_ids
                or row.get('role') not in ('license', 'source-header', 'linkage-evidence', 'review')):
            raise ValueError('Unsafe or duplicate runtime review file')
        path = regular(root, name)
        verify_identity(path, row.get('bytes'), row.get('sha256'))
        total += path.stat().st_size
        if path.stat().st_size > 1024 ** 2 or total > 4 * 1024 ** 2 or SECRET.search(path.read_bytes()):
            raise ValueError('Unreviewed or oversized runtime review content')
        seen_ids.add(row['id'])
        paths[name], records[name] = path, row
    if {entry.name for entry in root.iterdir()} != set(paths) or SECRET.search(manifest_path.read_bytes()):
        raise ValueError('Unexpected file or credential-like text in runtime review')
    atomic = manifest.get('libatomic', {})
    if (not HASH.fullmatch(atomic.get('archiveSha256', ''))
            or not COMMIT.fullmatch(atomic.get('sourceRevision', ''))
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', atomic.get('compilerVersion', ''))
            or not compiler_version.endswith(' ' + atomic['compilerVersion'])):
        raise ValueError('Runtime source or compiler identity is missing')
    for key, role in (('linkageEvidenceFile', 'linkage-evidence'), ('licenseFile', 'license'),
                      ('exceptionFile', 'license'), ('sourceHeaderFile', 'source-header')):
        if records.get(atomic.get(key), {}).get('role') != role:
            raise ValueError('Missing required runtime source, license or link evidence: ' + key)
    audit = read_json(paths[atomic['linkageEvidenceFile']])
    audited_build, audited_toolchain = audit.get('build', {}), audit.get('toolchain', {})
    if (audit.get('schemaVersion') != 1 or audit.get('kind') != 'post-build-toolchain-runtime-provenance'
            or audited_build.get('repository') != 'pascalghanimi/duskcut-ffmpeg'
            or audited_build.get('recipeCommit') != revision
            or audited_build.get('lockSha256') != result['lockSha256']
            or audited_build.get('binaryHashes') != result['binaryHashes']
            or audited_build.get('evidenceSha256') != digest_file(evidence_path)
            or audited_toolchain.get('image') != manifest['compilerImage']
            or audited_toolchain.get('imageId') != toolchain.get('Id')
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(toolchain.get('Id')))
            or audited_toolchain.get('compilerVersion') != compiler_version):
        raise ValueError('Post-build runtime audit does not match original compiler/build evidence')
    archives = [row for row in audit.get('archives', []) if row.get('name') == 'libatomic.a']
    if (len(archives) != 1 or archives[0].get('sha256') != atomic['archiveSha256']
            or type(archives[0].get('bytes')) is not int or not 8 <= archives[0]['bytes'] <= 100 * 1024 ** 2
            or archives[0].get('presentInBuildLinkTrace') is not True
            or not re.fullmatch(r'/opt/ct-ng/[A-Za-z0-9_./+-]+/libatomic\.a', archives[0].get('path', ''))
            or posixpath.normpath(archives[0]['path']) != archives[0]['path']):
        raise ValueError('libatomic archive hash/path is not supported by the actual link trace')
    archive = archives[0]
    traces = archive.get('linkTraceFiles')
    if not isinstance(traces, list) or not 1 <= len(traces) <= 100:
        raise ValueError('Missing resolved libatomic link trace records')
    seen_traces = set()
    for trace in traces:
        recorded = trace.get('recordedPath', '')
        if (recorded not in linked_paths or recorded in seen_traces
                or posixpath.basename(recorded) != 'libatomic.a'
                or trace.get('resolvedPath') != archive['path']
                or trace.get('bytes') != archive['bytes'] or trace.get('sha256') != archive['sha256']):
            raise ValueError('Resolved libatomic link trace differs from the original build or compiler archive')
        seen_traces.add(recorded)
    return {'manifest': manifest, 'paths': paths, 'manifestSha256': digest_file(manifest_path)}


def add_tar_bytes(archive, name, data):
    info = tarfile.TarInfo(safe_name(name))
    info.size = len(data)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))


def add_tar_file(archive, name, path):
    info = tarfile.TarInfo(safe_name(name))
    info.size = pathlib.Path(path).stat().st_size
    info.mode = 0o644
    with pathlib.Path(path).open('rb') as source:
        archive.addfile(info, source)


def package(artifact, inputs, repository, output, version, runtime_review=None):
    artifact, inputs, repository, output = map(pathlib.Path, (artifact, inputs, repository, output))
    if output.exists():
        raise ValueError('Output directory must be new')
    result, revision, snapshot, binaries, evidence_path, manifest_path, supplemental_path, supplement = verify_build(artifact, inputs, repository, version)
    release_path, parts = verify_input_parts(inputs, manifest_path)
    if json.loads(snapshot.get('release-assets.json', b'null')) != read_json(release_path):
        raise ValueError('Source assets are not those pinned by the actual build commit')
    runtime_evidence = verify_runtime_review(runtime_review, result, revision, evidence_path)
    # Conservative preflight estimate stops before writing an oversized source asset.
    estimated = sum(p.stat().st_size for p in parts) + evidence_path.stat().st_size + sum(map(len, snapshot.values())) + 8 * 1024 ** 2
    if estimated > MAX_ASSET_BYTES:
        raise ValueError('Corresponding source archive would exceed 2 GiB; split explicitly before release')
    output.mkdir(parents=True, exist_ok=False)
    build_id = result['buildId']
    runtime_name = f'duskcut-ffmpeg-{build_id}-win64.zip'
    source_name = f'duskcut-ffmpeg-{build_id}-corresponding-source.tar'
    binding = {'schemaVersion': 1, 'status': 'assembled-awaiting-source-runtime-and-functional-review',
               'buildId': build_id, 'versionTokenProvidedByOperator': version, 'buildRecipeCommit': revision,
               'ffmpegRevision': result['ffmpegRevision'], 'buildLockSha256': result['lockSha256'],
               'sourceManifestSha256': digest_file(manifest_path), 'binaryHashes': result['binaryHashes'],
               'buildEvidence': identity(evidence_path), 'sourceInputAssets': read_json(release_path)['assets'],
               'recipeSnapshot': [{'file': name, 'size': len(data), 'sha256': digest_bytes(data)} for name, data in sorted(snapshot.items())]}
    if supplemental_path:
        binding['supplementalSourceManifestSha256'] = digest_file(supplemental_path)
    if runtime_evidence:
        binding['runtimeReview'] = {
            'manifestSha256': runtime_evidence['manifestSha256'],
            'scope': 'Post-build compiler-runtime source/license evidence; original build lock and inputs unchanged',
            'files': runtime_evidence['manifest']['files'],
        }
    rebuild = f'''# Corresponding source for DuskCut FFmpeg {build_id}

This package binds FFmpeg source inputs, the executed build and the exact public
build scripts to the executable hashes recorded in evidence/BUILD-RESULT.json.
SOURCE-BINDING.json provides hashes and recipe commit {revision}.
The complete original source archives and notices, including any immutable
supplemental source parts, are included in inputs/*.tar; they are not external
download links. A supplemental manifest, when present, is separately hash-bound
by the executed build lock, input-release manifest and SOURCE-BINDING.json.
The executed recipe/configuration/patch
records are included in evidence/ffmpeg-build-evidence.tar.xz.

To inspect and rebuild on a Linux Docker host with Node.js 24 and Python 3.12:

1. Extract evidence/ffmpeg-build-evidence.tar.xz into a new evidence-expanded/
   directory. Inspect its BUILD-RESULT.json, inputs/build-lock.json and
   work/configuration/; compare the SHA-256 values with SOURCE-BINDING.json.
2. Enter recipe/ and verify/extract the original source parts locally:
   `python3 scripts/extract-inputs.py ../inputs/release-assets.json ../inputs input-data`
3. Copy `../evidence-expanded/inputs/build-lock.json` to `input-data/build-lock.json`.
   This preserves the exact original source and build-profile choices.
4. Read `toolchain.image` from that lock and pull that exact digest-pinned image.
   The source inputs already include the toolchain recipe and runtime notices.
5. Run `node build/controlled-build.mjs input-data/build-lock.json out`.
   Dependency and FFmpeg compilation execute with network access disabled.

The compiler container is a separately obtainable general build tool; this archive
includes its pinned identity, the crosstool-ng recipe and the documented applicable
runtime license/source treatment. This does not promise byte-for-byte identical
outputs on arbitrary hosts. Upstream component licenses govern modification and
redistribution; original notices remain in the complete source archives.

Use this package with the specified DuskCut build only. It does not establish
source correspondence for older Gyan executables or unchanged BtbN binaries.
The assembler does not perform legal, runtime-license, GPU or functional approval.
'''.encode()
    source_path = output / source_name
    with tarfile.open(source_path, 'w', format=tarfile.PAX_FORMAT) as archive:
        add_tar_bytes(archive, 'README.md', rebuild)
        add_tar_bytes(archive, 'SOURCE-BINDING.json', json_bytes(binding))
        add_tar_file(archive, 'inputs/release-assets.json', release_path)
        for part in parts:
            add_tar_file(archive, 'inputs/' + part.name, part)
        add_tar_file(archive, 'evidence/ffmpeg-build-evidence.tar.xz', evidence_path)
        for name in ('BUILD-RESULT.json', 'BUILD-RECIPE-COMMIT.txt', 'BUILD-RECIPE-STATUS.txt'):
            add_tar_file(archive, 'evidence/' + name, regular(artifact, name))
        for name, data in sorted(snapshot.items()):
            add_tar_bytes(archive, 'recipe/' + name, data)
        if runtime_evidence:
            for name, path in sorted(runtime_evidence['paths'].items()):
                add_tar_file(archive, 'runtime-review/' + name, path)
    if source_path.stat().st_size > MAX_ASSET_BYTES:
        raise ValueError('Corresponding source exceeds 2 GiB; asset must not be uploaded')
    url = f'{REPOSITORY}/releases/download/{build_id}/'
    source_identity = identity(source_path)
    # Release-specific runtime instructions refer to the complete, already assembled
    # archive. Original input notices and TAR members remain byte-for-byte unchanged.
    runtime_readme = f'''# DuskCut FFmpeg {build_id}

This Windows runtime contains bin/ffmpeg.exe and bin/ffprobe.exe, plus license
notices and source inventories. The observed version token supplied during
assembly is {version}. Assembly alone is not approval for distribution.

The complete corresponding-source package for this build is:
{url + source_name}
SHA-256: {source_identity['sha256']}
Size: {source_identity['size']} bytes

BUILD-RESULT.json is not inside this runtime ZIP. It is at
evidence/BUILD-RESULT.json inside the corresponding-source archive above,
alongside evidence/ffmpeg-build-evidence.tar.xz and SOURCE-BINDING.json.
These records bind the original executable hashes to the executed build recipe
and its source inputs. See SOURCES.md for verification and rebuild locations.

Read LICENSE, THIRD-PARTY-NOTICES.txt and any additional notices under sources/.
The source URL becomes publicly accessible when the reviewed release is published;
do not distribute this candidate while the corresponding source is unavailable.
'''.encode()
    runtime_sources = f'''# Corresponding source for DuskCut FFmpeg {build_id}

Download the complete source archive for this exact runtime:
{url + source_name}

Expected SHA-256: {source_identity['sha256']}
Expected size: {source_identity['size']} bytes
FFmpeg revision: {result['ffmpegRevision']}
Public build-recipe commit: {revision}

Verify that archive against the SHA-256 and size above, then extract it into
a new directory. Its README.md explains the offline-input reconstruction steps.
The archive contains:

- evidence/BUILD-RESULT.json: original executable hashes and build identity.
- evidence/ffmpeg-build-evidence.tar.xz: executed lock, configuration and logs.
- SOURCE-BINDING.json: recipe, source inventories and evidence hashes.
- recipe/: the exact public build scripts recorded for this build.
- inputs/release-assets.json: hashes and names of every original input part,
  including supplemental parts when used. Verify input parts against this file,
  not an older base-only checksum list.
- inputs/*.tar: the actual original source archives, patches and license notices.

The runtime source inventories under sources/ are summaries, not replacements
for the complete archive. Supplemental source and license files are separately
bound by the executed lock and SOURCE-BINDING.json. Original input documents
are retained unchanged inside their input parts; this file describes this final
runtime release. The package does not establish source correspondence for older
Gyan releases or unrelated FFmpeg binaries.
'''.encode()
    runtime = {**binaries}
    for name in DOCS:
        runtime[name] = regular(inputs, 'sources/distribution/' + name)
    runtime['README.md'] = runtime_readme
    runtime['SOURCES.md'] = runtime_sources
    if runtime_evidence:
        note = b'''

Post-build compiler-runtime notice: libatomic (GCC) is linked into this build.
The original license, GCC Runtime Library Exception and source-header evidence
are retained under sources/runtime-review/, bound by runtime-review.json to the
original build, compiler image and binary hashes. The complete corresponding
source archive contains the same post-build review in runtime-review/.
This adds documentation; it does not alter the original source lock or binaries.
'''
        runtime['THIRD-PARTY-NOTICES.txt'] = runtime['THIRD-PARTY-NOTICES.txt'].read_bytes() + note
        runtime['SOURCES.md'] += note
        for name, path in runtime_evidence['paths'].items():
            runtime['sources/runtime-review/' + name] = path
    runtime['sources/source-manifest.json'] = manifest_path
    if supplemental_path:
        runtime['sources/supplemental-manifest.json'] = supplemental_path
        for row in supplement['files'] + supplement['notices']:
            if row['role'] in ('license-evidence', 'review-evidence'):
                runtime[row['file']] = regular(inputs, row['file'])
    runtime_path = output / runtime_name
    inventory = []
    with zipfile.ZipFile(runtime_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=False) as archive:
        for name, content in sorted(runtime.items()):
            if isinstance(content, bytes):
                archive.writestr(name, content)
                entry_identity = {'size': len(content), 'sha256': digest_bytes(content)}
            else:
                archive.write(content, arcname=name)
                entry_identity = identity(content)
            inventory.append({'archivePath': name, 'path': name, **entry_identity})
    if runtime_path.stat().st_size > MAX_RUNTIME_BYTES:
        raise ValueError('Runtime ZIP exceeds application limit; asset must not be uploaded')
    with zipfile.ZipFile(runtime_path) as archive:
        if sorted(archive.namelist()) != sorted(runtime) or archive.testzip() is not None:
            raise ValueError('Generated runtime ZIP failed verification')
        for row in inventory:
            with archive.open(row['path']) as stream:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(chunk)
            if hasher.hexdigest() != row['sha256']:
                raise ValueError('Generated ZIP file hash mismatch')
    candidate = {'schemaVersion': 1, 'approvalStatus': 'candidate-awaiting-review', 'buildId': build_id,
                 'version': version, 'ffmpegCommit': result['ffmpegRevision'], 'buildRecipeCommit': revision,
                 'archive': {'url': url + runtime_name, **identity(runtime_path)},
                 'source': {'url': url + source_name, **source_identity}, 'files': inventory}
    (output / 'ffmpeg-release.candidate.json').write_bytes(json_bytes(candidate))
    (output / 'SOURCE-BINDING.json').write_bytes(json_bytes(binding))
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{digest_file(output / name)}  {name}\n' for name in
        (runtime_name, source_name, 'ffmpeg-release.candidate.json', 'SOURCE-BINDING.json')), encoding='utf-8')
    return candidate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', required=True)
    parser.add_argument('--source-staging', required=True)
    parser.add_argument('--repo-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--version', required=True, help='Exact ffmpeg/ffprobe -version token observed on Windows')
    parser.add_argument('--runtime-review', help='Bound post-build runtime source/license evidence directory; required when libatomic is linked')
    args = parser.parse_args()
    assembled = package(args.artifact_dir, args.source_staging, args.repo_dir, args.output, args.version, args.runtime_review)
    print(json.dumps({'status': assembled['approvalStatus'], 'runtime': assembled['archive'], 'source': assembled['source']}))
