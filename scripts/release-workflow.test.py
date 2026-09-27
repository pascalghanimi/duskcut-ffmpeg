"""Offline boundary tests: no GitHub/network calls or real release mutations."""
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), pathlib.Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load('prepare-release-inputs')
upload = load('upload-draft-release')


def encoded(value):
    return (json.dumps(value, indent=2) + '\n').encode()


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ffmpeg-release-workflow-test-')
        self.original_cwd = pathlib.Path.cwd()
        self.directory = pathlib.Path(self.temp.name)
        os.chdir(self.directory)

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.temp.cleanup()


class PrepareTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.metadata = {'id': 123, 'status': 'completed', 'conclusion': 'success',
                         'path': '.github/workflows/build.yml', 'head_branch': 'main',
                         'event': 'workflow_dispatch', 'repository': {'full_name': prepare.REPO},
                         'head_sha': 'a' * 40}
        self.artifacts = {'total_count': 1, 'artifacts': [{'id': 456, 'name': 'duskcut-ffmpeg-test.1-123',
            'expired': False, 'workflow_run': {'id': 123, 'head_sha': 'a' * 40}}]}
        self.manifest = {'schemaVersion': 1, 'extractTo': '.', 'releaseTag': 'sources-test.1',
            'sourceManifestSha256': 'b' * 64, 'supplementalManifestSha256': 'c' * 64,
            'assets': [{'file': 'input.tar', 'bytes': 4, 'sha256': hashlib.sha256(b'test').hexdigest(),
                       'url': f'https://github.com/{prepare.REPO}/releases/download/sources-test.1/input.tar'}]}
        self.calls = []
        self.artifact_commit = 'a' * 40

    def check_output(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['git', 'show']:
            self.assertEqual(args[2], 'a' * 40 + ':release-assets.json')
            return encoded(self.manifest)
        if args[:2] == ('gh', 'api'):
            return json.dumps(self.artifacts if '/artifacts?' in args[-1] else self.metadata)
        raise AssertionError('Unexpected command: ' + repr(args))

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        self.assertTrue(kwargs.get('check'))
        if args[:3] == ['gh', 'run', 'download']:
            pathlib.Path('build-artifact/BUILD-RECIPE-COMMIT.txt').write_text(self.artifact_commit)
        elif args[:2] == ['node', 'scripts/fetch-inputs.mjs']:
            pathlib.Path('downloads').mkdir()
            pathlib.Path('downloads/input.tar').write_bytes(b'test')
        elif args[:2] == ['python3', 'scripts/extract-inputs.py']:
            pathlib.Path('source-staging').mkdir()
        return subprocess.CompletedProcess(args, 0)

    def execute(self, run_id='123'):
        with patch.dict(os.environ, {'BUILD_RUN': run_id}), \
             patch.object(prepare.subprocess, 'check_output', side_effect=self.check_output), \
             patch.object(prepare.subprocess, 'run', side_effect=self.run_command):
            prepare.prepare()

    def test_exact_successful_build_commit_and_source_manifest_are_replayed(self):
        self.execute()
        self.assertEqual(json.loads(pathlib.Path('source-staging/release-assets.json').read_text()), self.manifest)
        self.assertEqual(pathlib.Path('source-staging/input.tar').read_bytes(), b'test')
        self.assertIn(['git', 'merge-base', '--is-ancestor', 'a' * 40, 'HEAD'], self.calls)

    def test_invalid_run_id_fails_before_any_command(self):
        for value in ('', '../123', '123;echo', '0', '-1', '1' * 21):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'numeric'):
                self.execute(value)
        self.assertEqual(self.calls, [])

    def test_wrong_run_identity_conclusion_branch_event_or_repository_rejected(self):
        original = copy.deepcopy(self.metadata)
        for key, value in [('id', 124), ('status', 'in_progress'), ('conclusion', 'failure'),
                           ('path', '.github/workflows/other.yml'), ('head_branch', 'fork'),
                           ('event', 'pull_request'), ('repository', {'full_name': 'other/repo'}),
                           ('head_sha', 'main')]:
            self.metadata = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.execute()
        self.assertFalse(pathlib.Path('build-artifact').exists())

    def test_missing_duplicate_expired_or_wrong_run_artifact_rejected(self):
        row = copy.deepcopy(self.artifacts['artifacts'][0])
        for rows, count in [([], 0), ([row, row], 2), ([{**row, 'expired': True}], 1),
                            ([{**row, 'workflow_run': {'id': 999, 'head_sha': 'a' * 40}}], 1),
                            ([{**row, 'workflow_run': {'id': 123, 'head_sha': 'b' * 40}}], 1),
                            ([row], 2)]:
            self.artifacts = {'artifacts': rows, 'total_count': count}
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.execute()
        self.assertFalse(pathlib.Path('build-artifact').exists())

    def test_artifact_recorded_commit_must_match_run_before_input_fetch(self):
        self.artifact_commit = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'recipe identity'):
            self.execute()
        self.assertFalse(any(args[0] in ('node', 'python3') for args in self.calls))

    def test_manifest_rejects_path_redirect_duplicates_and_wrong_hash_metadata(self):
        original = copy.deepcopy(self.manifest)
        for key, value in [('extractTo', '../outside'), ('releaseTag', 'latest'),
                           ('sourceManifestSha256', 'bad'), ('supplementalManifestSha256', 'bad')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare.validate_manifest({**original, key: value})
        for key, value in [('file', '../input.tar'), ('url', 'https://evil.invalid/input.tar'),
                           ('bytes', True), ('bytes', 0), ('sha256', 'bad')]:
            changed = copy.deepcopy(original)
            changed['assets'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare.validate_manifest(changed)
        changed = copy.deepcopy(original)
        changed['assets'].append(copy.deepcopy(changed['assets'][0]))
        with self.assertRaises(ValueError):
            prepare.validate_manifest(changed)


class UploadTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.release_dir = pathlib.Path('packaged-release')
        self.release_dir.mkdir()
        self.candidate = {'schemaVersion': 1, 'approvalStatus': 'candidate-awaiting-review', 'buildId': 'test.1',
            'buildRecipeCommit': 'a' * 40, 'ffmpegCommit': 'b' * 40, 'version': '9.0.2-test.1',
            'files': [{'path': 'bin/ffmpeg.exe', 'sha256': 'c' * 64}, {'path': 'bin/ffprobe.exe', 'sha256': 'd' * 64}]}
        for key, suffix in [('archive', 'win64.zip'), ('source', 'corresponding-source.tar')]:
            name = 'duskcut-ffmpeg-test.1-' + suffix
            path = self.release_dir / name
            path.write_bytes(name.encode())
            self.candidate[key] = {'url': f'https://github.com/{upload.REPO}/releases/download/test.1/{name}',
                                   'size': path.stat().st_size, 'sha256': upload.file_hash(path)}
        self.binding = {'schemaVersion': 1, 'status': 'assembled-awaiting-source-runtime-and-functional-review',
            'buildId': 'test.1', 'buildRecipeCommit': 'a' * 40, 'ffmpegRevision': 'b' * 40,
            'versionTokenProvidedByOperator': '9.0.2-test.1', 'buildLockSha256': 'e' * 64,
            'sourceManifestSha256': 'f' * 64, 'binaryHashes': {'ffmpeg.exe': 'c' * 64, 'ffprobe.exe': 'd' * 64}}
        self.save()
        self.pages = [[]]
        self.current = {'id': 789, 'draft': True, 'tag_name': 'test.1', 'target_commitish': 'a' * 40, 'assets': []}
        self.calls, self.uploaded = [], []

    def save(self):
        (self.release_dir / 'ffmpeg-release.candidate.json').write_bytes(encoded(self.candidate))
        (self.release_dir / 'SOURCE-BINDING.json').write_bytes(encoded(self.binding))
        (self.release_dir / 'SHA256SUMS.txt').write_text(''.join(upload.file_hash(path) + '  ' + path.name + '\n'
            for path in sorted(self.release_dir.iterdir()) if path.name != 'SHA256SUMS.txt'))

    def api(self, *args, payload=None):
        self.calls.append((args, payload))
        if '--paginate' in args:
            return self.pages
        if args[:2] == ('--method', 'POST') and args[2] == f'repos/{upload.REPO}/releases':
            self.assertIs(payload['draft'], True)
            return copy.deepcopy(self.current)
        if len(args) == 1:
            self.assertEqual(args[0], f'repos/{upload.REPO}/releases/789')
            return copy.deepcopy(self.current)
        if args[:2] == ('--method', 'POST') and args[2].startswith('https://uploads.github.com/'):
            self.assertIn('/releases/789/assets?name=', args[2])
            path = pathlib.Path(args[-1])
            self.uploaded.append(path.name)
            self.current['assets'].append({'name': path.name})
            return {'name': path.name, 'size': path.stat().st_size, 'state': 'uploaded',
                    'digest': 'sha256:' + upload.file_hash(path)}
        raise AssertionError('Unexpected API command: ' + repr(args))

    def execute(self):
        with patch.object(upload, 'api', side_effect=self.api):
            upload.upload()

    def test_new_draft_uses_immutable_release_id_and_never_clobbers_or_publishes(self):
        self.execute()
        self.assertEqual(len(self.uploaded), 5)
        self.assertTrue(all('--clobber' not in args and 'DELETE' not in args and 'PATCH' not in args
                            for args, _ in self.calls))
        self.assertEqual(sum(len(args) == 1 for args, _ in self.calls), 6)

    def test_existing_public_or_draft_release_is_never_modified_even_on_later_page(self):
        for draft in (True, False):
            self.pages = [[{'tag_name': 'other', 'draft': True}], [{'tag_name': 'test.1', 'draft': draft}]]
            self.calls = []
            with self.subTest(draft=draft), self.assertRaisesRegex(ValueError, 'already exists'):
                self.execute()
            self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.uploaded, [])

    def test_network_listing_error_is_not_treated_as_missing_release(self):
        with patch.object(upload, 'api', side_effect=RuntimeError('network unavailable')) as api:
            with self.assertRaisesRegex(RuntimeError, 'network'):
                upload.upload()
            self.assertEqual(api.call_count, 1)

    def test_published_or_identity_changed_draft_stops_before_upload(self):
        for key, value in [('draft', False), ('tag_name', 'other'), ('target_commitish', 'e' * 40)]:
            original = self.current[key]
            self.current[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'identity changed'):
                self.execute()
            self.current[key] = original
        self.assertEqual(self.uploaded, [])

    def test_concurrent_publish_after_first_asset_stops_remaining_uploads(self):
        original_api = self.api
        def publish_race(*args, **kwargs):
            result = original_api(*args, **kwargs)
            if self.uploaded:
                self.current['draft'] = False
            return result
        with patch.object(upload, 'api', side_effect=publish_race):
            with self.assertRaisesRegex(ValueError, 'draft was published'):
                upload.upload()
        self.assertEqual(len(self.uploaded), 1)

    def test_existing_asset_in_new_draft_is_not_replaced(self):
        self.current['assets'] = [{'name': 'duskcut-ffmpeg-test.1-win64.zip'}]
        with self.assertRaisesRegex(ValueError, 'asset already exists'):
            self.execute()
        self.assertEqual(self.uploaded, [])

    def test_checksum_manifest_mutation_missing_extra_and_binding_mismatch_fail_before_api(self):
        (self.release_dir / 'SOURCE-BINDING.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.execute()
        self.save()
        self.binding['buildRecipeCommit'] = 'f' * 40
        self.save()
        with self.assertRaisesRegex(ValueError, 'binding and candidate'):
            self.execute()
        self.binding['buildRecipeCommit'] = 'a' * 40
        self.binding['binaryHashes']['ffmpeg.exe'] = 'f' * 64
        self.save()
        with self.assertRaisesRegex(ValueError, 'Binary hashes'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_incorrect_asset_url_and_unexpected_file_fail_before_api(self):
        self.candidate['source']['url'] = 'https://evil.invalid/source.tar'
        self.save()
        with self.assertRaisesRegex(ValueError, 'changed after assembly'):
            self.execute()
        (self.release_dir / '.env').write_text('not a real secret')
        with self.assertRaisesRegex(ValueError, 'Unexpected file'):
            self.execute()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
