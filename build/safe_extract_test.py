"""Small adversarial archive tests; no vendor scripts or compiler are executed."""
import io
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

extractor = pathlib.Path(__file__).with_name('safe_extract.py')

class SafeExtractionTests(unittest.TestCase):
    def invoke(self, root, archive):
        return subprocess.run([sys.executable, str(extractor), str(archive), str(root/'result')], capture_output=True, text=True)

    def tar(self, root, name='source/file.txt', kind=tarfile.REGTYPE, link=''):
        archive=root/'input.tar.gz'
        with tarfile.open(archive,'w:gz') as t:
            entry=tarfile.TarInfo(name)
            entry.type=kind
            entry.linkname=link
            payload=b'public source'
            if kind==tarfile.REGTYPE: entry.size=len(payload)
            t.addfile(entry,io.BytesIO(payload) if kind==tarfile.REGTYPE else None)
        return archive

    def test_normal_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            self.assertEqual(self.invoke(root,self.tar(root)).returncode,0)
            self.assertEqual((root/'result/source/file.txt').read_text(),'public source')

    def test_normal_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            archive=root/'input.zip'
            with zipfile.ZipFile(archive,'w') as z: z.writestr('cache.tar.gz',b'cache')
            self.assertEqual(self.invoke(root,archive).returncode,0)

    def test_parent_paths_rejected(self):
        for name in ('../escape','source/../../escape','/absolute','C:/absolute','source\\escape'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp)
                self.assertNotEqual(self.invoke(root,self.tar(root,name)).returncode,0)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            self.assertNotEqual(self.invoke(root,self.tar(root,'source/link',tarfile.SYMTYPE,'../../escape')).returncode,0)

    def test_hardlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            self.assertNotEqual(self.invoke(root,self.tar(root,'source/link',tarfile.LNKTYPE,'../escape')).returncode,0)

    def test_special_tar_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            self.assertNotEqual(self.invoke(root,self.tar(root,'source/pipe',tarfile.FIFOTYPE)).returncode,0)

    def test_zip_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            archive=root/'input.zip'
            with zipfile.ZipFile(archive,'w') as z:
                entry=zipfile.ZipInfo('link')
                entry.create_system=3
                entry.external_attr=0o120777<<16
                z.writestr(entry,'../../escape')
            self.assertNotEqual(self.invoke(root,archive).returncode,0)

    def test_existing_destination_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            (root/'result').mkdir()
            self.assertNotEqual(self.invoke(root,self.tar(root)).returncode,0)

if __name__=='__main__': unittest.main()
