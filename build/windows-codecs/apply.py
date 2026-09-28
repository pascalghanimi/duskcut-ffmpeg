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
aac_mf_decoder_select="mpeg4audio adts_header aac_adtstoasc_bsf"
aac_latm_mf_decoder_deps="mediafoundation"
aac_latm_mf_decoder_select="mpeg4audio adts_header"
h264_mf_decoder_deps="mediafoundation"
h264_mf_decoder_select="h264_mp4toannexb_bsf h264_parser mpeg4audio adts_header"
hevc_mf_decoder_deps="mediafoundation"
hevc_mf_decoder_select="hevc_mp4toannexb_bsf hevc_parser mpeg4audio adts_header"
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

# Both APIs accept a WCHAR count, not sizeof(bytes). Keep this upstream helper
# bounds correction in the same reproducible source patch.
utility = root / "libavcodec/mf_utils.c"
contents = utility.read_text()
if contents.count('sizeof(s), NULL)') != 2:
    raise SystemExit("Pinned MF attribute-buffer layout changed")
contents = contents.replace('sizeof(s), NULL)', 'FF_ARRAY_ELEMS(s), NULL)')
def replace_once(old, new):
    global contents
    if contents.count(old) != 1:
        raise SystemExit("Pinned MF sample-allocation layout changed")
    contents = contents.replace(old, new)
replace_once('''    hr = f->MFCreateAlignedMemoryBuffer(size, align - 1, &buffer);
    if (FAILED(hr))
        return NULL;''', '''    hr = f->MFCreateAlignedMemoryBuffer(size, align - 1, &buffer);
    if (FAILED(hr)) {
        IMFSample_Release(sample);
        return NULL;
    }''')
replace_once('''        IMFMediaBuffer_SetCurrentLength(buffer, size);
        IMFMediaBuffer_Unlock(buffer);''', '''        hr = IMFMediaBuffer_SetCurrentLength(buffer, size);
        IMFMediaBuffer_Unlock(buffer);
        if (FAILED(hr)) {
            IMFMediaBuffer_Release(buffer);
            IMFSample_Release(sample);
            return NULL;
        }''')
replace_once('''    IMFSample_AddBuffer(sample, buffer);
    IMFMediaBuffer_Release(buffer);

    return sample;''', '''    hr = IMFSample_AddBuffer(sample, buffer);
    IMFMediaBuffer_Release(buffer);
    if (FAILED(hr)) {
        IMFSample_Release(sample);
        return NULL;
    }
    return sample;''')
utility.write_text(contents)
