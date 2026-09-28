/*
 * Windows Media Foundation decoding bridge for DuskCut's FFmpeg component.
 * Copyright (c) 2026 DuskCut contributors
 *
 * This is API glue, not an implementation of AAC, AVC or HEVC. Compressed
 * samples are decoded by an independently installed Windows MFT. No codec
 * binary is downloaded and no native FFmpeg decoder is used as a fallback.
 *
 * This file is part of FFmpeg, under the GNU Lesser General Public License,
 * version 2.1 or (at your option) any later version. See COPYING.LGPLv2.1.
 */

#define COBJMACROS
#if !defined(_WIN32_WINNT) || _WIN32_WINNT < 0x0602
#undef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif

#include "mf_utils.h"
#include "codec_internal.h"
#include "decode.h"
#include "internal.h"
#include "adts_header.h"
#include "mpeg4audio.h"
#include "compat/w32dlfcn.h"
#include "libavutil/channel_layout.h"
#include "libavutil/imgutils.h"
#include "libavutil/intreadwrite.h"
#include "libavutil/mastering_display_metadata.h"
#include "libavutil/pixdesc.h"

#define MF_TIME_BASE ((AVRational){ 1, 10000000 })

typedef struct MFDecodeContext {
    HMODULE library;
    MFFunctions functions;
    IMFTransform *mft;
    DWORD input_id, output_id;
    MFT_OUTPUT_STREAM_INFO output_info;
    AVPacket *packet;
    AVCodecParserContext *parser;
    AVCodecContext *parser_context;
    enum AVPixelFormat source_format;
    AVPacket *properties[512];
    int property_count;
    int audio, draining, eof, failed, configured, discontinuity;
    int width, height, stride;
    int crop_left, crop_top, crop_right, crop_bottom;
    enum AVPixelFormat pixel_format;
    enum AVSampleFormat sample_format;
    int sample_rate, channels;
    uint32_t channel_mask;
    AVMasteringDisplayMetadata mastering;
    AVContentLightMetadata content_light;
    int has_content_light;
} MFDecodeContext;

/* Windows SDK mfapi.h defines these, but not every MinGW snapshot does.
 * These are attribute identifiers, not imported codec functions. */
static const GUID duskcut_mf_max_cll =
    {0x50253128,0xc110,0x4de4,{0x98,0xae,0x46,0xa3,0x24,0xfa,0xe6,0xda}};
static const GUID duskcut_mf_max_fall =
    {0x58d4bf57,0x6f52,0x4733,{0xa1,0x95,0xa9,0xe2,0x9e,0xcf,0x9e,0x27}};
static const GUID duskcut_mf_max_mastering =
    {0xd6c6b997,0x272f,0x4ca1,{0x8d,0x00,0x80,0x42,0x11,0x1a,0x0f,0xf6}};
static const GUID duskcut_mf_min_mastering =
    {0x839a4460,0x4e7e,0x4b4f,{0xae,0x79,0xcc,0x08,0x90,0x5c,0x7b,0x27}};

/* MF enumeration values are NOT ISO/AVColor* values. Use the published numeric
 * values so newer enum members also compile with older MinGW headers. Unknown
 * information stays unknown; it must never silently become BT.709. */
static enum AVColorPrimaries mf_color_primaries(UINT32 value)
{
    switch (value) {
    case 2: return AVCOL_PRI_BT709;
    case 3: return AVCOL_PRI_BT470M;
    case 4: return AVCOL_PRI_BT470BG;
    case 5: case 8: return AVCOL_PRI_SMPTE170M;
    case 6: return AVCOL_PRI_SMPTE240M;
    case 7: return AVCOL_PRI_JEDEC_P22;
    case 9: return AVCOL_PRI_BT2020;
    case 10: return AVCOL_PRI_SMPTE428;
    case 11: return AVCOL_PRI_SMPTE431;
    case 13: return AVCOL_PRI_SMPTE432;
    default: return AVCOL_PRI_UNSPECIFIED;
    }
}

static enum AVColorTransferCharacteristic mf_color_transfer(UINT32 value)
{
    switch (value) {
    case 1: return AVCOL_TRC_LINEAR;
    case 4: return AVCOL_TRC_GAMMA22;
    case 5: return AVCOL_TRC_BT709;
    case 6: return AVCOL_TRC_SMPTE240M;
    case 7: return AVCOL_TRC_IEC61966_2_1;
    case 8: return AVCOL_TRC_GAMMA28;
    case 9: return AVCOL_TRC_LOG;
    case 10: return AVCOL_TRC_LOG_SQRT;
    case 13: return AVCOL_TRC_BT2020_10;
    case 15: return AVCOL_TRC_SMPTE2084;
    case 16: return AVCOL_TRC_ARIB_STD_B67;
    case 18: return AVCOL_TRC_BT1361_ECG;
    case 19: return AVCOL_TRC_SMPTE428;
    default: return AVCOL_TRC_UNSPECIFIED;
    }
}

static enum AVColorSpace mf_color_matrix(UINT32 value)
{
    switch (value) {
    case 1: return AVCOL_SPC_BT709;
    case 2: return AVCOL_SPC_SMPTE170M; /* BT.601 has the same matrix. */
    case 3: return AVCOL_SPC_SMPTE240M;
    case 4: case 5: return AVCOL_SPC_BT2020_NCL;
    case 6: return AVCOL_SPC_RGB;
    case 7: return AVCOL_SPC_FCC;
    case 8: return AVCOL_SPC_YCGCO;
    case 9: return AVCOL_SPC_SMPTE2085;
    case 10: return AVCOL_SPC_CHROMA_DERIVED_NCL;
    case 11: return AVCOL_SPC_CHROMA_DERIVED_CL;
    case 12: return AVCOL_SPC_ICTCP;
    default: return AVCOL_SPC_UNSPECIFIED;
    }
}

static void mf_apply_header_metadata(AVCodecContext *avctx)
{
    MFDecodeContext *c = avctx->priv_data;
    const AVCodecContext *header = c->parser_context;
    if (!header) return;
    /* Exact, explicitly signalled VUI takes priority over MFT approximations.
     * Microsoft HEVC MFTs can report BT.709 as gamma 2.2 and omit the matrix;
     * some H.264 MFTs do not expose sRGB. No pixels are decoded by the parser. */
    if (header->color_primaries != AVCOL_PRI_UNSPECIFIED)
        avctx->color_primaries = header->color_primaries;
    if (header->color_trc != AVCOL_TRC_UNSPECIFIED)
        avctx->color_trc = header->color_trc;
    if (header->colorspace != AVCOL_SPC_UNSPECIFIED)
        avctx->colorspace = header->colorspace;
    if (header->color_range != AVCOL_RANGE_UNSPECIFIED)
        avctx->color_range = header->color_range;
    if (header->chroma_sample_location != AVCHROMA_LOC_UNSPECIFIED)
        avctx->chroma_sample_location = header->chroma_sample_location;
    if (header->sample_aspect_ratio.num > 0 && header->sample_aspect_ratio.den > 0)
        avctx->sample_aspect_ratio = header->sample_aspect_ratio;
}

static void mf_read_video_metadata(AVCodecContext *avctx, IMFMediaType *type)
{
    MFDecodeContext *c = avctx->priv_data;
    UINT32 value, num, den, minimum, maximum, size = 0;
    MT_CUSTOM_VIDEO_PRIMARIES primaries;
    enum AVColorPrimaries pri;
    enum AVColorTransferCharacteristic trc;
    enum AVColorSpace matrix;

    /* Preserve explicit container metadata if the transform omits an attribute. */
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &MF_MT_VIDEO_PRIMARIES, &value)) &&
        (pri = mf_color_primaries(value)) != AVCOL_PRI_UNSPECIFIED)
        avctx->color_primaries = pri;
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &MF_MT_TRANSFER_FUNCTION, &value)) &&
        (trc = mf_color_transfer(value)) != AVCOL_TRC_UNSPECIFIED)
        avctx->color_trc = trc;
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &MF_MT_YUV_MATRIX, &value)) &&
        (matrix = mf_color_matrix(value)) != AVCOL_SPC_UNSPECIFIED)
        avctx->colorspace = matrix;
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &MF_MT_VIDEO_NOMINAL_RANGE, &value))) {
        if (value == 1) avctx->color_range = AVCOL_RANGE_JPEG; /* 0..255 */
        if (value == 2) avctx->color_range = AVCOL_RANGE_MPEG; /* 16..235 */
    }
    if (SUCCEEDED(ff_MFGetAttributeRatio((IMFAttributes *)type,
            &MF_MT_PIXEL_ASPECT_RATIO, &num, &den)) && num && den)
        av_reduce(&avctx->sample_aspect_ratio.num, &avctx->sample_aspect_ratio.den,
                  num, den, INT_MAX);
    mf_apply_header_metadata(avctx);

    memset(&c->mastering, 0, sizeof(c->mastering));
    c->has_content_light = 0;
    if (SUCCEEDED(IMFMediaType_GetBlob(type, &MF_MT_CUSTOM_VIDEO_PRIMARIES,
            (UINT8 *)&primaries, sizeof(primaries), &size)) && size == sizeof(primaries)) {
        const float xy[8] = { primaries.fRx, primaries.fRy, primaries.fGx, primaries.fGy,
                              primaries.fBx, primaries.fBy, primaries.fWx, primaries.fWy };
        int valid = 1;
        for (int i = 0; i < 8; i++)
            if (!(xy[i] >= 0.0f && xy[i] <= 1.0f)) valid = 0; /* also rejects NaN */
        for (int i = 0; i < 4; i++)
            if (!(xy[2*i] + xy[2*i+1] > 0.0f && xy[2*i] + xy[2*i+1] <= 1.0f)) valid = 0;
        if (valid) {
            for (int i = 0; i < 3; i++) for (int j = 0; j < 2; j++)
                c->mastering.display_primaries[i][j] = av_d2q(xy[2*i+j], 50000);
            c->mastering.white_point[0] = av_d2q(xy[6], 50000);
            c->mastering.white_point[1] = av_d2q(xy[7], 50000);
            c->mastering.has_primaries = 1;
        }
    }
    /* SDK units: maximum is whole nits; minimum is 0.0001 nits. */
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &duskcut_mf_max_mastering, &maximum)) &&
        SUCCEEDED(IMFMediaType_GetUINT32(type, &duskcut_mf_min_mastering, &minimum)) &&
        maximum > 0 && maximum <= INT_MAX && minimum <= (uint64_t)maximum * 10000) {
        c->mastering.max_luminance = (AVRational){ maximum, 1 };
        av_reduce(&c->mastering.min_luminance.num, &c->mastering.min_luminance.den,
                  minimum, 10000, INT_MAX);
        c->mastering.has_luminance = 1;
    }
    if (SUCCEEDED(IMFMediaType_GetUINT32(type, &duskcut_mf_max_cll, &maximum)) &&
        SUCCEEDED(IMFMediaType_GetUINT32(type, &duskcut_mf_max_fall, &minimum)) &&
        maximum <= 65535 && minimum <= 65535) {
        c->content_light.MaxCLL = maximum;
        c->content_light.MaxFALL = minimum;
        c->has_content_light = 1;
    }
}

static int mf_apply_hdr_metadata(MFDecodeContext *c, AVFrame *frame)
{
    /* Prefer matching packet/container side data. Only supplement information
     * the MFT actually exposed; absent HDR metadata is not reconstructed. */
    if ((c->mastering.has_primaries || c->mastering.has_luminance) &&
        !av_frame_get_side_data(frame, AV_FRAME_DATA_MASTERING_DISPLAY_METADATA)) {
        AVMasteringDisplayMetadata *metadata = av_mastering_display_metadata_create_side_data(frame);
        if (!metadata) return AVERROR(ENOMEM);
        *metadata = c->mastering;
    }
    if (c->has_content_light && !av_frame_get_side_data(frame, AV_FRAME_DATA_CONTENT_LIGHT_LEVEL)) {
        AVContentLightMetadata *metadata = av_content_light_metadata_create_side_data(frame);
        if (!metadata) return AVERROR(ENOMEM);
        *metadata = c->content_light;
    }
    return 0;
}

static int mf_error(AVCodecContext *avctx, const char *operation, HRESULT hr)
{
    MFDecodeContext *c = avctx->priv_data;
    /* A permanently rejected profile must not spin ProcessOutput forever. */
    c->failed = 1;
    av_log(avctx, AV_LOG_ERROR, "Windows %s failed: %s. "
           "The installed Windows codec may not support this media profile.\n",
           operation, ff_hr_str(hr));
    return AVERROR_EXTERNAL;
}

static void mf_clear_properties(MFDecodeContext *c)
{
    for (int i = 0; i < c->property_count; i++) av_packet_free(&c->properties[i]);
    c->property_count = 0;
}

static int mf_save_properties(MFDecodeContext *c, const AVPacket *packet)
{
    AVPacket *props = av_packet_alloc();
    int ret;
    if (!props) return AVERROR(ENOMEM);
    ret = av_packet_copy_props(props, packet);
    if (ret < 0) { av_packet_free(&props); return ret; }
    /* Bound retained metadata even for a malformed stream producing no frames. */
    if (c->property_count == FF_ARRAY_ELEMS(c->properties)) {
        av_packet_free(&c->properties[0]);
        memmove(c->properties, c->properties + 1, (--c->property_count) * sizeof(*c->properties));
    }
    c->properties[c->property_count++] = props;
    return 0;
}

static int mf_apply_properties(AVCodecContext *avctx, AVFrame *frame, int64_t pts)
{
    MFDecodeContext *c = avctx->priv_data;
    for (int i = 0; i < c->property_count; i++) {
        AVPacket *props = c->properties[i];
        if (props->pts != pts) continue;
        /* Use the matching packet, not the most recently submitted packet:
         * B-frame reorder and AAC priming otherwise attach trimming to the
         * wrong output. Windows can itself suppress the priming packet. */
        int ret = ff_decode_frame_props_from_pkt(avctx, frame, props);
        av_packet_free(&c->properties[i]);
        memmove(c->properties + i, c->properties + i + 1,
                (--c->property_count - i) * sizeof(*c->properties));
        return ret;
    }
    return 0;
}

static AVRational mf_packet_time_base(const AVCodecContext *avctx)
{
    return avctx->pkt_timebase.num > 0 && avctx->pkt_timebase.den > 0
        ? avctx->pkt_timebase : MF_TIME_BASE;
}

static int mf_read_output_type(AVCodecContext *avctx, IMFMediaType *type)
{
    MFDecodeContext *c = avctx->priv_data;
    UINT32 w, h, stride = 0, rate = 0, channels = 0, mask = 0;
    HRESULT hr;

    if (c->audio) {
        enum AVSampleFormat format = ff_media_type_to_sample_fmt((IMFAttributes *)type);
        if (format != AV_SAMPLE_FMT_FLT && format != AV_SAMPLE_FMT_S16)
            return AVERROR(ENOSYS);
        IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_SAMPLES_PER_SECOND, &rate);
        IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_NUM_CHANNELS, &channels);
        IMFMediaType_GetUINT32(type, &MF_MT_AUDIO_CHANNEL_MASK, &mask);
        if (!rate || !channels || channels > 64 || rate > 768000)
            return AVERROR_INVALIDDATA;
        /* Never silently choose the decoder's stereo downmix for multichannel input. */
        if (avctx->ch_layout.nb_channels > 2 && channels != avctx->ch_layout.nb_channels)
            return AVERROR(ENOSYS);
        c->sample_format = format;
        c->sample_rate = rate;
        c->channels = channels;
        c->channel_mask = mask;
    } else {
        enum AVPixelFormat format = ff_media_type_to_pix_fmt((IMFAttributes *)type);
        if (format != AV_PIX_FMT_NV12 && format != AV_PIX_FMT_P010LE)
            return AVERROR(ENOSYS);
        hr = ff_MFGetAttributeSize((IMFAttributes *)type, &MF_MT_FRAME_SIZE, &w, &h);
        if (FAILED(hr) || av_image_check_size(w, h, 0, avctx) < 0)
            return AVERROR_INVALIDDATA;
        IMFMediaType_GetUINT32(type, &MF_MT_DEFAULT_STRIDE, &stride);
        c->width = w;
        c->height = h;
        c->pixel_format = format;
        c->stride = stride ? (int32_t)stride : (int)w * (format == AV_PIX_FMT_P010LE ? 2 : 1);
        if (c->stride < (int)w * (format == AV_PIX_FMT_P010LE ? 2 : 1))
            return AVERROR_INVALIDDATA;
        c->crop_left = c->crop_top = c->crop_right = c->crop_bottom = 0;
        {
            MFVideoArea area;
            UINT32 size = 0;
            hr = IMFMediaType_GetBlob(type, &MF_MT_MINIMUM_DISPLAY_APERTURE,
                                      (UINT8 *)&area, sizeof(area), &size);
            if (SUCCEEDED(hr) && size == sizeof(area) &&
                !area.OffsetX.fract && !area.OffsetY.fract &&
                area.OffsetX.value >= 0 && area.OffsetY.value >= 0 &&
                area.Area.cx > 0 && area.Area.cy > 0 &&
                (int64_t)area.OffsetX.value + area.Area.cx <= w &&
                (int64_t)area.OffsetY.value + area.Area.cy <= h) {
                c->crop_left = area.OffsetX.value;
                c->crop_top = area.OffsetY.value;
                c->crop_right = w - c->crop_left - area.Area.cx;
                c->crop_bottom = h - c->crop_top - area.Area.cy;
            }
        }
    }
    return 0;
}

static int mf_choose_output(AVCodecContext *avctx)
{
    MFDecodeContext *c = avctx->priv_data;
    HRESULT hr = MF_E_NO_MORE_TYPES;
    int pass;
    /* Prefer float audio and the source bit depth. A 10-bit source is never
     * silently negotiated to NV12; colour conversion remains FFmpeg's job. */
    const AVPixFmtDescriptor *desc = av_pix_fmt_desc_get(c->source_format);
    int ten_bit = (desc && desc->comp[0].depth > 8) ||
        (avctx->codec_id == AV_CODEC_ID_HEVC && avctx->profile == 2);
    for (pass = 0; pass < (c->audio ? 2 : 1); pass++) {
        DWORD index;
        for (index = 0; index < 256; index++) {
            IMFMediaType *type = NULL;
            int matches;
            hr = IMFTransform_GetOutputAvailableType(c->mft, c->output_id, index, &type);
            if (FAILED(hr)) break;
            if (c->audio) {
                enum AVSampleFormat fmt = ff_media_type_to_sample_fmt((IMFAttributes *)type);
                matches = fmt == (pass ? AV_SAMPLE_FMT_S16 : AV_SAMPLE_FMT_FLT);
            } else {
                enum AVPixelFormat fmt = ff_media_type_to_pix_fmt((IMFAttributes *)type);
                matches = fmt == (ten_bit ? AV_PIX_FMT_P010LE : AV_PIX_FMT_NV12);
            }
            if (matches && mf_read_output_type(avctx, type) >= 0) {
                hr = IMFTransform_SetOutputType(c->mft, c->output_id, type, 0);
                IMFMediaType_Release(type);
                if (SUCCEEDED(hr)) {
                    if (!c->audio) {
                        IMFMediaType *current = NULL;
                        /* Read negotiated attributes, not the enumerated proposal. */
                        if (SUCCEEDED(IMFTransform_GetOutputCurrentType(c->mft, c->output_id, &current))) {
                            mf_read_video_metadata(avctx, current);
                            IMFMediaType_Release(current);
                        }
                    }
                    hr = IMFTransform_GetOutputStreamInfo(c->mft, c->output_id, &c->output_info);
                    return SUCCEEDED(hr) ? 0 : mf_error(avctx, "output buffer negotiation", hr);
                }
            } else {
                IMFMediaType_Release(type);
            }
        }
    }
    return mf_error(avctx, "decoder output negotiation", hr);
}

/* Read only LATM transport configuration. Compressed audio always stays in the
 * Windows transform. One program/layer is also the Windows decoder's limit. */
static int mf_latm_config(AVCodecContext *avctx, const AVPacket *packet, MPEG4AudioConfig *config)
{
    GetBitContext gb;
    int version, bytes;
    if (packet->size < 8 || AV_RB24(packet->data) >> 13 != 0x2b7 ||
        init_get_bits8(&gb, packet->data, packet->size) < 0)
        return AVERROR_INVALIDDATA;
    skip_bits(&gb, 24);
    if (get_bits1(&gb)) return AVERROR_INVALIDDATA; /* No StreamMuxConfig yet. */
    version = get_bits1(&gb);
    if (version) {
        if (get_bits1(&gb)) return AVERROR(ENOSYS); /* audioMuxVersionA */
        bytes = get_bits(&gb, 2) + 1;
        if (get_bits_left(&gb) < bytes * 8 + 14) return AVERROR_INVALIDDATA;
        skip_bits_long(&gb, bytes * 8); /* taraBufferFullness */
    }
    skip_bits(&gb, 7); /* allStreamsSameTimeFraming, numSubFrames */
    if (get_bits(&gb, 4) || get_bits(&gb, 3)) return AVERROR(ENOSYS);
    if (version) {
        bytes = get_bits(&gb, 2) + 1;
        if (get_bits_left(&gb) < bytes * 8 + 16) return AVERROR_INVALIDDATA;
        skip_bits_long(&gb, bytes * 8); /* ASC length, MFT consumes complete config. */
    }
    return ff_mpeg4audio_get_config_gb(config, &gb, 0, avctx);
}

static int mf_parse_video_header(AVCodecContext *avctx, const AVPacket *packet)
{
    MFDecodeContext *c = avctx->priv_data;
    if (!c->audio && c->parser && packet) {
        uint8_t *parsed;
        int parsed_size, ret;
        const AVPixFmtDescriptor *desc;
        /* The decoder input BSF already converted the packet to Annex B.
         * Use a separate header-only parser context without the original avcC/
         * hvcC extradata so the parser does not mistake it for length prefixes.
         * MOV demuxing alone does not populate profile/bit depth reliably. */
        ret = av_parser_parse2(c->parser, c->parser_context, &parsed, &parsed_size,
                          packet->data, packet->size, packet->pts, packet->dts, packet->pos);
        if (ret < 0) { c->failed = 1; return ret; }
        if (c->parser->format >= 0) c->source_format = c->parser->format;
        if (c->parser_context->profile >= 0) avctx->profile = c->parser_context->profile;
        if (c->parser_context->level >= 0) avctx->level = c->parser_context->level;
        mf_apply_header_metadata(avctx);
        if (c->parser->width > 0 && c->parser->height > 0) {
            avctx->width = c->parser->width;
            avctx->height = c->parser->height;
        }
        desc = av_pix_fmt_desc_get(c->source_format);
        if (desc && (desc->comp[0].depth > (avctx->codec_id == AV_CODEC_ID_H264 ? 8 : 10) ||
                     desc->log2_chroma_w != 1 || desc->log2_chroma_h != 1 || desc->nb_components != 3)) {
            av_log(avctx, AV_LOG_ERROR, "DUSKCUT_MF_PROFILE_UNSUPPORTED: Windows decoder does not support this source bit depth or chroma format.\n");
            c->failed = 1;
            return AVERROR(ENOSYS);
        }
    }
    return 0;
}

static int mf_set_input(AVCodecContext *avctx, const AVPacket *packet)
{
    MFDecodeContext *c = avctx->priv_data;
    IMFMediaType *type = NULL;
    const GUID *subtype = avctx->codec_id == AV_CODEC_ID_AAC_LATM
        ? &MFAudioFormat_AAC : ff_codec_to_mf_subtype(avctx->codec_id);
    HRESULT hr;
    int ret = 0;

#define SET_ATTR(call) do { hr = (call); if (FAILED(hr)) goto fail; } while (0)
    SET_ATTR(c->functions.MFCreateMediaType(&type));
    SET_ATTR(IMFMediaType_SetGUID(type, &MF_MT_MAJOR_TYPE,
                                 c->audio ? &MFMediaType_Audio : &MFMediaType_Video));
    SET_ATTR(IMFMediaType_SetGUID(type, &MF_MT_SUBTYPE, subtype));
    if (c->audio) {
        int rate = avctx->sample_rate, channels = avctx->ch_layout.nb_channels;
        const uint8_t *extra = avctx->extradata;
        size_t extra_size = avctx->extradata_size;
        size_t packet_extra_size = 0;
        const uint8_t *packet_extra = packet ? av_packet_get_side_data(packet,
            AV_PKT_DATA_NEW_EXTRADATA, &packet_extra_size) : NULL;
        int payload = avctx->codec_id == AV_CODEC_ID_AAC_LATM ? 3 :
            (packet && packet->size >= 2 && packet->data[0] == 0xff &&
             (packet->data[1] & 0xf6) == 0xf0 ? 1 : 0);
        MPEG4AudioConfig config;
        /* aac_adtstoasc only removes transport framing; Windows still decodes
         * every compressed sample. ADTS stores its AudioSpecificConfig on the
         * first output packet rather than in the container codec parameters. */
        if (packet_extra && packet_extra_size) {
            if (packet_extra_size > 65536) { ret = AVERROR_INVALIDDATA; goto done; }
            /* Stream-info probing creates a temporary decoder. Publish the
             * transport-derived ASC so later seek/export decoder contexts have
             * the same initialization data, not only the very first packet. */
            if (packet_extra_size != avctx->extradata_size ||
                !avctx->extradata || memcmp(avctx->extradata, packet_extra, packet_extra_size)) {
                uint8_t *copy = av_mallocz(packet_extra_size + AV_INPUT_BUFFER_PADDING_SIZE);
                if (!copy) { ret = AVERROR(ENOMEM); goto done; }
                memcpy(copy, packet_extra, packet_extra_size);
                av_freep(&avctx->extradata);
                avctx->extradata = copy;
                avctx->extradata_size = packet_extra_size;
            }
            extra = avctx->extradata;
            extra_size = avctx->extradata_size;
        }
        if (payload == 1 && packet->size >= AV_AAC_ADTS_HEADER_SIZE) {
            AACADTSHeaderInfo header;
            if (ff_adts_header_parse_buf(packet->data, &header) < 0) {
                ret = AVERROR_INVALIDDATA;
                goto done;
            }
            rate = header.sample_rate;
            if (header.chan_config > 0 && header.chan_config < FF_ARRAY_ELEMS(ff_mpeg4audio_channels))
                channels = ff_mpeg4audio_channels[header.chan_config];
        }
        if (extra_size > 65536) { ret = AVERROR_INVALIDDATA; goto done; }
        if (!payload && extra_size > 0 &&
            avpriv_mpeg4audio_get_config2(&config, extra, extra_size, 1, avctx) >= 0) {
            rate = config.sample_rate;
            channels = config.channels;
        }
        if (payload == 3 && packet && mf_latm_config(avctx, packet, &config) >= 0) {
            rate = config.sample_rate;
            channels = config.channels;
        }
        SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_AAC_PAYLOAD_TYPE, payload));
        SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_AAC_AUDIO_PROFILE_LEVEL_INDICATION, 0xfe));
        if (rate > 0) SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_SAMPLES_PER_SECOND, rate));
        if (channels > 0) SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_NUM_CHANNELS, channels));
        SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_AUDIO_BITS_PER_SAMPLE, 32));
        if (payload || extra_size > 0) {
            uint8_t *user_data;
            const size_t asc_size = payload ? 0 : extra_size;
            user_data = av_mallocz(12 + asc_size);
            if (!user_data) { ret = AVERROR(ENOMEM); goto done; }
            /* HEAACWAVEINFO after WAVEFORMATEX, followed by AudioSpecificConfig. */
            AV_WL16(user_data, payload);
            AV_WL16(user_data + 2, 0xfe);
            if (asc_size) memcpy(user_data + 12, extra, asc_size);
            hr = IMFMediaType_SetBlob(type, &MF_MT_USER_DATA, user_data, 12 + asc_size);
            av_free(user_data);
            if (FAILED(hr)) goto fail;
        }
    } else {
        if (avctx->width > 0 && avctx->height > 0)
            SET_ATTR(ff_MFSetAttributeSize((IMFAttributes *)type, &MF_MT_FRAME_SIZE,
                                          avctx->width, avctx->height));
        if (avctx->framerate.num > 0 && avctx->framerate.den > 0)
            SET_ATTR(ff_MFSetAttributeRatio((IMFAttributes *)type, &MF_MT_FRAME_RATE,
                                           avctx->framerate.num, avctx->framerate.den));
        if (avctx->profile >= 0)
            SET_ATTR(IMFMediaType_SetUINT32(type, &MF_MT_MPEG2_PROFILE, avctx->profile));
        if (avctx->sample_aspect_ratio.num > 0 && avctx->sample_aspect_ratio.den > 0)
            SET_ATTR(ff_MFSetAttributeRatio((IMFAttributes *)type, &MF_MT_PIXEL_ASPECT_RATIO,
                                           avctx->sample_aspect_ratio.num, avctx->sample_aspect_ratio.den));
    }
    SET_ATTR(IMFTransform_SetInputType(c->mft, c->input_id, type, 0));
    ret = mf_choose_output(avctx);
    if (ret < 0) goto done;
    SET_ATTR(IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0));
    SET_ATTR(IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0));
    c->configured = 1;
    goto done;
fail:
    ret = mf_error(avctx, "decoder input negotiation", hr);
done:
    if (type) IMFMediaType_Release(type);
    return ret;
#undef SET_ATTR
}

static int mf_copy_sample(AVCodecContext *avctx, IMFSample *sample, AVFrame *frame)
{
    MFDecodeContext *c = avctx->priv_data;
    IMFMediaBuffer *buffer = NULL;
    BYTE *data = NULL;
    DWORD length = 0;
    LONGLONG time;
    HRESULT hr;
    int ret;

    ret = ff_decode_frame_props(avctx, frame);
    if (ret < 0) return ret;
    hr = IMFSample_ConvertToContiguousBuffer(sample, &buffer);
    if (FAILED(hr)) return mf_error(avctx, "sample buffer", hr);
    hr = IMFMediaBuffer_Lock(buffer, &data, NULL, &length);
    if (FAILED(hr)) { ret = mf_error(avctx, "sample lock", hr); goto done; }
    if (c->audio) {
        const int block = av_get_bytes_per_sample(c->sample_format) * c->channels;
        if (!block || !length || length % block) { ret = AVERROR_INVALIDDATA; goto done; }
        frame->format = c->sample_format;
        frame->sample_rate = c->sample_rate;
        frame->nb_samples = length / block;
        av_channel_layout_uninit(&frame->ch_layout);
        if (c->channel_mask) {
            ret = av_channel_layout_from_mask(&frame->ch_layout, c->channel_mask);
            if (ret < 0) goto done;
        } else av_channel_layout_default(&frame->ch_layout, c->channels);
        if (frame->ch_layout.nb_channels != c->channels) { ret = AVERROR_INVALIDDATA; goto done; }
        avctx->sample_fmt = c->sample_format;
        avctx->sample_rate = c->sample_rate;
        /* Raw AAC/LATM demuxers use the decoded frame size to derive packet
         * durations. Leaving this unset makes raw ADTS timestamps unknown and
         * an input-side seek can consume the entire file without producing audio. */
        avctx->frame_size = frame->nb_samples;
        av_channel_layout_uninit(&avctx->ch_layout);
        ret = av_channel_layout_copy(&avctx->ch_layout, &frame->ch_layout);
        if (ret < 0) goto done;
        ret = ff_get_buffer(avctx, frame, 0);
        if (ret < 0) goto done;
        memcpy(frame->data[0], data, length);
    } else {
        const int row_bytes = c->width * (c->pixel_format == AV_PIX_FMT_P010LE ? 2 : 1);
        const int chroma_rows = (c->height + 1) / 2;
        const int chroma_bytes = ((c->width + 1) / 2) * (c->pixel_format == AV_PIX_FMT_P010LE ? 4 : 2);
        const uint64_t needed = (uint64_t)c->stride * (c->height + chroma_rows - 1) + chroma_bytes;
        if (c->stride < chroma_bytes || needed > length) { ret = AVERROR_INVALIDDATA; goto done; }
        avctx->pix_fmt = c->pixel_format;
        ret = ff_set_dimensions(avctx, c->width, c->height);
        if (ret < 0) goto done;
        avctx->width = c->width - c->crop_left - c->crop_right;
        avctx->height = c->height - c->crop_top - c->crop_bottom;
        frame->format = c->pixel_format;
        frame->width = c->width;
        frame->height = c->height;
        ret = ff_get_buffer(avctx, frame, 0);
        if (ret < 0) goto done;
        av_image_copy_plane(frame->data[0], frame->linesize[0], data, c->stride, row_bytes, c->height);
        av_image_copy_plane(frame->data[1], frame->linesize[1], data + (size_t)c->stride * c->height,
                            c->stride, chroma_bytes, chroma_rows);
        frame->crop_left = c->crop_left;
        frame->crop_top = c->crop_top;
        frame->crop_right = c->crop_right;
        frame->crop_bottom = c->crop_bottom;
    }
    frame->pts = SUCCEEDED(IMFSample_GetSampleTime(sample, &time))
        ? av_rescale_q(time, MF_TIME_BASE, mf_packet_time_base(avctx)) : AV_NOPTS_VALUE;
    ret = mf_apply_properties(avctx, frame, frame->pts);
    if (ret < 0) goto done;
    if (!c->audio) {
        ret = mf_apply_hdr_metadata(c, frame);
        if (ret < 0) goto done;
    }
    if (SUCCEEDED(IMFSample_GetSampleDuration(sample, &time)))
        frame->duration = av_rescale_q(time, MF_TIME_BASE, mf_packet_time_base(avctx));
    else if (c->audio)
        frame->duration = av_rescale_q(frame->nb_samples, (AVRational){1, c->sample_rate},
                                       mf_packet_time_base(avctx));
    frame->time_base = mf_packet_time_base(avctx);
    ret = 0;
done:
    if (data) IMFMediaBuffer_Unlock(buffer);
    if (buffer) IMFMediaBuffer_Release(buffer);
    return ret;
}

static int mf_output(AVCodecContext *avctx, AVFrame *frame)
{
    MFDecodeContext *c = avctx->priv_data;
    MFT_OUTPUT_DATA_BUFFER output = {0};
    IMFSample *provided = NULL;
    DWORD status = 0;
    HRESULT hr;
    int ret;

    output.dwStreamID = c->output_id;
    if (!(c->output_info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES)) {
        size_t size = c->output_info.cbSize;
        if (!size) {
            if (c->audio) size = (size_t)c->sample_rate * c->channels * 4;
            else size = (size_t)c->stride * (c->height + (c->height + 1) / 2);
        }
        if (!size || size > 512U * 1024 * 1024) return AVERROR_INVALIDDATA;
        provided = ff_create_memory_sample(&c->functions, NULL, size, c->output_info.cbAlignment);
        if (!provided) return AVERROR(ENOMEM);
        output.pSample = provided;
    }
    hr = IMFTransform_ProcessOutput(c->mft, 0, 1, &output, &status);
    if (output.pEvents) IMFCollection_Release(output.pEvents);
    if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) ret = AVERROR(EAGAIN);
    else if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
        ret = mf_choose_output(avctx);
        if (ret >= 0) ret = AVERROR_INPUT_CHANGED;
    } else if (FAILED(hr)) ret = mf_error(avctx, "decode", hr);
    else if (!output.pSample) ret = AVERROR(EAGAIN);
    else ret = mf_copy_sample(avctx, output.pSample, frame);
    if (output.pSample && output.pSample != provided) IMFSample_Release(output.pSample);
    if (provided) IMFSample_Release(provided);
    return ret;
}

static int mf_receive_frame(AVCodecContext *avctx, AVFrame *frame)
{
    MFDecodeContext *c = avctx->priv_data;
    int ret, changes = 0;
    HRESULT hr;

    if (c->eof) return AVERROR_EOF;
    if (c->failed) {
        /* Drain compressed packets after reporting a fatal MFT error. Returning
         * EOF while libavcodec still has a queued input violates send/receive
         * progress guarantees and can hang avformat_find_stream_info. */
        do {
            av_packet_unref(c->packet);
            ret = ff_decode_get_packet(avctx, c->packet);
        } while (ret >= 0);
        return ret;
    }
    for (;;) {
        if (c->configured) {
            ret = mf_output(avctx, frame);
            if (ret == AVERROR_INPUT_CHANGED) {
                if (++changes > 8) return AVERROR_INVALIDDATA;
                continue;
            }
            if (ret != AVERROR(EAGAIN)) return ret;
            if (c->draining) { c->eof = 1; return AVERROR_EOF; }
        }
        if (!c->packet->buf) {
            ret = ff_decode_get_packet(avctx, c->packet);
            if (ret == AVERROR_EOF) {
                c->draining = 1;
                if (!c->configured) { c->eof = 1; return AVERROR_EOF; }
                hr = IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_NOTIFY_END_OF_STREAM, c->input_id);
                if (FAILED(hr)) return mf_error(avctx, "end of stream", hr);
                hr = IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_COMMAND_DRAIN, 0);
                if (FAILED(hr)) return mf_error(avctx, "drain", hr);
                continue;
            }
            if (ret < 0) return ret;
            /* Refresh VUI/profile for each new access unit. A later stream-change
             * must choose P010 for new 10-bit content, never retain stale NV12.
             * The retained packet is not parsed again after MFT backpressure. */
            ret = mf_parse_video_header(avctx, c->packet);
            if (ret < 0) return ret;
        }
        if (!c->configured) {
            ret = mf_set_input(avctx, c->packet);
            if (ret < 0) return ret;
        }
        {
            IMFSample *sample = ff_create_memory_sample(&c->functions, c->packet->data,
                                                        c->packet->size, 0);
            if (!sample) return AVERROR(ENOMEM);
            if (c->packet->pts != AV_NOPTS_VALUE)
                IMFSample_SetSampleTime(sample, av_rescale_q(c->packet->pts,
                                         mf_packet_time_base(avctx), MF_TIME_BASE));
            if (c->packet->duration > 0)
                IMFSample_SetSampleDuration(sample, av_rescale_q(c->packet->duration,
                                             mf_packet_time_base(avctx), MF_TIME_BASE));
            if (c->packet->flags & AV_PKT_FLAG_KEY)
                IMFSample_SetUINT32(sample, &MFSampleExtension_CleanPoint, TRUE);
            if (c->discontinuity)
                IMFSample_SetUINT32(sample, &MFSampleExtension_Discontinuity, TRUE);
            hr = IMFTransform_ProcessInput(c->mft, c->input_id, sample, 0);
            IMFSample_Release(sample);
            /* Keep the packet on backpressure; never drop the first syllable/frame. */
            if (hr == MF_E_NOTACCEPTING) return AVERROR(EAGAIN);
            if (FAILED(hr)) return mf_error(avctx, "compressed sample input", hr);
            ret = mf_save_properties(c, c->packet);
            c->discontinuity = 0;
            av_packet_unref(c->packet);
            if (ret < 0) { c->failed = 1; return ret; }
        }
    }
}

static void mf_flush(AVCodecContext *avctx)
{
    MFDecodeContext *c = avctx->priv_data;
    av_packet_unref(c->packet);
    mf_clear_properties(c);
    c->draining = c->eof = c->failed = 0;
    c->discontinuity = 1;
    if (c->mft && c->configured) {
        IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_COMMAND_FLUSH, 0);
        IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
    }
}

static av_cold int mf_close_decoder(AVCodecContext *avctx)
{
    MFDecodeContext *c = avctx->priv_data;
    if (c->mft) {
        IMFTransform_ProcessMessage(c->mft, MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
        ff_free_mf(&c->functions, &c->mft);
    }
    av_packet_free(&c->packet);
    av_parser_close(c->parser);
    avcodec_free_context(&c->parser_context);
    mf_clear_properties(c);
#if !HAVE_UWP
    if (c->library) dlclose(c->library);
#endif
    c->library = NULL;
    return 0;
}

static av_cold int mf_init_decoder(AVCodecContext *avctx)
{
    MFDecodeContext *c = avctx->priv_data;
    MFT_REGISTER_TYPE_INFO input;
    const GUID *subtype = avctx->codec_id == AV_CODEC_ID_AAC_LATM
        ? &MFAudioFormat_AAC : ff_codec_to_mf_subtype(avctx->codec_id);
    HRESULT hr;
    int ret;
    if (!subtype) return AVERROR(ENOSYS);
    c->audio = avctx->codec_type == AVMEDIA_TYPE_AUDIO;
    c->source_format = avctx->pix_fmt;
    if (!c->audio) {
        c->parser = av_parser_init(avctx->codec_id);
        c->parser_context = avcodec_alloc_context3(NULL);
        if (!c->parser || !c->parser_context) return AVERROR(ENOMEM);
        c->parser_context->codec_id = avctx->codec_id;
        c->parser_context->codec_type = AVMEDIA_TYPE_VIDEO;
        c->parser->flags |= PARSER_FLAG_COMPLETE_FRAMES;
    }
    c->discontinuity = 1;
    c->packet = av_packet_alloc();
    if (!c->packet) return AVERROR(ENOMEM);
#if !HAVE_UWP
    c->library = dlopen("mfplat.dll", 0);
    if (!c->library) {
        av_log(avctx, AV_LOG_ERROR, "DUSKCUT_MF_UNAVAILABLE: Install the Windows Media Feature Pack.\n");
        return AVERROR(ENOSYS);
    }
#define LOAD(name) do { c->functions.name = (void *)dlsym(c->library, #name); \
    if (!c->functions.name) { av_log(avctx, AV_LOG_ERROR, "DUSKCUT_MF_UNAVAILABLE: Missing Windows Media Foundation entry point " #name "\n"); \
        return AVERROR(ENOSYS); } } while (0)
#else
#define LOAD(name) c->functions.name = name
#endif
    LOAD(MFStartup);
    LOAD(MFShutdown);
    LOAD(MFCreateAlignedMemoryBuffer);
    LOAD(MFCreateSample);
    LOAD(MFCreateMediaType);
    LOAD(MFTEnumEx);
#undef LOAD
    input.guidMajorType = c->audio ? MFMediaType_Audio : MFMediaType_Video;
    input.guidSubtype = *subtype;
    ret = ff_instantiate_mf(avctx, &c->functions,
                            c->audio ? MFT_CATEGORY_AUDIO_DECODER : MFT_CATEGORY_VIDEO_DECODER,
                            &input, NULL, 0, &c->mft);
    if (ret < 0) {
        av_log(avctx, AV_LOG_ERROR, "DUSKCUT_MF_CODEC_UNAVAILABLE: No installed Windows %s decoder.\n",
               avcodec_get_name(avctx->codec_id));
        return ret;
    }
    hr = IMFTransform_GetStreamIDs(c->mft, 1, &c->input_id, 1, &c->output_id);
    if (hr == E_NOTIMPL) c->input_id = c->output_id = 0;
    else if (FAILED(hr)) return mf_error(avctx, "stream IDs", hr);
    return 0;
}

#define MF_DECODER(NAME, ID, TYPE, BSFS) \
const FFCodec ff_ ## NAME ## _mf_decoder = { \
    .p.name = #NAME "_mf", \
    CODEC_LONG_NAME(#ID " via installed Windows Media Foundation codec"), \
    .p.type = AVMEDIA_TYPE_ ## TYPE, \
    .p.id = AV_CODEC_ID_ ## ID, \
    .priv_data_size = sizeof(MFDecodeContext), \
    .init = mf_init_decoder, \
    .close = mf_close_decoder, \
    .flush = mf_flush, \
    FF_CODEC_RECEIVE_FRAME_CB(mf_receive_frame), \
    .p.capabilities = AV_CODEC_CAP_DELAY | AV_CODEC_CAP_CHANNEL_CONF, \
    .caps_internal = FF_CODEC_CAP_INIT_CLEANUP | FF_CODEC_CAP_SETS_FRAME_PROPS, \
    .bsfs = BSFS, \
    .p.wrapper_name = "mediafoundation", \
};

MF_DECODER(aac, AAC, AUDIO, "aac_adtstoasc")
MF_DECODER(aac_latm, AAC_LATM, AUDIO, NULL)
MF_DECODER(h264, H264, VIDEO, "h264_mp4toannexb")
MF_DECODER(hevc, HEVC, VIDEO, "hevc_mp4toannexb")
