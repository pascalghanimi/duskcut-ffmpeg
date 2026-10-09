"""Windows-only codec policy and exact patch provenance, without compiling."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


policy = load("verify_codec_profile")
patcher = load("apply_windows_codecs")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads(Path(__file__).with_name("profile.json").read_text())
        self.configs = {"config.h": b"#define CONFIG_MEDIAFOUNDATION 1\n",
                        "config_components.h": "".join("#define CONFIG_" + name + " 1\n"
                            for name in policy.REQUIRED).encode()}

    def test_selected_profile_preserves_effects_and_open_media(self):
        audit = policy.verify(self.profile, self.configs)
        self.assertEqual(audit["forbiddenEnabled"], [])
        self.assertIn("RUBBERBAND_FILTER", audit["requiredEnabled"])
        self.assertIn("VIDSTABTRANSFORM_FILTER", audit["requiredEnabled"])
        self.assertIn("LIBVPX_VP9_DECODER", audit["requiredEnabled"])
        self.assertEqual(len(self.profile["sourceStages"]), 39)
        self.assertNotIn("x264", self.profile["rootDependencies"])
        self.assertNotIn("x265", self.profile["rootDependencies"])

    def test_bundled_decoders_gpu_wrappers_and_removed_formats_fail_closed(self):
        for name in ("H264_DECODER", "HEVC_DECODER", "AAC_FIXED_DECODER", "AAC_LATM_DECODER",
                     "LIBX264_ENCODER", "LIBX264RGB_ENCODER", "LIBX265_ENCODER", "LIBFDK_AAC_ENCODER", "HEVC_MF_ENCODER",
                     "H264_D3D11VA_HWACCEL", "HEVC_CUVID_DECODER", "H264_QSV_ENCODER",
                     "AV1_NVENC_ENCODER", "AV1_AMF_ENCODER", "VP9_QSV_ENCODER",
                     "PRORES_KS_ENCODER", "PRORES_RAW_DECODER", "PRORES_VIDEOTOOLBOX_ENCODER",
                     "WMV3IMAGE_DECODER", "VC1_CUVID_DECODER", "LIBDVDREAD"):
            with self.subTest(name=name):
                configs = {**self.configs, "config_components.h": self.configs["config_components.h"] +
                           ("#define CONFIG_" + name + " 1\n").encode()}
                with self.assertRaisesRegex(ValueError, "Forbidden bundled"):
                    policy.verify(self.profile, configs)

    def test_disabling_windows_wrapper_or_effect_cannot_silently_pass(self):
        for name in policy.REQUIRED:
            configs = {key: value.replace(("#define CONFIG_" + name + " 1").encode(),
                                          ("#define CONFIG_" + name + " 0").encode())
                       for key, value in self.configs.items()}
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Required"):
                policy.verify(self.profile, configs)

    def test_conflicting_duplicate_config_and_duplicate_sources_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            policy.verify(self.profile, {**self.configs, "other": b"#define CONFIG_GPL 0\n"})
        changed = copy.deepcopy(self.profile)
        changed["sourceStages"].append(changed["sourceStages"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            policy.verify(changed, self.configs)

    def test_windows_codecs_are_reenabled_after_broad_exclusions(self):
        flags = self.profile["configureFlags"]
        decoder_disable = next(i for i, flag in enumerate(flags) if flag.startswith("--disable-decoder="))
        decoder_enable = next(i for i, flag in enumerate(flags) if flag.startswith("--enable-decoder="))
        self.assertLess(decoder_disable, decoder_enable)
        self.assertEqual(set(flags[decoder_enable].partition("=")[2].split(",")),
                         {"h264_mf", "hevc_mf", "aac_mf", "aac_latm_mf"})


OWN = Path(__file__).parent / "windows-codecs"
# Only the pinned anchors windows-codecs/apply.py replaces, each exactly once.
ORIGINAL = {'configure': 'aac_mf_encoder_deps="mediafoundation"\n',
            'libavcodec/Makefile': "# codec objects\n",
            'libavcodec/allcodecs.c': "extern const FFCodec ff_aac_mf_encoder;\n",
            'libavcodec/h264_parser.c': "            avctx->profile = ff_h264_get_profile(sps);\n",
            'libavcodec/hevc/parser.c': "    avctx->profile  = sps->ptl.general_ptl.profile_idc;\n",
            'libavcodec/mf_utils.c': """call1(s, sizeof(s), NULL);
call2(s, sizeof(s), NULL);
    hr = f->MFCreateAlignedMemoryBuffer(size, align - 1, &buffer);
    if (FAILED(hr))
        return NULL;
        IMFMediaBuffer_SetCurrentLength(buffer, size);
        IMFMediaBuffer_Unlock(buffer);
    IMFSample_AddBuffer(sample, buffer);
    IMFMediaBuffer_Release(buffer);

    return sample;
""",
            'libavcodec/bsf/h264_mp4toannexb.c': """#include "libavutil/mem.h"
typedef struct H264BSFContext {
    uint8_t *sps;
    s->extradata_parsed = 1;
    uint8_t unit_type, new_idr, sps_seen, pps_seen;
        new_idr  = s->new_idr;
            count_or_copy(&out, &out_size, buf, nal_size, ps, j);
    s->new_idr      = new_idr;
    s->idr_sps_seen = sps_seen;
    s->new_idr      = s->extradata_parsed;
const FFBitStreamFilter ff_h264_mp4toannexb_bsf = {
    .p.name         = "h264_mp4toannexb",
"""}


class PatchTests(unittest.TestCase):
    def fixture(self, root, apply_script=None):
        source, controls, evidence = (root / name for name in ("source", "controls", "evidence"))
        for directory in (source / "libavcodec/hevc", source / "libavcodec/bsf", controls / "windows-codecs"):
            directory.mkdir(parents=True)
        for name, data in ORIGINAL.items():
            (source / name).write_text(data)
        for name in ("apply.py", "mfdec.c"):
            shutil.copyfile(OWN / name, controls / "windows-codecs" / name)
        if apply_script is not None:
            (controls / "windows-codecs/apply.py").write_text(apply_script)
        return source, controls, evidence

    def test_recorded_files_match_fixture_and_adapter_is_the_double_patch_sentinel(self):
        self.assertEqual(set(patcher.SOURCE_FILES), set(ORIGINAL) | {patcher.ADAPTER})
        self.assertEqual(len(patcher.SOURCE_FILES), 8)
        self.assertEqual(patcher.SOURCE_FILES[-1], "libavcodec/mfdec.c")
        self.assertEqual(patcher.ADAPTER, "libavcodec/mfdec.c")

    def test_exact_adapter_source_and_patch_are_retained_and_double_patch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="duskcut-mf-patch-") as root:
            source, controls, evidence = self.fixture(Path(root))
            patcher.apply(source, controls, evidence)
            self.assertEqual((source / "libavcodec/mfdec.c").read_bytes(), (OWN / "mfdec.c").read_bytes())
            record = json.loads((evidence / "windows-codecs-source.json").read_text())
            self.assertEqual([row["file"] for row in record["files"]], list(patcher.SOURCE_FILES))
            self.assertEqual(len(record["files"]), 8)
            self.assertTrue(all(row["beforeSha256"] for row in record["files"][:-1]))
            self.assertIsNone(record["files"][-1]["beforeSha256"])
            patch = (evidence / "windows-codecs.patch").read_text()
            self.assertIn("--- /dev/null", patch)
            self.assertIn("+++ b/libavcodec/mfdec.c", patch)
            self.assertIn("h264_mf_decoder_deps", patch)
            for name in ("libavcodec/h264_parser.c", "libavcodec/hevc/parser.c"):
                self.assertIn("+++ b/" + name, patch)
                self.assertIn("avctx->color_primaries = vui->colour_primaries;", (source / name).read_text())
            self.assertIn("--- a/libavcodec/bsf/h264_mp4toannexb.c", patch)
            self.assertIn("+++ b/libavcodec/bsf/h264_mp4toannexb.c", patch)
            with self.assertRaisesRegex(ValueError, "already exists"):
                patcher.apply(source, controls, evidence)

    def test_patch_that_leaves_a_recorded_file_unchanged_is_refused(self):
        # A controls script that adds the adapter and edits every other file but the
        # bitstream filter: the build must stop instead of silently dropping that change.
        skipped = "libavcodec/bsf/h264_mp4toannexb.c"
        script = "\n".join((
            "import pathlib, sys",
            "root, own = pathlib.Path(sys.argv[1]), pathlib.Path(__file__).parent",
            "for name in %r:" % [name for name in patcher.SOURCE_FILES if name not in (skipped, patcher.ADAPTER)],
            "    (root / name).write_text((root / name).read_text() + '/* patched */\\n')",
            "(root / %r).write_bytes((own / 'mfdec.c').read_bytes())" % patcher.ADAPTER,
            ""))
        with tempfile.TemporaryDirectory(prefix="duskcut-mf-patch-") as root:
            source, controls, evidence = self.fixture(Path(root), script)
            with self.assertRaisesRegex(ValueError, "not applied: " + skipped):
                patcher.apply(source, controls, evidence)
            self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
