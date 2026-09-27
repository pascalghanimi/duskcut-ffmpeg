"""Offline recipe preparation must include the exact missing FreeType submodule."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


class RecipePreparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="duskcut-recipe-test-")
        self.root = Path(self.tmp.name)
        self.recipe = self.root / "recipe"
        fonts = self.recipe / "scripts.d/45-fonts"
        fonts.mkdir(parents=True)
        for name in ("25-freetype", "50-freetype"):
            (fonts / (name + ".sh")).write_text(
                '#!/bin/bash\nSCRIPT_COMMIT="d333439633039de426f943f28a2926c7f97b5ae5"\n'
                'ffbuild_dockerbuild() {\n    ./autogen.sh\n}\n')
        (self.recipe / "scripts.d/zz-final.sh").write_text("ffbuild_depends() {\n    echo unused\n}\n")
        (self.recipe / "scripts.d/50-onevpl.sh").write_text("curl -fL https://github.com/intel/libvpl/pull/198.patch | git am\n")
        (self.recipe / "util").mkdir()
        (self.recipe / "util/run_stage.sh").write_text('if [[ -d "$FFBUILD_DESTDIR" ]]; then\n    true\nfi\n')
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"rootDependencies": ["fonts"], "forbiddenStagePatterns": ["dvd", "css"]}))
        self.patch = self.root / "onevpl.patch"
        self.patch.write_text("immutable patch\n")
        self.archive = self.root / "dlg.tar.gz"

    def tearDown(self):
        self.tmp.cleanup()

    def archive_source(self, omit=None):
        with tarfile.open(self.archive, "w:gz") as archive:
            for path in ("include/dlg/dlg.h", "include/dlg/output.h", "src/dlg/dlg.c", "LICENSE"):
                if path == omit:
                    continue
                data = (path + " content\n").encode()
                member = tarfile.TarInfo("dlg-395ccad2c1e0daae535c4d20bb0a3f2424648e17/" + path)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def prepare(self):
        return subprocess.run([sys.executable, str(Path(__file__).with_name("prepare_recipe.py")),
                               str(self.recipe), str(self.profile), str(self.patch),
                               str(self.archive), str(self.root / "evidence")], capture_output=True, text=True)

    def test_both_freetype_passes_prepopulate_and_verify_exact_gitlink(self):
        self.archive_source()
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("25-freetype", "50-freetype"):
            contents = (self.recipe / "scripts.d/45-fonts" / (name + ".sh")).read_text()
            self.assertIn('git rev-parse HEAD:subprojects/dlg', contents)
            self.assertIn('395ccad2c1e0daae535c4d20bb0a3f2424648e17', contents)
            self.assertLess(contents.index('cp -a /duskcut-freetype-dlg/.'), contents.index('./autogen.sh'))
            self.assertIn('--mount=src=patches/freetype-dlg,dst=/duskcut-freetype-dlg', contents)
            self.assertNotIn('git submodule update', contents)
            self.assertNotIn('curl', contents)
        self.assertTrue((self.recipe / "patches/freetype-dlg/include/dlg/dlg.h").is_file())
        diff = (self.root / "evidence/duskcut-recipe.patch").read_text()
        self.assertIn('a/scripts.d/45-fonts/25-freetype.sh', diff)
        self.assertIn('a/scripts.d/45-fonts/50-freetype.sh', diff)

    def test_incomplete_submodule_is_rejected_before_build(self):
        self.archive_source(omit="include/dlg/output.h")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing dlg submodule source", result.stderr)

    def test_unknown_freetype_parent_cannot_use_this_patch(self):
        self.archive_source()
        path = self.recipe / "scripts.d/45-fonts/25-freetype.sh"
        path.write_text(path.read_text().replace("d333439633039de426f943f28a2926c7f97b5ae5", "a" * 40))
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pinned FreeType recipe changed", result.stderr)


if __name__ == "__main__":
    unittest.main()
