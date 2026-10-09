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

# Windows MFTs omit some bitstream colour values or approximate BT.709 as gamma
# 2.2. The already-enabled header parsers know the exact VUI; expose only values
# that were actually signalled. This is metadata parsing, not pixel decoding.
for relative, vui, marker in (
    ("libavcodec/h264_parser.c", "sps->vui",
     "            avctx->profile = ff_h264_get_profile(sps);"),
    ("libavcodec/hevc/parser.c", "sps->vui.common",
     "    avctx->profile  = sps->ptl.general_ptl.profile_idc;"),
):
    path = root / relative
    source = path.read_text()
    if source.count(marker) != 1:
        raise SystemExit("Pinned VUI header-parser layout changed: " + relative)
    indent = marker[:len(marker) - len(marker.lstrip())]
    lines = [
        "/* Retain explicit VUI metadata for installed-system-codec wrappers. */",
        "{",
        f"    const H2645VUI *vui = &{vui};",
        "    if (vui->aspect_ratio_info_present_flag && vui->sar.num > 0 && vui->sar.den > 0)",
        "        avctx->sample_aspect_ratio = vui->sar;",
        "    if (vui->video_signal_type_present_flag)",
        "        avctx->color_range = vui->video_full_range_flag ? AVCOL_RANGE_JPEG : AVCOL_RANGE_MPEG;",
        "    if (vui->colour_description_present_flag) {",
        "        avctx->color_primaries = vui->colour_primaries;",
        "        avctx->color_trc = vui->transfer_characteristics;",
        "        avctx->colorspace = vui->matrix_coeffs;",
        "    }",
        "    if (vui->chroma_loc_info_present_flag)",
        "        avctx->chroma_sample_location = vui->chroma_location;",
        "}",
    ]
    source = source.replace(marker, "\n".join(indent + line for line in lines) + "\n" + marker)
    path.write_text(source)

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

# The MP4-to-Annex-B filter prepends SPS/PPS only to IDR pictures. After a seek
# onto an H.264 recovery point (a non-IDR keyframe: open-GOP x264, YouTube) the
# Windows decoder therefore received no parameter sets and returned nothing
# until the next IDR. Only this filter knows the CURRENT parameter sets (it
# refreshes them from new sample descriptions and in-band units), so it hands
# them over once: before the first picture after start, flush or a change,
# whatever that picture is. Windows keeps them from then on. Opt-in option,
# set only by h264_mf: stream copies and remuxes are unchanged.
annexb = root / "libavcodec/bsf/h264_mp4toannexb.c"
contents = annexb.read_text()
def replace_once(old, new):
    global contents
    if contents.count(old) != 1:
        raise SystemExit("Pinned h264_mp4toannexb layout changed")
    contents = contents.replace(old, new)
replace_once('''#include "libavutil/mem.h"
''', '''#include "libavutil/mem.h"
#include "libavutil/opt.h"
''')
replace_once('''typedef struct H264BSFContext {
    uint8_t *sps;''', '''typedef struct H264BSFContext {
    const AVClass *class;
    int      resume_ps;
    int      ps_pending;
    uint8_t *sps;''')
replace_once('''    s->extradata_parsed = 1;
''', '''    s->extradata_parsed = 1;
    s->ps_pending       = 1;
''')
replace_once('''    s->new_idr      = s->extradata_parsed;
''', '''    s->new_idr      = s->extradata_parsed;
    s->ps_pending   = s->extradata_parsed;
''')
replace_once('''    uint8_t unit_type, new_idr, sps_seen, pps_seen;''',
             '''    uint8_t unit_type, new_idr, sps_seen, pps_seen, ps_pending;''')
replace_once('''        new_idr  = s->new_idr;''', '''        new_idr  = s->new_idr;
        ps_pending = s->ps_pending;''')
replace_once('''            count_or_copy(&out, &out_size, buf, nal_size, ps, j);''', '''            /* resume_ps: the first picture after start, flush or a parameter
             * set change gets the current SPS/PPS if it lacks them, whatever its
             * type (recovery point, unflagged I-frame, intra refresh). An IDR
             * picture keeps the rule above. */
            if (ps_pending && (unit_type == H264_NAL_SLICE || unit_type == H264_NAL_IDR_SLICE)) {
                if (s->resume_ps && unit_type == H264_NAL_SLICE) {
                    if (!sps_seen && !pps_seen) {
                        if (s->sps_size)
                            count_or_copy(&out, &out_size, s->sps, s->sps_size, PS_OUT_OF_BAND, j);
                        if (s->pps_size)
                            count_or_copy(&out, &out_size, s->pps, s->pps_size, PS_OUT_OF_BAND, j);
                    } else if (sps_seen && !pps_seen && s->pps_size) {
                        count_or_copy(&out, &out_size, s->pps, s->pps_size, PS_OUT_OF_BAND, j);
                    }
                }
                ps_pending = 0;
            }
            count_or_copy(&out, &out_size, buf, nal_size, ps, j);''')
replace_once('''    s->new_idr      = new_idr;
    s->idr_sps_seen = sps_seen;''', '''    s->new_idr      = new_idr;
    s->ps_pending   = ps_pending;
    s->idr_sps_seen = sps_seen;''')
replace_once('''const FFBitStreamFilter ff_h264_mp4toannexb_bsf = {
    .p.name         = "h264_mp4toannexb",''', '''#define OFFSET(x) offsetof(H264BSFContext, x)
static const AVOption h264_mp4toannexb_options[] = {
    { "resume_ps", "insert the current SPS/PPS before the first picture after start, flush or a parameter set change when it carries none",
      OFFSET(resume_ps), AV_OPT_TYPE_BOOL, { .i64 = 0 }, 0, 1,
      AV_OPT_FLAG_VIDEO_PARAM | AV_OPT_FLAG_BSF_PARAM },
    { NULL },
};

static const AVClass h264_mp4toannexb_class = {
    .class_name = "h264_mp4toannexb",
    .item_name  = av_default_item_name,
    .option     = h264_mp4toannexb_options,
    .version    = LIBAVUTIL_VERSION_INT,
};

const FFBitStreamFilter ff_h264_mp4toannexb_bsf = {
    .p.name         = "h264_mp4toannexb",
    .p.priv_class   = &h264_mp4toannexb_class,''')
annexb.write_text(contents)
