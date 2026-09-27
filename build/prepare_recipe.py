"""Apply the complete, versioned DuskCut profile to the verified upstream recipe."""
import difflib
import json
import pathlib
import re
import sys

recipe, profile_file, patch_file, evidence = map(pathlib.Path, sys.argv[1:])
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
