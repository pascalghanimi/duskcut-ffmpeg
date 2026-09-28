"""Fail closed on compiled codec inventory, not merely requested configure flags."""
import hashlib
import json
import pathlib
import re
import sys

PROFILE_ID = "duskcut-win64-gpl-windows-codecs-v2"
MF_CODECS = frozenset(("H264_MF", "HEVC_MF", "AAC_MF", "AAC_LATM_MF"))
REQUIRED = (
    "GPL", "MEDIAFOUNDATION", "H264_MF_DECODER", "HEVC_MF_DECODER", "AAC_MF_DECODER", "AAC_LATM_MF_DECODER",
    "H264_MF_ENCODER", "HEVC_MF_ENCODER", "AAC_MF_ENCODER",
    "LIBRUBBERBAND", "LIBVIDSTAB", "RUBBERBAND_FILTER", "VIDSTABDETECT_FILTER", "VIDSTABTRANSFORM_FILTER",
    "AV1_DECODER", "LIBDAV1D_DECODER", "LIBSVTAV1_ENCODER", "AVIF_MUXER", "MOV_DEMUXER",
    "VP8_DECODER", "VP9_DECODER", "LIBVPX_VP9_DECODER", "LIBVPX_VP9_ENCODER", "MPEG2VIDEO_DECODER",
)


def forbidden(name):
    if name in ("LIBX264", "LIBX265", "LIBOPENH264", "LIBFDK_AAC", "LIBDVDREAD", "LIBDVDNAV", "LIBCDIO"):
        return True
    match = re.fullmatch(r"(.+)_(DECODER|ENCODER|HWACCEL)", name)
    if not match:
        return False
    codec, kind = match.groups()
    if codec.startswith(("PRORES", "WMV", "VC1", "LIBX264", "LIBX265", "LIBOPENH264", "LIBFDK_AAC")):
        return True
    if codec.startswith(("H264", "HEVC", "AAC")) and codec not in MF_CODECS:
        return True
    return kind == "ENCODER" and codec.endswith(("_NVENC", "_QSV", "_AMF"))


def verify(profile, configs):
    if profile.get("id") != PROFILE_ID:
        raise ValueError("Unsupported codec-policy profile")
    defined = {}
    for data in configs.values():
        for name, value in re.findall(r"^#define CONFIG_([A-Z0-9_]+) ([01])$", data.decode(), re.M):
            if name in defined and defined[name] != int(value):
                raise ValueError("Conflicting component configuration: " + name)
            defined[name] = int(value)
    enabled = {name for name, value in defined.items() if value}
    prohibited = sorted(name for name in enabled if forbidden(name))
    if prohibited:
        raise ValueError("Forbidden bundled codec/component enabled: " + ", ".join(prohibited))
    missing = sorted(set(REQUIRED) - enabled)
    if missing:
        raise ValueError("Required Windows/media/effect component missing: " + ", ".join(missing))
    roots, stages = profile.get("rootDependencies", []), profile.get("sourceStages", [])
    if len(stages) != len(set(stages)) or any(re.search(r"x26[45]|dvd|css", name) for name in [*roots, *stages]):
        raise ValueError("Forbidden or duplicate dependency source stage")
    return {"schemaVersion": 1, "profileId": PROFILE_ID, "status": "configured-components-verified",
            "configSha256": {name: hashlib.sha256(data).hexdigest() for name, data in configs.items()},
            "requiredEnabled": sorted(REQUIRED), "forbiddenEnabled": [],
            "enabledCodecs": sorted(name for name in enabled if name.endswith(("_ENCODER", "_DECODER", "_HWACCEL")))}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Usage: verify_codec_profile.py <profile.json> <config.h> <config_components.h>")
    profile = json.loads(pathlib.Path(sys.argv[1]).read_text())
    configs = {pathlib.Path(name).name: pathlib.Path(name).read_bytes() for name in sys.argv[2:]}
    print(json.dumps(verify(profile, configs), indent=2))
