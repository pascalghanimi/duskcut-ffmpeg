"""Reconstruct the reviewed source TAR byte-for-byte on a fast CI host.

The small support package contains literal TAR headers/padding and small files.
Large source payloads come from a content-pinned BtbN artifact and three immutable
public source snapshots. No compiler or acquired build code is executed here.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

CHUNK = 1024 * 1024
EXPECTED_TAR_SHA256 = "e996b20b3583f2b0aef8919641de9be1df7ce13178383865c9573c38a3a58f65"
EXPECTED_TAR_BYTES = 744173568
CACHE_ZIP_SHA256 = "fa2f9f26e34789bdac3563fc57872e5e2dc88970060364c502199edecf626055"
CACHE_ZIP_BYTES = 2191426035
CACHE_INNER_SHA256 = "a1911f8cc14f31f9a1f6460f35fd400513b690c513bad65effa531fb73687dd6"
CACHE_INNER_BYTES = 2190760252


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Invalid relative input path")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) in ("", "."):
        raise ValueError("Input path escapes its root")
    return path


def local_file(root, relative):
    path = root.joinpath(*safe_relative(relative).parts)
    resolved = path.resolve(strict=True)
    if root.resolve() not in resolved.parents or not path.is_file():
        raise ValueError("Input is not a regular contained file")
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("Input symlinks are not accepted")
        cursor = cursor.parent
    return path


def valid_pin(record):
    if not isinstance(record.get("bytes"), int) or isinstance(record["bytes"], bool) or record["bytes"] < 1:
        raise ValueError("Input size is not pinned")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
        raise ValueError("Input digest is not pinned")


def check_file(path, record):
    valid_pin(record)
    if path.stat().st_size != record["bytes"]:
        raise ValueError("Input size mismatch: " + path.name)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while data := source.read(CHUNK):
            digest.update(data)
    if digest.hexdigest() != record["sha256"]:
        raise ValueError("Input digest mismatch: " + path.name)


def copy_checked(source, target, record):
    valid_pin(record)
    digest = hashlib.sha256()
    remaining = record["bytes"]
    with target.open("xb") as output:
        while remaining:
            data = source.read(min(CHUNK, remaining))
            if not data:
                raise ValueError("Source payload truncated: " + target.name)
            output.write(data)
            digest.update(data)
            remaining -= len(data)
        if source.read(1):
            raise ValueError("Source payload exceeds declared size: " + target.name)
    if digest.hexdigest() != record["sha256"]:
        raise ValueError("Source payload digest mismatch: " + target.name)


def extract_cache(cache_zip, destination, selected, zip_pin=None, inner_pin=None):
    zip_pin = zip_pin or {"bytes": CACHE_ZIP_BYTES, "sha256": CACHE_ZIP_SHA256}
    inner_pin = inner_pin or {"bytes": CACHE_INNER_BYTES, "sha256": CACHE_INNER_SHA256}
    check_file(cache_zip, zip_pin)
    by_member = {}
    for record in selected:
        name = safe_relative(record["file"]).name
        if not re.fullmatch(r"\d{2}-[a-z0-9-]+_[0-9a-f]{64}\.tar\.xz", name):
            raise ValueError("Unexpected cache source filename")
        member = ".cache/downloads/" + name
        if member in by_member:
            raise ValueError("Duplicate requested cache source")
        by_member[member] = record
    with zipfile.ZipFile(cache_zip) as archive:
        members = [entry for entry in archive.infolist() if entry.filename == "cache.tar.gz"]
        if len(members) != 1 or members[0].file_size != inner_pin["bytes"]:
            raise ValueError("Unexpected BtbN cache container")
        mode = (members[0].external_attr >> 16) & 0o170000
        if mode not in (0, stat.S_IFREG):
            raise ValueError("Cache container is not a regular file")
        container = destination / "cache.tar.gz"
        with archive.open(members[0]) as stream:
            copy_checked(stream, container, inner_pin)
    found = set()
    # Streaming tar access avoids extracting arbitrary files or following links.
    with tarfile.open(container, "r|gz") as archive:
        for member in archive:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name in ("", "."):
                continue
            safe_relative(name)
            if name not in by_member:
                continue
            if name in found or not member.isfile():
                raise ValueError("Duplicate or nonregular selected source")
            record = by_member[name]
            if member.size != record["bytes"]:
                raise ValueError("Selected source size mismatch")
            target = destination.joinpath(*safe_relative(record["file"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Missing selected source stream")
            with stream:
                copy_checked(stream, target, record)
            found.add(name)
    if found != set(by_member):
        raise ValueError("Selected cache sources missing: " + ", ".join(sorted(set(by_member) - found)))
    # Only this newly created, verified temporary container is removed.
    container.unlink()


def immutable_origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "codeload.github.com" or parsed.query or parsed.fragment:
        raise ValueError("Source origin must be an immutable public codeload URL")
    if not re.fullmatch(r"/(?:BtbN/FFmpeg-Builds|FFmpeg/FFmpeg|crosstool-ng/crosstool-ng)/tar\.gz/[0-9a-f]{40}", parsed.path):
        raise ValueError("Source origin is outside the reviewed snapshot set")
    return url


class PublicRedirectsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        immutable_origin(new_url)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def fetch_snapshot(record, destination):
    url = immutable_origin(record["origin"])
    target = destination.joinpath(*safe_relative(record["file"]).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "DuskCut-FFmpeg-source-reconstruction/1"})
    opener = urllib.request.build_opener(PublicRedirectsOnly())
    with opener.open(request, timeout=120) as response:
        copy_checked(response, target, record)


def reconstruct(recipe, support, payload_root, target):
    valid_pin(recipe["output"])
    if target.exists():
        raise ValueError("Output already exists")
    digest = hashlib.sha256()
    count = 0
    seen_payloads = {}
    with target.open("xb") as output:
        for block in recipe["blocks"]:
            if set(block) == {"literalBase64"}:
                data = base64.b64decode(block["literalBase64"], validate=True)
                output.write(data)
                digest.update(data)
                count += len(data)
            elif set(block) == {"file", "bytes", "sha256"}:
                valid_pin(block)
                name = str(safe_relative(block["file"]))
                source_root = payload_root if payload_root.joinpath(*safe_relative(name).parts).exists() else support
                path = local_file(source_root, name)
                if name in seen_payloads and seen_payloads[name] != (block["bytes"], block["sha256"]):
                    raise ValueError("Contradictory source payload identity")
                seen_payloads[name] = (block["bytes"], block["sha256"])
                check_file(path, block)
                with path.open("rb") as source:
                    while data := source.read(CHUNK):
                        output.write(data)
                        digest.update(data)
                        count += len(data)
            else:
                raise ValueError("Unknown reconstruction block")
            if count > recipe["output"]["bytes"]:
                raise ValueError("Reconstruction exceeds declared size")
    if count != recipe["output"]["bytes"] or digest.hexdigest() != recipe["output"]["sha256"]:
        raise ValueError("Final reconstructed TAR identity mismatch")


def assemble(support, cache_zip, output):
    support = support.resolve(strict=True)
    recipe = json.loads(local_file(support, "source-reconstruction.json").read_text())
    manifest = json.loads(local_file(support, "source-manifest.json").read_text())
    if recipe.get("schemaVersion") != 1 or recipe["output"].get("sha256") != EXPECTED_TAR_SHA256 or recipe["output"].get("bytes") != EXPECTED_TAR_BYTES:
        raise ValueError("Reconstruction recipe is not the reviewed source TAR")
    artifact = recipe["cacheArtifact"]
    if artifact.get("id") != 10931492582 or artifact.get("sha256") != CACHE_ZIP_SHA256 or artifact.get("bytes") != CACHE_ZIP_BYTES:
        raise ValueError("Unreviewed BtbN artifact")
    records = {"sources/" + row["file"]: row for row in manifest["files"]}
    external = recipe["externalFiles"]
    if len(external) != 44 or len({row["file"] for row in external}) != 44:
        raise ValueError("Unexpected external payload closure")
    for row in external:
        known = records.get(row["file"])
        if not known or any(row.get(key) != known.get(key) for key in ("bytes", "sha256", "origin", "role")):
            raise ValueError("External payload does not match reviewed manifest")
    cache = [row for row in external if row["role"] == "dependency-source"]
    snapshots = [row for row in external if row["role"] in ("recipe", "ffmpeg-source", "toolchain-recipe-source")]
    if len(cache) != 41 or len(snapshots) != 3 or len(cache) + len(snapshots) != len(external):
        raise ValueError("Unexpected source payload roles")
    output = output.resolve()
    if output.exists():
        raise ValueError("Output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="duskcut-source-reconstruction-", dir=output.parent) as temporary:
        scratch = pathlib.Path(temporary)
        print("Verifying BtbN artifact and recovering 41 selected source archives", flush=True)
        extract_cache(cache_zip, scratch, cache)
        for row in snapshots:
            print("Fetching fixed public snapshot: " + safe_relative(row["file"]).name, flush=True)
            fetch_snapshot(row, scratch)
        candidate = scratch / "reviewed-source.tar"
        reconstruct(recipe, support, scratch, candidate)
        # No overwriting an existing release asset, even if it appeared mid-run.
        with candidate.open("rb") as source, output.open("xb") as target:
            shutil.copyfileobj(source, target, CHUNK)
        check_file(output, recipe["output"])
    print(json.dumps({"file": str(output), "bytes": EXPECTED_TAR_BYTES, "sha256": EXPECTED_TAR_SHA256, "status": "exact-reviewed-source-TAR-reconstructed"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support", required=True, type=pathlib.Path)
    parser.add_argument("--btbn-zip", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    assemble(args.support, args.btbn_zip, args.output)
