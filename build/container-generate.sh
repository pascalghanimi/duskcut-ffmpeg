#!/bin/bash
# Executed only in a digest-pinned compiler container with --network=none.
set -euo pipefail
umask 022
cd /work
python3 control/capture_environment.py > toolchain-build-environment.json
mapfile -t fields < <(python3 - <<'PY'
import json
lock=json.load(open('/work/control/lock.json'))
blobs={b['id']:b for b in lock['blobs']}
for key in ('recipe','oneVplPatch','freetypeDlg'):
    print('/inputs/'+blobs[lock[key]['blobId']]['file'])
PY
)
[[ ${#fields[@]} == 3 ]]
python3 control/safe_extract.py "${fields[0]}" recipe-unpacked
mapfile -t roots < <(find recipe-unpacked -mindepth 1 -maxdepth 1 -type d)
[[ ${#roots[@]} == 1 ]]
mv "${roots[0]}" recipe
python3 control/prepare_recipe.py recipe control/profile.json "${fields[1]}" "${fields[2]}" configuration
python3 - <<'PY'
import json,pathlib,shutil
l=json.load(open('/work/control/lock.json'))
b={x['id']:x for x in l['blobs']}
d=pathlib.Path('/work/recipe/.cache/downloads')
d.mkdir(parents=True)
for member in l['sourceCache']['members']:
    shutil.copyfile('/inputs/'+b[member['blobId']]['file'], d/member['cacheName'])
PY
cd recipe
# Unlike upstream makeimage.sh, this never runs download.sh or refreshes refs.
./generate.sh win64 gpl 9.0
# Verify every actual generated dependency-cache mount exists. Docker itself
# verifies hash-pinned inputs before this phase; the inner tar is pinned above.
python3 - <<'PY'
import json,pathlib,re
d=pathlib.Path('Dockerfile').read_text()
profile=json.load(open('/work/control/profile.json'))
for pattern in profile['forbiddenStagePatterns']:
    if re.search(r'(?:SELF="[^"]*|STAGENAME="[^"]*|FROM base-layer AS [^\n]*)'+re.escape(pattern),d):
        raise SystemExit('Forbidden generated build stage: '+pattern)
files=sorted(set(re.findall(r'src=(\.cache/downloads/[^,\s]+)',d)))
if not files:
    raise SystemExit('No dependency sources selected: refusing empty source closure')
for f in files:
    if not pathlib.Path(f).is_file(): raise SystemExit('Missing source-cache member: '+f)
lock=json.load(open('/work/control/lock.json'))
declared={'.cache/downloads/'+x['cacheName'] for x in lock['sourceCache']['members']}
if set(files)!=declared:
    raise SystemExit('Declared source members do not exactly match generated recipe: '+str(set(files)^declared))
pathlib.Path('/work/selected-source-cache-files.txt').write_text('\n'.join(files)+'\n')
pathlib.Path('/work/configuration/generated-Dockerfile-original').write_text(d)
PY
