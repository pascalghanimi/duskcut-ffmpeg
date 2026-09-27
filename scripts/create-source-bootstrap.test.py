import base64
import hashlib
import importlib.util
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('bootstrap', pathlib.Path(__file__).with_name('create-source-bootstrap.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BootstrapTests(unittest.TestCase):
    def test_sparse_blocks_preserve_headers_padding_and_file_bytes(self):
        with tempfile.TemporaryDirectory(prefix='ffmpeg-bootstrap-test-') as folder:
            root = pathlib.Path(folder)
            data = b'public source payload'
            (root / 'source').write_bytes(data)
            before, after = b'original PAX header\x00\x00', b'\x00' * 1024
            blocks = [{'literalBase64': base64.b64encode(before).decode()},
                      {'file': 'source', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                      {'literalBase64': base64.b64encode(after).decode()}]
            self.assertEqual(module.reconstructed_identity(root, blocks),
                             {'bytes': len(before + data + after), 'sha256': hashlib.sha256(before + data + after).hexdigest()})
            (root / 'source').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'payload mismatch'):
                module.reconstructed_identity(root, blocks)

    def test_malformed_or_outside_payloads_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='ffmpeg-bootstrap-test-') as folder:
            for blocks in ([{'literalBase64': 'not*base64'}],
                           [{'file': '../outside', 'bytes': 1, 'sha256': '0' * 64}],
                           [{'other': True}]):
                with self.assertRaises(ValueError):
                    module.reconstructed_identity(folder, blocks)


if __name__ == '__main__':
    unittest.main()
