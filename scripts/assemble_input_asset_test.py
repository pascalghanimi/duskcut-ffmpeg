import base64
import hashlib
import importlib.util
import io
import json
import pathlib
import tarfile
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("assembler", pathlib.Path(__file__).with_name("assemble-input-asset.py"))
assembler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assembler)


def pin(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class SourceReconstructionTests(unittest.TestCase):
    def test_reconstruction_preserves_literal_tar_bytes_and_exact_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            support, payloads = root / "support", root / "payloads"
            support.mkdir()
            payloads.mkdir()
            payload = b"source archive bytes\x00\xff"
            (payloads / "source.tar.xz").write_bytes(payload)
            prefix, suffix = b"literal\x00header", b"\x00" * 777
            expected = prefix + payload + suffix
            recipe = {"output": pin(expected), "blocks": [{"literalBase64": base64.b64encode(prefix).decode()}, {"file": "source.tar.xz", **pin(payload)}, {"literalBase64": base64.b64encode(suffix).decode()}]}
            assembler.reconstruct(recipe, support, payloads, root / "result.tar")
            self.assertEqual((root / "result.tar").read_bytes(), expected)

    def test_source_hash_change_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "source").write_bytes(b"bad")
            recipe = {"output": pin(b"good"), "blocks": [{"file": "source", **pin(b"good")} ]}
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                assembler.reconstruct(recipe, root, root, root / "result")

    def test_paths_and_authenticated_or_mutable_urls_are_refused(self):
        for name in ("../secret", "/absolute", "C:/file", "x\\file", "x/../../secret"):
            with self.assertRaises(ValueError):
                assembler.safe_relative(name)
        for url in ("http://codeload.github.com/FFmpeg/FFmpeg/tar.gz/" + "a" * 40,
                    "https://user:pass@codeload.github.com/FFmpeg/FFmpeg/tar.gz/" + "a" * 40,
                    "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/master",
                    "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/" + "a" * 40 + "?token=secret"):
            with self.assertRaises(ValueError):
                assembler.immutable_origin(url)

    def test_selected_cache_extracts_only_matching_verified_regular_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            destination = root / "output"
            destination.mkdir()
            payload = b"compressed exact source"
            name = "50-x264_" + "a" * 64 + ".tar.xz"
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                for entry, data in ((".cache/downloads/" + name, payload), ("unselected-source", b"ignore")):
                    info = tarfile.TarInfo(entry)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            inner = raw.getvalue()
            zip_path = root / "artifact.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("cache.tar.gz", inner)
            record = {"file": "sources/downloads/" + name, **pin(payload)}
            assembler.extract_cache(zip_path, destination, [record], pin(zip_path.read_bytes()), pin(inner))
            self.assertEqual((destination / record["file"]).read_bytes(), payload)
            self.assertFalse((destination / "unselected-source").exists())

    def test_cache_symlink_cannot_supply_selected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            destination = root / "output"
            destination.mkdir()
            name = "50-x264_" + "a" * 64 + ".tar.xz"
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                info = tarfile.TarInfo(".cache/downloads/" + name)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            inner = raw.getvalue()
            zip_path = root / "artifact.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("cache.tar.gz", inner)
            with self.assertRaisesRegex(ValueError, "nonregular"):
                assembler.extract_cache(zip_path, destination, [{"file": "sources/downloads/" + name, **pin(b"x")}], pin(zip_path.read_bytes()), pin(inner))


if __name__ == "__main__":
    unittest.main()
