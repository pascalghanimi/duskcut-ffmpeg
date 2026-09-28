"""Apply the versioned Windows decoder API glue to the pinned FFmpeg source."""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
own = pathlib.Path(__file__).parent
config = root / "configure"
contents = config.read_text()
marker = 'aac_mf_encoder_deps="mediafoundation"'
if contents.count(marker) != 1:
    raise SystemExit("Pinned FFmpeg configure layout changed")
contents = contents.replace(marker, '''aac_mf_decoder_deps="mediafoundation"
aac_mf_decoder_select="mpeg4audio"
aac_latm_mf_decoder_deps="mediafoundation"
aac_latm_mf_decoder_select="mpeg4audio"
h264_mf_decoder_deps="mediafoundation"
h264_mf_decoder_select="h264_mp4toannexb_bsf mpeg4audio"
hevc_mf_decoder_deps="mediafoundation"
hevc_mf_decoder_select="hevc_mp4toannexb_bsf mpeg4audio"
''' + marker)
config.write_text(contents)
makefile = root / "libavcodec/Makefile"
contents = makefile.read_text()
for name in ("AAC", "AAC_LATM", "H264", "HEVC"):
    contents += f"\nOBJS-$(CONFIG_{name}_MF_DECODER) += mfdec.o mf_utils.o\n"
makefile.write_text(contents)
registry = root / "libavcodec/allcodecs.c"
contents = registry.read_text()
marker = "extern const FFCodec ff_aac_mf_encoder;"
if contents.count(marker) != 1:
    raise SystemExit("Pinned FFmpeg codec registry changed")
contents = contents.replace(marker, "\n".join(
    f"extern const FFCodec ff_{name}_mf_decoder;" for name in ("aac", "aac_latm", "h264", "hevc")
) + "\n" + marker)
registry.write_text(contents)
(root / "libavcodec/mfdec.c").write_bytes((own / "mfdec.c").read_bytes())
