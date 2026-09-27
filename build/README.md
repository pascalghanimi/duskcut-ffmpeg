# Controlled DuskCut FFmpeg build

The `duskcut-win64-gpl-no-dvd-v1` profile builds Windows x64 static FFmpeg and
ffprobe from fixed source inputs. It excludes DVD access/CSS decryption and the
unused Rust/SVG/JXL/Vulkan dependency stacks. `profile.json` is the authoritative
list of selected dependencies and FFmpeg flags. It retains DuskCut's local-media
editing, subtitle/font rendering, audio processing, CPU video encoders and
NVIDIA/Intel/AMD hardware integration. Hardware still requires suitable drivers.

The build uses the BtbN recipe revision and base-win64 compiler image pinned in
the source manifest. It rebuilds 41 selected source archives, including MinGW CRT
and winpthreads. All dependency and FFmpeg compilation runs with network access
disabled. The oneVPL patch is an archived, hash-checked input, applied locally.

## Rebuild

Use a Linux x64 Docker host with Node.js, tar/xz, and sufficient disk space
(approximately 50 GB free is prudent). Docker dependency stages are serialized;
each stage uses the runner's available CPU cores. The CI runner has four cores
and 16 GB RAM. The compiler image contains Python and build tools.

1. Download and verify the immutable source input release archives using the
   repository's input-fetching script. Extract them into `input-data/`.
2. Create and validate the build lock:

   ```sh
   node build/create-lock.mjs input-data 9.0.2-duskcut.1
   node build/validate-lock.mjs input-data/build-lock.json
   ```

3. Pull the exact compiler image recorded as `toolchain.image` in the lock. This
   provisioning step needs network access; compilation does not. Never substitute
   a floating `latest` tag.
4. Build into a new, dedicated directory:

   ```sh
   node build/controlled-build.mjs input-data/build-lock.json out
   ```

The lock binds all selected sources and license evidence to their content hashes.
It also binds the profile. The executed generated Dockerfile must select exactly
the same cache members: missing or extra compile inputs stop the build.

## Outputs and traceability

- `out/work/binary/bin/ffmpeg.exe` and `ffprobe.exe`: unsigned built executables.
- `out/BUILD-RESULT.json`: build identity, input lock, source revisions and binary
  SHA-256 hashes. Status remains `built-not-approved-for-distribution` until the
  independent native/application and publication gates pass.
- `out/ffmpeg-build-evidence.tar.xz`: executed scripts, profile, exact recipe diff,
  generated Dockerfile, compiler identity, dependency configuration, full build
  logs, FFmpeg configure logs and link inputs. Intermediate object trees are
  omitted; complete original sources are independently published inputs.

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

This pipeline addresses future controlled builds. It does not establish source
completeness for older Gyan releases, automatically resolve codec patent rights,
or constitute a legal guarantee.
