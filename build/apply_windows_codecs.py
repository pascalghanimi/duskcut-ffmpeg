"""Apply the reviewed MF adapter and retain exact pre/post source correspondence."""
import difflib
import hashlib
import json
import pathlib
import subprocess
import sys


SOURCE_FILES = ("configure", "libavcodec/Makefile", "libavcodec/allcodecs.c", "libavcodec/mf_utils.c",
                "libavcodec/h264_parser.c", "libavcodec/hevc/parser.c", "libavcodec/mfdec.c")


def apply(source, controls, evidence):
    source, controls, evidence = map(pathlib.Path, (source, controls, evidence))
    if (source / SOURCE_FILES[-1]).exists():
        raise ValueError("MF adapter source already exists; do not patch twice")
    before = {name: (source / name).read_bytes() if (source / name).exists() else b"" for name in SOURCE_FILES}
    subprocess.run([sys.executable, str(controls / "windows-codecs/apply.py"), str(source)], check=True)
    after = {name: (source / name).read_bytes() for name in SOURCE_FILES}
    if after["libavcodec/mfdec.c"] != (controls / "windows-codecs/mfdec.c").read_bytes():
        raise ValueError("Applied MF adapter does not match controlled source")
    if any(before[name] == after[name] for name in SOURCE_FILES):
        raise ValueError("Expected MF source patch was not applied")
    evidence.mkdir(parents=True, exist_ok=True)
    patch = "".join("".join(difflib.unified_diff(
        before[name].decode().splitlines(keepends=True), after[name].decode().splitlines(keepends=True),
        fromfile="a/" + name if before[name] else "/dev/null", tofile="b/" + name)) for name in SOURCE_FILES)
    (evidence / "windows-codecs.patch").write_text(patch, encoding="utf-8", newline="\n")
    record = {"schemaVersion": 1, "files": [
        {"file": name, "beforeSha256": hashlib.sha256(before[name]).hexdigest() if before[name] else None,
         "afterSha256": hashlib.sha256(after[name]).hexdigest()} for name in SOURCE_FILES],
        "controls": {name: hashlib.sha256((controls / name).read_bytes()).hexdigest()
                     for name in ("windows-codecs/apply.py", "windows-codecs/mfdec.c")}}
    (evidence / "windows-codecs-source.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Usage: apply_windows_codecs.py <FFmpeg source> <controls> <evidence>")
    apply(*sys.argv[1:])
