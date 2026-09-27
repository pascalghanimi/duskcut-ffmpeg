"""Small real Git/tar/ZIP fixtures verify release binding and rejection paths."""
import importlib.util
import io
import json
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('package_release', pathlib.Path(__file__).with_name('package-release.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Fixture:
    def __init__(self, root):
        self.root = root
        self.repo, self.inputs, self.artifact = [root / name for name in ('recipe', 'inputs', 'artifact')]
        for path in (self.repo, self.inputs, self.artifact):
            path.mkdir()
        profile = module.json_bytes({'id': 'test-no-dvd'})
        self.controls = {name: profile if name == 'profile.json' else ('# fixture ' + name + '\n').encode()
                         for name in module.CONTROLS}
        source = b'Pinned example source contents\n'
        manifest = {'schemaVersion': 1, 'recipeRevision': 'a' * 40, 'ffmpegRevision': 'b' * 40,
                    'files': [{'id': 'example-source', 'file': 'downloads/source.tar.xz', 'bytes': len(source),
                               'sha256': module.digest_bytes(source)}], 'notices': []}
        manifest_bytes = module.json_bytes(manifest)
        self.write(self.inputs, 'source-manifest.json', manifest_bytes)
        self.write(self.inputs, 'sources/source-manifest.json', manifest_bytes)
        self.write(self.inputs, 'sources/README.md', b'Source instructions\n')
        self.write(self.inputs, 'sources/downloads/source.tar.xz', source)
        for name in module.DOCS:
            self.write(self.inputs, 'sources/distribution/' + name, ('Fixture ' + name + '\n').encode())
        source_part = self.inputs / 'inputs-part01.tar'
        with tarfile.open(source_part, 'w') as archive:
            for path in sorted(self.inputs.rglob('*')):
                if path.is_file() and path != source_part:
                    module.add_tar_file(archive, path.relative_to(self.inputs).as_posix(), path)
        release = {'schemaVersion': 1, 'sourceManifestSha256': module.digest_bytes(manifest_bytes),
                   'releaseTag': 'sources-test.1', 'extractTo': '.',
                   'assets': [{'file': source_part.name, 'bytes': source_part.stat().st_size, 'sha256': module.digest_file(source_part)}]}
        release_bytes = module.json_bytes(release)
        self.write(self.inputs, 'release-assets.json', release_bytes)
        for name in ('README.md', 'LICENSE', 'build/controlled-build.mjs', 'build/create-lock.mjs', 'scripts/extract-inputs.py'):
            self.write(self.repo, name, ('# fixture ' + name + '\n').encode())
        self.write(self.repo, 'release-assets.json', release_bytes)
        for name, data in self.controls.items():
            self.write(self.repo, 'build/' + name, data)
        self.git('init', '--quiet')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'fixture')
        self.revision = self.git('rev-parse', 'HEAD').decode().strip()
        self.write(self.artifact, 'BUILD-RECIPE-COMMIT.txt', (self.revision + '\n').encode())
        self.write(self.artifact, 'BUILD-RECIPE-STATUS.txt', b'')
        binaries = {}
        for name in ('ffmpeg.exe', 'ffprobe.exe'):
            data = b'MZ' + name.encode() + b'\x00' * 128
            self.write(self.artifact, 'work/binary/bin/' + name, data)
            binaries[name] = module.digest_bytes(data)
        blob = {**manifest['files'][0], 'file': 'sources/' + manifest['files'][0]['file'], 'role': 'source-cache'}
        lock = {'schemaVersion': 2, 'buildId': 'test.1', 'sourceManifest': {'sha256': module.digest_bytes(manifest_bytes)},
                'profile': {'sha256': module.digest_bytes(profile)}, 'blobs': [blob]}
        lock_bytes = module.json_bytes(lock)
        self.result = {'schemaVersion': 1, 'status': 'built-not-approved-for-distribution',
                       'ffmpegRevision': 'b' * 40, 'recipeRevision': 'a' * 40, 'buildId': 'test.1',
                       'lockSha256': module.digest_bytes(lock_bytes), 'binaryHashes': binaries, 'sourceInputs': [blob]}
        result_bytes = module.json_bytes(self.result)
        self.write(self.artifact, 'BUILD-RESULT.json', result_bytes)
        self.evidence_files = {'BUILD-RESULT.json': result_bytes, 'inputs/build-lock.json': lock_bytes,
                               'work/control/lock.json': lock_bytes,
                               **{'work/control/' + name: data for name, data in self.controls.items()}}
        self.save_evidence()

    @staticmethod
    def write(root, name, data):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.STDOUT)

    def save_evidence(self):
        path = self.artifact / 'ffmpeg-build-evidence.tar.xz'
        with tarfile.open(path, 'w:xz') as archive:
            for name, data in self.evidence_files.items():
                module.add_tar_bytes(archive, name, data)
        self.write(self.artifact, 'SHA256SUMS.txt', (module.digest_file(path) + '  ' + path.name + '\n').encode())

    def package(self):
        return module.package(self.artifact, self.inputs, self.repo, self.root / 'result', '9.0.2-test.1')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='duskcut-ffmpeg-package-test-')
        self.fixture = Fixture(pathlib.Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_package_binds_hashes_and_has_only_runtime_files(self):
        fixture = self.fixture
        fixture.write(fixture.repo, '.env', b'private untracked fixture data')
        candidate = fixture.package()
        self.assertEqual(candidate['approvalStatus'], 'candidate-awaiting-review')
        out = fixture.root / 'result'
        with zipfile.ZipFile(out / 'duskcut-ffmpeg-test.1-win64.zip') as archive:
            self.assertEqual(set(archive.namelist()), {'bin/ffmpeg.exe', 'bin/ffprobe.exe', *module.DOCS, 'sources/source-manifest.json'})
        with tarfile.open(out / 'duskcut-ffmpeg-test.1-corresponding-source.tar') as archive:
            members = module.archive_files(archive)
            self.assertIn('inputs/inputs-part01.tar', members)
            self.assertIn('evidence/ffmpeg-build-evidence.tar.xz', members)
            self.assertIn('recipe/build/container-compile.sh', members)
            self.assertFalse(any('/.git/' in name or name.endswith('.env') for name in members))
        self.assertEqual(candidate['source']['sha256'], module.digest_file(out / 'duskcut-ffmpeg-test.1-corresponding-source.tar'))

    def test_changed_executable_rejected_before_output(self):
        fixture = self.fixture
        fixture.write(fixture.artifact, 'work/binary/bin/ffmpeg.exe', b'MZ changed')
        with self.assertRaisesRegex(ValueError, 'Binary does not match'):
            fixture.package()
        self.assertFalse((fixture.root / 'result').exists())

    def test_changed_executed_control_rejected(self):
        fixture = self.fixture
        fixture.evidence_files['work/control/container-compile.sh'] = b'# different command\n'
        fixture.save_evidence()
        with self.assertRaisesRegex(ValueError, 'Executed build control differs'):
            fixture.package()

    def test_changed_staged_source_rejected(self):
        fixture = self.fixture
        fixture.write(fixture.inputs, 'sources/downloads/source.tar.xz', b'changed source')
        with self.assertRaisesRegex(ValueError, 'size or SHA-256 mismatch'):
            fixture.package()

    def test_changed_runtime_notice_rejected_against_original_part(self):
        fixture = self.fixture
        fixture.write(fixture.inputs, 'sources/distribution/README.md', b'Changed notice')
        with self.assertRaisesRegex(ValueError, 'Staged source differs'):
            fixture.package()

    def test_dirty_build_record_or_existing_output_rejected(self):
        fixture = self.fixture
        fixture.write(fixture.artifact, 'BUILD-RECIPE-STATUS.txt', b' M build/profile.json\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            fixture.package()
        (fixture.root / 'result').mkdir()
        with self.assertRaisesRegex(ValueError, 'must be new'):
            fixture.package()

    def test_archive_traversal_and_links_rejected(self):
        for name, kind in (('../outside', tarfile.REGTYPE), ('safe', tarfile.SYMTYPE)):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w') as archive:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.linkname = '/outside' if kind == tarfile.SYMTYPE else ''
                archive.addfile(item)
            data.seek(0)
            with tarfile.open(fileobj=data, mode='r') as archive:
                with self.assertRaises(ValueError):
                    module.archive_files(archive)


if __name__ == '__main__':
    unittest.main()
