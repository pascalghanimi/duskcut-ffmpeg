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


class PatchTests(unittest.TestCase):
    def test_exact_adapter_source_and_patch_are_retained_and_double_patch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="duskcut-mf-patch-") as root:
            root = Path(root)
            source, controls, evidence = (root / name for name in ("source", "controls", "evidence"))
            (source / "libavcodec/hevc").mkdir(parents=True)
            (controls / "windows-codecs").mkdir(parents=True)
            original = {'configure': 'aac_mf_encoder_deps="mediafoundation"\n',
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
"""}
            for name, data in original.items():
                (source / name).write_text(data)
            own = Path(__file__).parent / "windows-codecs"
            for name in ("apply.py", "mfdec.c"):
                shutil.copyfile(own / name, controls / "windows-codecs" / name)
            patcher.apply(source, controls, evidence)
            self.assertEqual((source / "libavcodec/mfdec.c").read_bytes(), (own / "mfdec.c").read_bytes())
            record = json.loads((evidence / "windows-codecs-source.json").read_text())
            self.assertEqual(len(record["files"]), 7)
            patch = (evidence / "windows-codecs.patch").read_text()
            self.assertIn("--- /dev/null", patch)
            self.assertIn("+++ b/libavcodec/mfdec.c", patch)
            self.assertIn("h264_mf_decoder_deps", patch)
            for name in ("libavcodec/h264_parser.c", "libavcodec/hevc/parser.c"):
                self.assertIn("+++ b/" + name, patch)
                self.assertIn("avctx->color_primaries = vui->colour_primaries;", (source / name).read_text())
            with self.assertRaisesRegex(ValueError, "already exists"):
                patcher.apply(source, controls, evidence)


if __name__ == "__main__":
    unittest.main()
