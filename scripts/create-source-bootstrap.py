"""Describe an existing source tar byte-for-byte, without re-encoding its headers.

This publisher-side helper emits a small support ZIP. Large verified source
payloads are fetched separately by the public workflow before reconstruction.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import tarfile
import zipfile


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def safe_name(name):
    if (not isinstance(name, str) or name.startswith('/') or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(ord(char) < 32 for char in name)):
        raise ValueError('Unsafe bootstrap path')
    return name


def reconstructed_identity(staging, blocks):
    staging = pathlib.Path(staging).resolve()
    combined = hashlib.sha256()
    size = 0
    for block in blocks:
        if set(block) == {'literalBase64'}:
            data = base64.b64decode(block['literalBase64'], validate=True)
            combined.update(data)
            size += len(data)
        elif set(block) == {'file', 'bytes', 'sha256'}:
            path = staging / safe_name(block['file'])
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(staging):
                raise ValueError('Unsafe reconstruction payload')
            individual = hashlib.sha256()
            count = 0
            with path.open('rb') as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b''):
                    combined.update(data)
                    individual.update(data)
                    size += len(data)
                    count += len(data)
            if count != block['bytes'] or individual.hexdigest() != block['sha256']:
                raise ValueError('Reconstruction payload mismatch')
        else:
            raise ValueError('Unknown reconstruction block')
    return {'bytes': size, 'sha256': combined.hexdigest()}


def create(staging, output):
    staging, output = pathlib.Path(staging).resolve(), pathlib.Path(output).resolve()
    if output.exists():
        raise ValueError('Use a new bootstrap output directory')
    assets = json.loads((staging / 'release-assets.json').read_text())
    manifest = json.loads((staging / 'source-manifest.json').read_text())
    if len(assets['assets']) != 1:
        raise ValueError('This helper expects one reviewed source tar')
    asset = assets['assets'][0]
    original = staging / safe_name(asset['file'])
    if (original.stat().st_size != asset['bytes'] or digest(original) != asset['sha256']
            or digest(staging / 'source-manifest.json') != assets['sourceManifestSha256']):
        raise ValueError('Reviewed source asset identity mismatch')
    external = []
    for row in manifest['files']:
        if row['role'] in ('dependency-source', 'ffmpeg-source', 'recipe', 'toolchain-recipe-source'):
            external.append({'file': 'sources/' + row['file'], 'bytes': row['bytes'], 'sha256': row['sha256'],
                             'origin': row['origin'], 'role': row['role']})
    if (len([row for row in external if row['role'] == 'dependency-source']) != 41
            or len(external) != 44):
        raise ValueError('Unexpected external source input selection')
    external_map = {row['file']: row for row in external}
    recipe = {'schemaVersion': 1, 'output': {'file': original.name, 'bytes': asset['bytes'], 'sha256': asset['sha256']},
              'blocks': [], 'externalFiles': external,
              'cacheArtifact': {'id': 10931492582, 'bytes': 2191426035,
                                'sha256': 'fa2f9f26e34789bdac3563fc57872e5e2dc88970060364c502199edecf626055',
                                'origin': 'https://api.github.com/repos/BtbN/FFmpeg-Builds/actions/artifacts/10931492582/zip'}}
    small_files = []
    seen = set()
    offset = 0
    total_literal = 0
    with original.open('rb') as raw, tarfile.open(original, 'r:') as archive:
        for member in archive.getmembers():
            name = safe_name(member.name)
            if not member.isfile() or name.casefold() in seen:
                raise ValueError('Original source TAR has unexpected types or duplicate entries')
            seen.add(name.casefold())
            if member.offset_data < offset:
                raise ValueError('Overlapping source TAR members')
            literal_size = member.offset_data - offset
            if literal_size > 1024 * 1024:
                raise ValueError('Unexpectedly large TAR header/padding gap')
            raw.seek(offset)
            literal = raw.read(literal_size)
            if literal:
                recipe['blocks'].append({'literalBase64': base64.b64encode(literal).decode('ascii')})
                total_literal += len(literal)
            file = staging / name
            if (file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(staging)
                    or file.stat().st_size != member.size):
                raise ValueError('Missing or unsafe source payload: ' + name)
            file_hash = digest(file)
            raw.seek(member.offset_data)
            tar_hash = hashlib.sha256()
            remaining = member.size
            while remaining:
                chunk = raw.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('Truncated original source TAR')
                tar_hash.update(chunk)
                remaining -= len(chunk)
            if tar_hash.hexdigest() != file_hash:
                raise ValueError('Original TAR differs from staging payload')
            recipe['blocks'].append({'file': name, 'bytes': member.size, 'sha256': file_hash})
            if name in external_map:
                row = external_map[name]
                if row['bytes'] != member.size or row['sha256'] != file_hash:
                    raise ValueError('External payload differs from pinned manifest')
            else:
                if member.size > 8 * 1024 ** 2:
                    raise ValueError('Unexpectedly large support payload')
                small_files.append((name, file))
            offset = member.offset_data + member.size
        raw.seek(offset)
        trailer = raw.read()
        if len(trailer) > 1024 * 1024:
            raise ValueError('Unexpected TAR trailer')
        if trailer:
            recipe['blocks'].append({'literalBase64': base64.b64encode(trailer).decode('ascii')})
            total_literal += len(trailer)
    if not all(name.casefold() in seen for name in external_map):
        raise ValueError('Original TAR missing a required source payload')
    reconstructed = reconstructed_identity(staging, recipe['blocks'])
    if reconstructed != {'bytes': asset['bytes'], 'sha256': asset['sha256']}:
        raise ValueError('Sparse recipe does not reconstruct the exact original TAR')
    output.mkdir(parents=True, exist_ok=False)
    recipe_bytes = (json.dumps(recipe, indent=2) + '\n').encode()
    (output / 'source-reconstruction.json').write_bytes(recipe_bytes)
    support = output / 'source-bootstrap-support.zip'
    with zipfile.ZipFile(support, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr('source-reconstruction.json', recipe_bytes)
        for name, file in small_files:
            archive.write(file, name)
    if support.stat().st_size >= 2 * 1024 ** 2:
        raise ValueError('Bootstrap support exceeds requested 2 MiB limit')
    summary = {'schemaVersion': 1, 'file': support.name, 'bytes': support.stat().st_size, 'sha256': digest(support),
               'reconstructedAsset': recipe['output'], 'smallFiles': len(small_files), 'externalFiles': len(external),
               'literalBytes': total_literal, 'blocks': len(recipe['blocks'])}
    (output / 'bootstrap-support.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary))
    return recipe, summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staging', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    create(args.staging, args.output)
