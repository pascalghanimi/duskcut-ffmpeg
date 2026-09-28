# Controlled DuskCut FFmpeg build

The `duskcut-win64-gpl-windows-codecs-v3` profile builds Windows x64 static FFmpeg
and ffprobe from fixed source inputs. H.264 and AAC encode/decode and HEVC decode
use only Windows Media Foundation wrappers. HEVC encoding is disabled. It excludes native implementations, x264/x265,
direct GPU codec encoders, ProRes, WMV/VC-1, DVD access/CSS decryption and the
unused Rust/SVG/JXL/Vulkan dependency stacks. `profile.json` is authoritative.
AV1/AVIF, VP8/VP9 (including alpha), MPEG-2, font rendering, Rubber Band pitch and
vidstab stabilization remain. This is still a GPL build, not an LGPL conversion.
Windows codecs must be installed and support the actual stream profile. HEVC can
require the separately installed Windows extension; no native codec fallback is
silently substituted. Hardware is used through Media Foundation where available.

The build uses the BtbN recipe revision and a base-win64 compiler image pinned in
`toolchain-pin.json`. The immutable source release still records the original
upstream image digest. BtbN removed that digest from GHCR after the first build,
so the exact current upstream image is preserved under this project's GHCR
namespace by `mirror-toolchain.yml`; the lock records both provenance digests,
the original source-manifest digest, and the mirror run. A mirror is not a new
compiler build, nor an implicit substitution of the original source release.
It rebuilds 39 selected source archives, including MinGW CRT
and winpthreads. All dependency and FFmpeg compilation runs with network access
disabled. The oneVPL patch is an archived, hash-checked input, applied locally.
The pinned FreeType snapshots omit their `dlg` git submodule. A separately
published supplemental source archive supplies the exact gitlink revision
`395ccad2c1e0daae535c4d20bb0a3f2424648e17` and its Boost license. Both FreeType
build passes verify the parent gitlink and copy this source before running
upstream `autogen.sh`; no submodule clone or other network access is allowed.

## Rebuild

Use a Linux x64 Docker host with Node.js, tar/xz, and sufficient disk space
(approximately 50 GB free is prudent). Docker dependency stages are serialized;
each stage uses the runner's available CPU cores. The CI runner has four cores
and 16 GB RAM. The compiler image contains Python and build tools.

1. Download and verify the immutable source input release archives using the
   repository's input-fetching script. Extract both the original source archive
   and the supplemental source archive into `input-data/`. The original archive
   is unchanged; the supplement adds `sources/supplemental-manifest.json` and its
   content-hashed source/license files without overwriting original inputs.
2. Create and validate the build lock:

   ```sh
   node build/create-lock.mjs input-data 9.0.2-duskcut.3
   node build/validate-lock.mjs input-data/build-lock.json
   ```

3. Pull the exact mirrored compiler image recorded as `toolchain.image` in the
   lock. Check its original-upstream provenance in `toolchain-pin.json` and the
   recorded mirror run. This provisioning step needs network access; compilation
   does not. Never substitute a floating `latest` tag.
4. Build into a new, dedicated directory:

   ```sh
   node build/controlled-build.mjs input-data/build-lock.json out
   ```

The lock binds all selected sources and license evidence to their content hashes.
It also binds the profile. The executed generated Dockerfile must select exactly
the same cache members: missing or extra compile inputs stop the build.
The binary build ID is separate from `sourceReleaseTag`: .3 reuses the exact
hash-pinned `sources-9.0.2-duskcut.1` inputs and adds versioned local MF wrapper
source from this repository. This does not claim the old inputs were republished.
Before configure, `apply_windows_codecs.py` applies the reviewed adapter and
records all five changed/added FFmpeg files in a unified diff and pre/post hash
inventory. Configuration then fails if any forbidden implementation is enabled
or any required Windows wrapper, retained format, or effect is missing.

## Outputs and traceability

- `out/work/binary/bin/ffmpeg.exe` and `ffprobe.exe`: unsigned built executables.
- `out/BUILD-RESULT.json`: build identity, input lock, source revisions and binary
  SHA-256 hashes. Status remains `built-not-approved-for-distribution` until the
  independent native/application and publication gates pass.
- `out/ffmpeg-build-evidence.tar.xz`: executed scripts, profile, exact recipe diff,
  generated Dockerfile, compiler identity, dependency configuration, full build
  logs, FFmpeg configure logs and link inputs. Intermediate object trees are
  omitted; complete original sources are independently published inputs.
- `work/configuration/windows-codecs.patch`, `windows-codecs-source.json`, and
  `codec-policy-audit.json`: exact custom adapter source correspondence and the
  actual generated codec configuration. Packaging verifies copied controls,
  including `mfdec.c`, against the recorded Git commit and rechecks the inventory.

The original BtbN recipe is preserved unchanged in the source inputs.
`prepare_recipe.py` makes the selected-dependency and offline-patch changes and
records an exact unified diff. MinGW runtime sources and notices are supplied;
GCC runtime exception evidence is documented component by component in the source
package. Compiler environment records are allowlisted; arbitrary runner
environment variables, provider secrets and private application data are never
mounted into the builder or included in the evidence archive.

The final binary package also needs the source package's distribution documents
and notices. Verify actual DLL imports, linked runtime archives and the complete
native/application test suite before replacing installed resources. Signing
changes executable bytes, so release records must retain both original built
hashes and final signed hashes.

## Post-build runtime audit for each new version

Do not edit a previous version's review or substitute guessed run IDs/hashes.
After the controlled build has completed successfully:

1. Dispatch `audit-toolchain-runtime.yml` on `main`, supplying that exact
   controlled build's numeric run ID as `build_run`. The audit workflow selects
   its one matching, unexpired build artifact. It checks the GitHub run, recipe
   commit, clean-tree record, actual binaries, archived lock, compiler image and
   identical standalone/archived records before pulling any image.
2. The generated `runtime-audit-binding.json` contains the actual build ID,
   run ID, recipe commit, lock hash, binary hashes and compiler identity. No
   source-code rebinding is needed for .2 or later releases. The original pinned
   compiler image is inspected read-only, offline and without host mounts.
3. Require a successful audit workflow, then download its
   `runtime-audit-<build-run>-<audit-run>` artifact. Check its checksum file.
   `libatomic-toolchain-audit.json` supplies actual archive hashes and resolved
   original linker paths. Diagnostics alone are not a completed audit.
4. Create a **new** `public-source/runtime-review/<buildId>/` directory. Run
   direct PE/configuration inspection against that build's actual executables
   and evidence; do not reuse an older compiled-runtime inspection. The four
   immutable GCC license/source text files from an earlier review may be reused
   only when the same compiler/source version and runtime archive identity are
   corroborated; otherwise acquire and review the matching evidence.
5. Add the new audit, new static inspection and a factual version-specific
   explanation. Generate `runtime-review.json` from their actual byte sizes and
   SHA-256 values, binding the new build ID, lock, binaries, compiler image and
   actual libatomic hash. Keep old reviews unchanged. The historical local
   review-preparation scripts may have .1-specific constants: do not run them
   unchanged for a new version or merely rename their output.
6. Commit the new review after inspection. Only then dispatch
   `package-release.yml` with the same controlled-build run and version token
   observed from **both** executables. Packaging independently verifies runtime
   evidence against that build before producing a new draft. Functional testing,
   review and publication remain separate steps.

This pipeline addresses future controlled builds. It does not establish source
completeness for older Gyan releases, automatically resolve codec patent rights,
or constitute a legal guarantee.
