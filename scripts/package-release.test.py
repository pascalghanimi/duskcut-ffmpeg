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
                   'assets': [{'file': source_part.name, 'bytes': source_part.stat().st_size,
                               'sha256': module.digest_file(source_part),
                               'url': module.REPOSITORY + '/releases/download/sources-test.1/' + source_part.name}]}
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
        self.evidence_files.update({'work/configuration/' + name: ('Fixture evidence ' + name + '\n').encode()
                                    for name in module.CONFIGURATION})
        self.evidence_files.update({name: ('Fixture evidence ' + name + '\n').encode()
                                    for name in module.BUILD_RECORDS})
        self.evidence_files['work/configuration/profile.json'] = profile
        self.evidence_files['work/compile-result.json'] = module.json_bytes({
            'buildId': 'test.1', 'ffmpegRevision': 'b' * 40, 'recipeRevision': 'a' * 40,
        })
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

    def package(self, runtime_review=None):
        return module.package(self.artifact, self.inputs, self.repo, self.root / 'result', '9.0.2-test.1', runtime_review)

    def update_lock(self, lock):
        data = module.json_bytes(lock)
        self.evidence_files['inputs/build-lock.json'] = data
        self.evidence_files['work/control/lock.json'] = data
        self.result['lockSha256'] = module.digest_bytes(data)
        self.result['sourceInputs'] = lock['blobs']
        data = module.json_bytes(self.result)
        self.evidence_files['BUILD-RESULT.json'] = data
        self.write(self.artifact, 'BUILD-RESULT.json', data)
        self.save_evidence()

    def add_supplement(self):
        rows = []
        revision = 'c' * 40
        for name, role, path, data in [
            ('freetype-dlg', 'source', 'sources/supplemental/dlg.tar.gz', b'Fixture submodule source'),
            ('freetype-dlg-license', 'license-evidence', 'sources/notices/freetype-dlg/LICENSE', b'Fixture original license'),
            ('freetype-dlg-review', 'review-evidence', 'sources/supplemental/README.md', b'Fixture supplement review'),
        ]:
            self.write(self.inputs, path, data)
            rows.append({'id': name, 'role': role, 'file': path, 'bytes': len(data),
                         'sha256': module.digest_bytes(data), 'revision': revision})
        supplement = {'schemaVersion': 1, 'freetypeDlg': {'id': 'freetype-dlg', 'revision': revision,
                        'parentRevision': 'd' * 40}, 'files': rows, 'notices': []}
        manifest_name = 'sources/supplemental-manifest.json'
        data = module.json_bytes(supplement)
        self.write(self.inputs, manifest_name, data)
        source_part = self.inputs / 'inputs-supplement.tar'
        with tarfile.open(source_part, 'w') as archive:
            for name in [manifest_name] + [row['file'] for row in rows]:
                module.add_tar_file(archive, name, self.inputs / name)
        release = module.read_json(self.inputs / 'release-assets.json')
        release['supplementalManifestSha256'] = module.digest_bytes(data)
        release['assets'].append({'file': source_part.name, 'bytes': source_part.stat().st_size,
                                 'sha256': module.digest_file(source_part),
                                 'url': module.REPOSITORY + '/releases/download/sources-test.1/' + source_part.name})
        self.write(self.inputs, 'release-assets.json', module.json_bytes(release))
        self.write(self.repo, 'release-assets.json', module.json_bytes(release))
        self.git('add', 'release-assets.json')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'supplement')
        self.revision = self.git('rev-parse', 'HEAD').decode().strip()
        self.write(self.artifact, 'BUILD-RECIPE-COMMIT.txt', (self.revision + '\n').encode())
        lock = json.loads(self.evidence_files['inputs/build-lock.json'])
        lock['supplementalSourceManifest'] = {'file': manifest_name, 'sha256': module.digest_bytes(data)}
        lock['freetypeDlg'] = {'blobId': 'freetype-dlg', 'noticeBlobId': 'freetype-dlg-license',
                              'revision': revision, 'parentRevision': 'd' * 40}
        lock['blobs'].extend(rows)
        self.update_lock(lock)
        return supplement

    def add_runtime_review(self, link_path=None, archive_path=None):
        root = self.root / 'runtime-review'
        root.mkdir()
        image = 'ghcr.io/example/compiler@sha256:' + '1' * 64
        image_id = 'sha256:' + '2' * 64
        compiler_version = 'example-mingw32-gcc (fixture compiler) 16.2.0'
        link_path = link_path or '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/libatomic.a'
        archive_path = archive_path or module.posixpath.normpath(link_path)
        self.evidence_files['work/ffmpeg-build.log'] = (link_path + '\n').encode()
        self.evidence_files['work/toolchain-image.json'] = module.json_bytes({'reference': image, 'Id': image_id})
        self.evidence_files['work/configuration/compiler-version.txt'] = (compiler_version + '\n').encode()
        lock = json.loads(self.evidence_files['inputs/build-lock.json'])
        lock['toolchain'] = {'image': image}
        self.update_lock(lock)
        audit = {'schemaVersion': 1, 'kind': 'post-build-toolchain-runtime-provenance',
            'build': {'repository': 'pascalghanimi/duskcut-ffmpeg', 'recipeCommit': self.revision,
                'lockSha256': self.result['lockSha256'], 'binaryHashes': self.result['binaryHashes'],
                'evidenceSha256': module.digest_file(self.artifact / 'ffmpeg-build-evidence.tar.xz')},
            'toolchain': {'image': image, 'imageId': image_id, 'compilerVersion': compiler_version},
            'archives': [{'name': 'libatomic.a', 'sha256': '3' * 64, 'bytes': 12345, 'path': archive_path,
                          'reportedPath': archive_path,
                          'linkTraceFiles': [{'recordedPath': link_path, 'resolvedPath': archive_path,
                                              'bytes': 12345, 'sha256': '3' * 64}],
                          'presentInBuildLinkTrace': True}]}
        files = [('GCC-COPYING3.txt', 'license', b'Fixture GPL3 license'),
                 ('GCC-RUNTIME-EXCEPTION-3.1.txt', 'license', b'Fixture runtime exception'),
                 ('GCC-libatomic_i.h.txt', 'source-header', b'Fixture source header'),
                 ('libatomic-toolchain-audit.json', 'linkage-evidence', module.json_bytes(audit)),
                 ('README.md', 'review', b'Post-build license review fixture only')]
        rows = []
        for name, role, data in files:
            self.write(root, name, data)
            rows.append({'id': name, 'file': name, 'role': role, 'bytes': len(data), 'sha256': module.digest_bytes(data)})
        manifest = {'schemaVersion': 1, 'buildId': self.result['buildId'], 'buildLockSha256': self.result['lockSha256'],
            'binaryHashes': self.result['binaryHashes'], 'compilerImage': image, 'files': rows,
            'libatomic': {'archiveSha256': '3' * 64, 'sourceRevision': '4' * 40, 'compilerVersion': '16.2.0',
                'linkageEvidenceFile': 'libatomic-toolchain-audit.json', 'licenseFile': 'GCC-COPYING3.txt',
                'exceptionFile': 'GCC-RUNTIME-EXCEPTION-3.1.txt', 'sourceHeaderFile': 'GCC-libatomic_i.h.txt'}}
        self.write(root, 'runtime-review.json', module.json_bytes(manifest))
        return root, manifest, audit


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

    def test_runtime_instructions_bind_complete_source_without_modifying_original_input_docs(self):
        fixture = self.fixture
        fixture.add_supplement()
        original_part_hashes = {path.name: module.digest_file(path) for path in fixture.inputs.glob('*.tar')}
        original_docs = {name: (fixture.inputs / 'sources/distribution' / name).read_bytes()
                         for name in ('README.md', 'SOURCES.md')}
        candidate = fixture.package()
        out = fixture.root / 'result'
        with zipfile.ZipFile(out / 'duskcut-ffmpeg-test.1-win64.zip') as archive:
            inventory = {row['path']: row for row in candidate['files']}
            self.assertNotIn('BUILD-RESULT.json', archive.namelist())
            for name in original_docs:
                data = archive.read(name)
                text = data.decode()
                self.assertIn(candidate['source']['url'], text)
                self.assertIn(candidate['source']['sha256'], text)
                self.assertIn(str(candidate['source']['size']), text)
                self.assertIn('evidence/BUILD-RESULT.json', text)
                self.assertEqual(inventory[name]['size'], len(data))
                self.assertEqual(inventory[name]['sha256'], module.digest_bytes(data))
                self.assertNotEqual(data, original_docs[name])
            self.assertIn('inputs/release-assets.json', archive.read('SOURCES.md').decode())
        for name, data in original_docs.items():
            self.assertEqual((fixture.inputs / 'sources/distribution' / name).read_bytes(), data)
        self.assertEqual(original_part_hashes,
                         {path.name: module.digest_file(path) for path in fixture.inputs.glob('*.tar')})
        with tarfile.open(out / 'duskcut-ffmpeg-test.1-corresponding-source.tar') as archive:
            for name, digest in original_part_hashes.items():
                self.assertEqual(module.digest_bytes(archive.extractfile('inputs/' + name).read()), digest)
        self.assertEqual(len(list(out.iterdir())), 5)

    def test_supplement_binds_build_and_replay_and_preserves_runtime_license(self):
        fixture = self.fixture
        fixture.add_supplement()
        candidate = fixture.package()
        names = {row['path'] for row in candidate['files']}
        self.assertIn('sources/notices/freetype-dlg/LICENSE', names)
        self.assertIn('sources/supplemental-manifest.json', names)
        self.assertIn('sources/supplemental/README.md', names)
        with tarfile.open(fixture.root / 'result/duskcut-ffmpeg-test.1-corresponding-source.tar') as archive:
            self.assertIn('inputs/inputs-supplement.tar', archive.getnames())
        binding = module.read_json(fixture.root / 'result/SOURCE-BINDING.json')
        self.assertEqual(binding['supplementalSourceManifestSha256'],
                         module.digest_file(fixture.inputs / 'sources/supplemental-manifest.json'))

    def test_unbound_changed_or_wrong_identity_supplement_rejected(self):
        fixture = self.fixture
        fixture.add_supplement()
        lock = json.loads(fixture.evidence_files['inputs/build-lock.json'])
        original = module.json_bytes(lock)
        lock['supplementalSourceManifest']['sha256'] = '0' * 64
        fixture.update_lock(lock)
        with self.assertRaisesRegex(ValueError, 'Supplemental source manifest hash'):
            fixture.package()
        lock = json.loads(original)
        lock['freetypeDlg']['revision'] = '0' * 40
        fixture.update_lock(lock)
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            fixture.package()
        lock = json.loads(original)
        del lock['supplementalSourceManifest']
        fixture.update_lock(lock)
        with self.assertRaisesRegex(ValueError, 'Unbound supplemental'):
            fixture.package()
        self.assertFalse((fixture.root / 'result').exists())

    def test_supplement_cannot_replace_base_source_or_runtime_notice(self):
        fixture = self.fixture
        supplement = fixture.add_supplement()
        base = module.read_json(fixture.inputs / 'source-manifest.json')
        for path in ['sources/downloads/source.tar.xz', 'sources/distribution/LICENSE',
                     'sources/README.md', 'sources/SUPPLEMENTAL-MANIFEST.json']:
            supplement['files'][0]['file'] = path
            data = module.json_bytes(supplement)
            fixture.write(fixture.inputs, 'sources/supplemental-manifest.json', data)
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'replacing or invalid'):
                    module.source_records(fixture.inputs, base, module.digest_bytes(data))

    def test_supplement_input_part_cannot_be_missing_or_duplicate_original_file(self):
        fixture = self.fixture
        fixture.add_supplement()
        release = module.read_json(fixture.inputs / 'release-assets.json')
        release['assets'].pop()
        fixture.write(fixture.inputs, 'release-assets.json', module.json_bytes(release))
        with self.assertRaisesRegex(ValueError, 'full manifest and notices'):
            module.verify_input_parts(fixture.inputs, fixture.inputs / 'source-manifest.json')

    def test_changed_executed_control_rejected(self):
        fixture = self.fixture
        fixture.evidence_files['work/control/container-compile.sh'] = b'# different command\n'
        fixture.save_evidence()
        with self.assertRaisesRegex(ValueError, 'Executed build control differs'):
            fixture.package()

    def test_missing_or_empty_configuration_and_build_records_rejected(self):
        fixture = self.fixture
        for name in tuple('work/configuration/' + item for item in module.CONFIGURATION) + module.BUILD_RECORDS:
            original = fixture.evidence_files.pop(name)
            with self.subTest(name=name):
                fixture.save_evidence()
                with self.assertRaisesRegex(ValueError, 'required build evidence'):
                    fixture.package()
                if name not in module.EMPTY_LOGS_ALLOWED:
                    fixture.evidence_files[name] = b''
                    fixture.save_evidence()
                    with self.assertRaisesRegex(ValueError, 'required build evidence'):
                        fixture.package()
            fixture.evidence_files[name] = original
        self.assertFalse((fixture.root / 'result').exists())

    def test_successful_generation_can_have_an_empty_but_present_log(self):
        fixture = self.fixture
        fixture.evidence_files['work/generate.log'] = b''
        fixture.save_evidence()
        self.assertEqual(fixture.package()['approvalStatus'], 'candidate-awaiting-review')

    def test_generated_profile_and_compile_identity_must_match(self):
        fixture = self.fixture
        original = fixture.evidence_files['work/configuration/profile.json']
        fixture.evidence_files['work/configuration/profile.json'] = b'{"id": "different"}\n'
        fixture.save_evidence()
        with self.assertRaisesRegex(ValueError, 'configuration profile differs'):
            fixture.package()
        fixture.evidence_files['work/configuration/profile.json'] = original
        fixture.evidence_files['work/compile-result.json'] = module.json_bytes({
            'buildId': 'different-build', 'ffmpegRevision': 'b' * 40, 'recipeRevision': 'a' * 40,
        })
        fixture.save_evidence()
        with self.assertRaisesRegex(ValueError, 'Compile-stage result differs'):
            fixture.package()

    def test_source_part_replay_metadata_is_required(self):
        fixture = self.fixture
        release = module.read_json(fixture.inputs / 'release-assets.json')
        original = module.json_bytes(release)
        for key, value in [('extractTo', 'elsewhere'), ('releaseTag', 'latest')]:
            with self.subTest(key=key):
                changed = json.loads(original)
                changed[key] = value
                fixture.write(fixture.inputs, 'release-assets.json', module.json_bytes(changed))
                with self.assertRaisesRegex(ValueError, 'cannot be replayed'):
                    module.verify_input_parts(fixture.inputs, fixture.inputs / 'source-manifest.json')
        for value in [None, 'https://example.invalid/source.tar', release['assets'][0]['url'] + '?secret=123']:
            with self.subTest(url=value):
                changed = json.loads(original)
                changed['assets'][0]['url'] = value
                fixture.write(fixture.inputs, 'release-assets.json', module.json_bytes(changed))
                with self.assertRaisesRegex(ValueError, 'Source asset URL'):
                    module.verify_input_parts(fixture.inputs, fixture.inputs / 'source-manifest.json')

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

    def test_linked_libatomic_requires_separate_bound_runtime_review(self):
        fixture = self.fixture
        fixture.evidence_files['work/ffmpeg-build.log'] = b'/opt/ct-ng/lib/libatomic.a\n'
        fixture.save_evidence()
        with self.assertRaisesRegex(ValueError, 'requires bound post-build'):
            fixture.package()
        self.assertFalse((fixture.root / 'result').exists())

    def test_post_build_runtime_review_is_separate_and_preserved_in_source_and_runtime(self):
        fixture = self.fixture
        root, manifest, _ = fixture.add_runtime_review()
        original_lock = fixture.evidence_files['inputs/build-lock.json']
        candidate = fixture.package(root)
        names = {row['path'] for row in candidate['files']}
        self.assertIn('sources/runtime-review/GCC-RUNTIME-EXCEPTION-3.1.txt', names)
        with zipfile.ZipFile(fixture.root / 'result/duskcut-ffmpeg-test.1-win64.zip') as archive:
            self.assertIn(b'libatomic', archive.read('THIRD-PARTY-NOTICES.txt'))
            self.assertEqual(archive.read('sources/runtime-review/runtime-review.json'), module.json_bytes(manifest))
        with tarfile.open(fixture.root / 'result/duskcut-ffmpeg-test.1-corresponding-source.tar') as archive:
            self.assertEqual(archive.extractfile('runtime-review/runtime-review.json').read(), module.json_bytes(manifest))
        binding = module.read_json(fixture.root / 'result/SOURCE-BINDING.json')
        self.assertEqual(binding['runtimeReview']['manifestSha256'], module.digest_bytes(module.json_bytes(manifest)))
        self.assertEqual(fixture.evidence_files['inputs/build-lock.json'], original_lock)

    def test_actual_gcc_dotdot_link_trace_matches_canonical_audited_archive(self):
        fixture = self.fixture
        actual_trace_path = '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libatomic.a'
        root, _, audit = fixture.add_runtime_review(actual_trace_path)
        self.assertEqual(audit['archives'][0]['path'], '/opt/ct-ng/x86_64-w64-mingw32/lib/libatomic.a')
        self.assertEqual(fixture.package(root)['approvalStatus'], 'candidate-awaiting-review')

    def test_actual_gcc_dotdot_trace_can_resolve_through_container_symlink(self):
        fixture = self.fixture
        actual_trace_path = '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libatomic.a'
        resolved = '/opt/ct-ng/x86_64-w64-mingw32/sysroot/lib/libatomic.a'
        root, _, audit = fixture.add_runtime_review(actual_trace_path, resolved)
        self.assertNotEqual(module.posixpath.normpath(actual_trace_path), resolved)
        self.assertEqual(audit['archives'][0]['linkTraceFiles'][0]['recordedPath'], actual_trace_path)
        self.assertEqual(fixture.package(root)['approvalStatus'], 'candidate-awaiting-review')

    def test_post_build_runtime_review_wrong_bindings_extra_files_or_changed_content_rejected(self):
        fixture = self.fixture
        root, manifest, _ = fixture.add_runtime_review()
        original = module.json_bytes(manifest)
        for key, value in [('buildId', 'wrong'), ('buildLockSha256', '0' * 64), ('binaryHashes', {}),
                           ('compilerImage', 'ghcr.io/example/other@sha256:' + '1' * 64)]:
            manifest = json.loads(original)
            manifest[key] = value
            fixture.write(root, 'runtime-review.json', module.json_bytes(manifest))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'not bound'):
                fixture.package(root)
        fixture.write(root, 'runtime-review.json', original)
        fixture.write(root, 'unexpected.txt', b'not approved')
        with self.assertRaisesRegex(ValueError, 'Unexpected file'):
            fixture.package(root)
        (root / 'unexpected.txt').unlink()
        fixture.write(root, 'GCC-COPYING3.txt', b'changed')
        with self.assertRaisesRegex(ValueError, 'size or SHA-256'):
            fixture.package(root)
        self.assertFalse((fixture.root / 'result').exists())

    def test_post_build_link_audit_must_match_original_archive_image_trace_and_executables(self):
        fixture = self.fixture
        root, manifest, audit = fixture.add_runtime_review()
        original = module.json_bytes(audit)
        changes = [('build', 'evidenceSha256', '0' * 64), ('build', 'binaryHashes', {}),
                   ('toolchain', 'imageId', 'sha256:' + '0' * 64),
                   ('archive', 'sha256', '0' * 64), ('archive', 'presentInBuildLinkTrace', False),
                   ('archive', 'path', '/opt/ct-ng/wrong/libatomic.a'), ('archive', 'linkTraceFiles', []),
                   ('trace', 'recordedPath', '/opt/ct-ng/unrecorded/libatomic.a'),
                   ('trace', 'resolvedPath', '/opt/ct-ng/other/libatomic.a'),
                   ('trace', 'bytes', 12346), ('trace', 'sha256', '0' * 64)]
        for group, key, value in changes:
            audit = json.loads(original)
            target = (audit['archives'][0]['linkTraceFiles'][0] if group == 'trace' else
                      audit['archives'][0] if group == 'archive' else audit[group])
            target[key] = value
            data = module.json_bytes(audit)
            fixture.write(root, 'libatomic-toolchain-audit.json', data)
            row = next(row for row in manifest['files'] if row['role'] == 'linkage-evidence')
            row.update(bytes=len(data), sha256=module.digest_bytes(data))
            fixture.write(root, 'runtime-review.json', module.json_bytes(manifest))
            with self.subTest(group=group, key=key), self.assertRaises(ValueError):
                fixture.package(root)

    def test_only_exact_audited_public_security_fixture_is_preserved_without_redaction(self):
        fixture = self.fixture
        name = 'build/capture_environment_test.py'
        data = (pathlib.Path(__file__).resolve().parents[1] / name).read_bytes().replace(b'\r\n', b'\n')
        self.assertEqual(module.digest_bytes(data), module.AUDITED_TEST_FIXTURES[name])
        self.assertIsNotNone(module.SECRET.search(data))
        fixture.write(fixture.repo, name, data)
        fixture.git('add', name)
        fixture.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'audited fixture')
        revision = fixture.git('rev-parse', 'HEAD').decode().strip()
        self.assertEqual(module.repository_snapshot(fixture.repo, revision)[name], data)
        for path, content in [(name, data + b'\n# changed\n'), ('build/other_test.py', data)]:
            fixture.write(fixture.repo, path, content)
            fixture.git('add', name, path)
            fixture.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--quiet', '-m', 'unapproved fixture variation')
            revision = fixture.git('rev-parse', 'HEAD').decode().strip()
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Unreviewed'):
                module.repository_snapshot(fixture.repo, revision)
            # Restore the exact approved file before checking the different-path case.
            fixture.write(fixture.repo, name, data)


if __name__ == '__main__':
    unittest.main()
