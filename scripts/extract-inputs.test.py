"""Small real tar fixtures exercise source integrity and extraction boundaries."""
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import tarfile
import tempfile
import unittest

MODULE = pathlib.Path(__file__).with_name('extract-inputs.py')
SPEC = importlib.util.spec_from_file_location('extract_inputs', MODULE)
EXTRACTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACTOR)


class SourceExtractionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='duskcut-source-extract-')
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.output = self.root / 'new-inputs'
        self.manifest = {
            'schemaVersion': 1, 'extractTo': '.',
            'releaseTag': 'sources-9.0.2-duskcut.1', 'assets': [],
        }

    def part(self, entries, name=None):
        name = name or f'part-{len(self.manifest["assets"])}.tar'
        path = self.downloads / name
        with tarfile.open(path, 'w') as archive:
            for name, payload, kind in entries:
                item = tarfile.TarInfo(name)
                item.type = kind
                if kind == tarfile.REGTYPE:
                    item.size = len(payload)
                    archive.addfile(item, io.BytesIO(payload))
                else:
                    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                        item.linkname = '../escape.txt'
                    archive.addfile(item)
        data = path.read_bytes()
        self.manifest['assets'].append({
            'file': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'url': ('https://github.com/pascalghanimi/duskcut-ffmpeg/releases/download/'
                    + self.manifest['releaseTag'] + '/' + path.name),
        })
        return path

    def extract(self):
        manifest = self.root / 'release-assets.json'
        manifest.write_text(json.dumps(self.manifest), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            EXTRACTOR.extract(manifest, self.downloads, self.output)

    def good(self):
        return self.part([
            ('source-manifest.json', b'{"sources": []}', tarfile.REGTYPE),
            ('sources/', b'', tarfile.DIRTYPE),
            ('sources/archives/source.tar.xz', b'fixed source archive', tarfile.REGTYPE),
        ])

    def test_regular_sources_from_multiple_verified_parts(self):
        self.good()
        self.part([
            ('sources/', b'', tarfile.DIRTYPE),
            ('sources/patches/fix.patch', b'patch bytes', tarfile.REGTYPE),
        ])
        self.extract()
        self.assertEqual((self.output / 'sources/archives/source.tar.xz').read_bytes(), b'fixed source archive')
        self.assertEqual((self.output / 'sources/patches/fix.patch').read_bytes(), b'patch bytes')

    def test_existing_destination_is_never_overwritten(self):
        self.good()
        self.output.mkdir()
        sentinel = self.output / 'original.txt'
        sentinel.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.extract()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_modified_archive_rejected_before_destination_is_created(self):
        archive = self.good()
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        archive.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            self.extract()
        self.assertFalse(self.output.exists())

    def test_size_mismatch_rejected_before_destination_is_created(self):
        self.good()
        self.manifest['assets'][0]['bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'size/type mismatch'):
            self.extract()
        self.assertFalse(self.output.exists())

    def test_unsafe_paths_rejected(self):
        for path in ['../escape.txt', '/sources/absolute.txt', 'sources/../escape.txt',
                     'C:/escape.txt', 'sources\\escape.txt', 'unexpected.txt',
                     'source-manifest.json/hidden.txt', 'sources/NUL.txt',
                     'sources/file.', 'sources/file ', 'sources/file:stream']:
            with self.subTest(path=path):
                self.manifest['assets'] = []
                self.part([(path, b'bad', tarfile.REGTYPE)])
                self.output = self.root / f'bad-output-{len(list(self.root.iterdir()))}'
                with self.assertRaisesRegex(ValueError, 'Unsafe|Unexpected'):
                    self.extract()
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_links_devices_and_fifos_are_rejected(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]:
            with self.subTest(kind=kind):
                self.manifest['assets'] = []
                self.part([('sources/special', b'', kind)])
                self.output = self.root / f'special-output-{len(list(self.root.iterdir()))}'
                with self.assertRaisesRegex(ValueError, 'only regular files'):
                    self.extract()

    def test_duplicate_file_across_parts_is_rejected(self):
        self.good()
        self.part([('sources/archives/source.tar.xz', b'replacement', tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.extract()
        self.assertEqual((self.output / 'sources/archives/source.tar.xz').read_bytes(), b'fixed source archive')

    def test_windows_case_collision_is_rejected(self):
        self.good()
        self.part([('sources/ARCHIVES/another.tar.xz', b'other', tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, 'Duplicate or conflicting'):
            self.extract()

    def test_file_cannot_become_parent_directory(self):
        self.good()
        self.part([('sources/archives/source.tar.xz/child', b'bad', tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, 'Duplicate or conflicting'):
            self.extract()

    def test_source_manifest_is_required(self):
        self.part([('sources/data.txt', b'data', tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, 'Source manifest missing'):
            self.extract()

    def test_unpinned_or_escaping_asset_manifest_rejected(self):
        self.good()
        original = dict(self.manifest['assets'][0])
        for key, value in [('file', '../part-0.tar'), ('file', 'CON.tar'), ('sha256', 'unverified'),
                           ('url', original['url'] + '?private=token'), ('bytes', True)]:
            with self.subTest(key=key, value=value):
                self.manifest['assets'][0] = {**original, key: value}
                with self.assertRaises(ValueError):
                    self.extract()
                self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
