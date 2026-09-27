"""Verify and unpack pinned public source parts, never into an existing checkout."""
import hashlib
import json
import pathlib
import re
import sys
import tarfile


def safe_windows_name(part):
    return not (
        part.endswith(('.', ' '))
        or any(ord(char) < 32 or char in '<>:"\\|?*' for char in part)
        or re.match(r'^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', part, re.I)
    )


def verified_archives(manifest, downloads):
    if (manifest.get('schemaVersion') != 1 or manifest.get('extractTo') != '.'
            or not re.fullmatch(r'sources-[a-z0-9.-]+', manifest.get('releaseTag', ''))):
        raise ValueError('Unsupported source asset manifest')
    assets = manifest.get('assets')
    if not isinstance(assets, list) or not assets:
        raise ValueError('No source assets')
    names = set()
    verified = []
    for asset in assets:
        filename = asset.get('file', '')
        if (not re.fullmatch(r'[a-zA-Z0-9_.-]+\.tar(?:\.gz|\.xz)?', filename)
                or not safe_windows_name(filename) or filename.casefold() in names):
            raise ValueError('Unsafe or duplicate archive filename')
        names.add(filename.casefold())
        size, digest = asset.get('bytes'), asset.get('sha256', '')
        if type(size) is not int or not 0 < size < 2 ** 31 or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Source asset not size/hash pinned')
        expected_url = ('https://github.com/pascalghanimi/duskcut-ffmpeg/releases/download/'
                        + manifest['releaseTag'] + '/' + filename)
        if asset.get('url') != expected_url:
            raise ValueError('Source asset URL must match the fixed public release')
        path = pathlib.Path(downloads) / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise ValueError('Source archive size/type mismatch: ' + filename)
        hasher = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError('Source archive SHA-256 mismatch: ' + filename)
        verified.append(path)
    return verified


def extract(manifest_file, downloads, destination):
    manifest = json.loads(pathlib.Path(manifest_file).read_text(encoding='utf-8'))
    # This script is independently safe to invoke; never assume a preceding download step ran.
    archives = verified_archives(manifest, downloads)
    root = pathlib.Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    names = {}
    for archive_path in archives:
        with tarfile.open(archive_path, 'r:*') as archive:
            for member in archive.getmembers():
                path = pathlib.PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts or ':' in member.name or '\\' in member.name
                        or any(not safe_windows_name(part) for part in path.parts)):
                    raise ValueError('Unsafe source path')
                if not path.parts or (path.parts[0] != 'sources' and str(path) != 'source-manifest.json'):
                    raise ValueError('Unexpected source input location: ' + member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Source parts must contain only regular files and directories')
                # Track implicit parents as well, so a later file or differently-cased directory
                # cannot reinterpret a path that was already created on Windows.
                for length in range(1, len(path.parts) + 1):
                    canonical = '/'.join(path.parts[:length])
                    kind = 'file' if length == len(path.parts) and member.isfile() else 'directory'
                    previous = names.get(canonical.casefold())
                    if previous and (previous != (canonical, kind) or kind == 'file'):
                        raise ValueError('Duplicate or conflicting source input: ' + canonical)
                    names[canonical.casefold()] = (canonical, kind)
            archive.extractall(root, filter='data')
    if not (root / 'source-manifest.json').is_file():
        raise ValueError('Source manifest missing')
    print('Source inputs extracted into new isolated directory')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Usage: extract-inputs.py release-assets.json downloads new-input-directory')
    extract(*sys.argv[1:])
