"""Extract verified source archives into empty dedicated directories, rejecting escapes and special files."""
import pathlib
import sys
import tarfile
import zipfile

archive, target = map(pathlib.Path, sys.argv[1:])
target = target.resolve()
target.mkdir(parents=True, exist_ok=False)

def bounded(name):
    if "\\" in name or name.startswith("/") or ":" in name or ".." in pathlib.PurePosixPath(name).parts:
        raise ValueError("unsafe archive path: " + name)
    path = (target / name).resolve()
    if path != target and target not in path.parents:
        raise ValueError("archive path escapes destination")
    return path

if zipfile.is_zipfile(archive):
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            bounded(entry.filename)
            kind = (entry.external_attr >> 16) & 0o170000
            if kind not in (0, 0o100000, 0o040000):
                raise ValueError("zip links or special files forbidden")
        z.extractall(target)
else:
    with tarfile.open(archive, "r:*") as t:
        for entry in t.getmembers():
            path = bounded(entry.name)
            if entry.issym():
                if pathlib.PurePosixPath(entry.linkname).is_absolute() or "\\" in entry.linkname:
                    raise ValueError("unsafe symbolic link")
                linked = (path.parent / entry.linkname).resolve()
                if linked != target and target not in linked.parents:
                    raise ValueError("symbolic link escapes destination")
            elif entry.islnk():
                bounded(entry.linkname)
            elif not (entry.isfile() or entry.isdir()):
                raise ValueError("special archive member forbidden")
        # Python 3.12+ data filter also prevents link-mediated and ownership attacks.
        if not hasattr(tarfile, "data_filter"):
            raise RuntimeError("Python 3.12+ safe tar extraction required")
        t.extractall(target, filter="data")
