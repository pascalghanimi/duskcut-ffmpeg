"""Apply the complete, versioned DuskCut profile to the verified upstream recipe."""
import difflib
import json
import pathlib
import re
import shutil
import subprocess
import sys

recipe, profile_file, patch_file, dlg_file, evidence = map(pathlib.Path, sys.argv[1:])
profile = json.loads(profile_file.read_text())
evidence.mkdir(parents=True, exist_ok=True)
changes = []

def replace(relative, new):
    path = recipe / relative
    old = path.read_text()
    changes.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      fromfile="a/" + relative, tofile="b/" + relative))
    path.write_text(new)

roots = profile["rootDependencies"]
if len(roots) != len(set(roots)) or any(not re.fullmatch(r"[a-z][a-z0-9-]*", x) for x in roots):
    raise ValueError("Invalid dependency profile")
for name in roots:
    if any(part in name for part in profile["forbiddenStagePatterns"]):
        raise ValueError("Forbidden source dependency: " + name)
    matches = list((recipe / "scripts.d").glob("??-" + name)) + list((recipe / "scripts.d").glob("??-" + name + ".sh"))
    if len(matches) != 1:
        raise ValueError("Dependency does not resolve uniquely: " + name)

entry = recipe / "scripts.d/zz-final.sh"
contents = entry.read_text()
start = contents.index("ffbuild_depends() {")
end = contents.index("\n}", start) + 2
contents = contents[:start] + "ffbuild_depends() {\n" + "".join("    echo " + name + "\n" for name in roots) + "}" + contents[end:]
replace("scripts.d/zz-final.sh", contents)

onevpl = recipe / "scripts.d/50-onevpl.sh"
contents = onevpl.read_text()
old_patch = "curl -fL https://github.com/intel/libvpl/pull/198.patch | git am"
if contents.count(old_patch) != 1:
    raise ValueError("Upstream oneVPL recipe no longer matches the pinned patch operation")
contents = contents.replace(old_patch, "git apply /duskcut-onevpl.patch")
contents += '\nffbuild_dockerstage() {\n    to_df "RUN --mount=src=${SELF},dst=/stage.sh --mount=src=${SELFCACHE},dst=/cache.tar.xz --mount=src=patches/onevpl.patch,dst=/duskcut-onevpl.patch run_stage /stage.sh"\n}\n'
replace("scripts.d/50-onevpl.sh", contents)
(recipe / "patches").mkdir(exist_ok=True)
(recipe / "patches/onevpl.patch").write_bytes(patch_file.read_bytes())

# The upstream FreeType cache contains the gitlink but no dlg submodule files.
# Resolve that exact gitlink from our hash-verified supplementary source archive
# before autogen; compilation remains offline and autogen keeps its normal copy.
dlg_revision = "395ccad2c1e0daae535c4d20bb0a3f2424648e17"
dlg_unpacked = recipe / "patches/freetype-dlg-unpacked"
subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("safe_extract.py")),
                str(dlg_file), str(dlg_unpacked)], check=True)
dlg_roots = list(dlg_unpacked.iterdir())
if len(dlg_roots) != 1 or not dlg_roots[0].is_dir():
    raise ValueError("dlg archive must contain exactly one source directory")
dlg_root = dlg_roots[0]
for required in ("include/dlg/dlg.h", "include/dlg/output.h", "src/dlg/dlg.c"):
    if not (dlg_root / required).is_file():
        raise ValueError("Missing dlg submodule source: " + required)
shutil.move(str(dlg_root), recipe / "patches/freetype-dlg")
dlg_unpacked.rmdir()
for stage_name in ("25-freetype", "50-freetype"):
    relative = "scripts.d/45-fonts/" + stage_name + ".sh"
    contents = (recipe / relative).read_text()
    marker = "    ./autogen.sh"
    if contents.count(marker) != 1 or 'SCRIPT_COMMIT="d333439633039de426f943f28a2926c7f97b5ae5"' not in contents:
        raise ValueError("Pinned FreeType recipe changed: " + stage_name)
    populate = ('    test "$(git rev-parse HEAD:subprojects/dlg)" = "' + dlg_revision + '"\n'
                '    mkdir -p subprojects/dlg\n'
                '    cp -a /duskcut-freetype-dlg/. subprojects/dlg/\n'
                '    test -f subprojects/dlg/include/dlg/dlg.h\n'
                '    test -f subprojects/dlg/include/dlg/output.h\n')
    contents = contents.replace(marker, populate + marker)
    contents += '\nffbuild_dockerstage() {\n    to_df "RUN --mount=src=${SELF},dst=/stage.sh --mount=src=${SELFCACHE},dst=/cache.tar.xz --mount=src=patches/freetype-dlg,dst=/duskcut-freetype-dlg run_stage /stage.sh"\n}\n'
    replace(relative, contents)

# Keep generated dependency configuration and license notices in the combined
# prefix. Source archives are preserved separately, so no object trees are copied.
stage = recipe / "util/run_stage.sh"
contents = stage.read_text()
marker = 'if [[ -d "$FFBUILD_DESTDIR" ]]; then'
if contents.count(marker) != 1:
    raise ValueError("Upstream stage runner changed")
capture = '''# DuskCut: keep the generated build settings beside exact source inputs.
if [[ -n "$STAGENAME" && -d "$FFBUILD_DESTPREFIX" ]]; then
    evidence="$FFBUILD_DESTPREFIX/share/duskcut-build/$STAGENAME"
    mkdir -p "$evidence"
    while IFS= read -r -d '' file; do
        relative="${file#./}"
        mkdir -p "$evidence/$(dirname "$relative")"
        cp "$file" "$evidence/$relative"
    done < <(find . -type f \\( -name config.log -o -name CMakeCache.txt -o -name meson-log.txt -o -name config.h -o -name '*.pc' \\) -size -8M -print0)
fi

'''
replace("util/run_stage.sh", contents.replace(marker, capture + marker))
(evidence / "duskcut-recipe.patch").write_text("".join(changes))
(evidence / "profile.json").write_bytes(profile_file.read_bytes())
