# DuskCut FFmpeg

Source and build records for DuskCut's Windows x64 FFmpeg command-line tools.
The private DuskCut editor is not part of this repository.

## Status

The first controlled build and native compatibility review are in progress. This repository is not yet a
download or source-completeness claim for a released DuskCut installer.

The selected build keeps the software codecs, audio/video filters, subtitle
support and hardware interfaces needed by DuskCut. It does not include DVD
navigation or DVD CSS decryption, and does not ship ffplay.

Build inputs are fixed by SHA-256 and source revision. Dependency compilation
and FFmpeg compilation run with networking disabled. Build configuration,
patches, notices and the exact source inputs accompany each approved binary
release. Native compatibility tests are required before the editor uses a new
pair of executables.

## Licensing

The FFmpeg binary profile enables GPL components; see its accompanying GPL and
third-party notices. Build orchestration written for this repository is provided
under the MIT license in `LICENSE`. Upstream build recipes and all dependency
sources retain their own licenses. This is not a license grant for the editor.

Older installers using a different FFmpeg supplier are not covered by an own
build's corresponding-source archive.

## Source inputs and replay

`release-assets.json` pins every public source part by size and SHA-256. The
original input archive is unchanged; a separate part supplies the exact `dlg`
submodule used by both FreeType build stages, including its original Boost
license. Both manifest hashes are checked before compilation.

The controlled build runs on an isolated Linux Docker host with Node.js 24 and
Python 3.12. It does not use a private editor checkout. See `build/README.md` for
source acquisition, compiler pinning and offline execution instructions.

## Release assembly

The manual `package-release.yml` workflow takes a successful controlled-build
run ID and the identical version token actually observed from both Windows
executables. It downloads that run, validates the exact source manifests from
its recipe commit, and assembles runtime and corresponding-source archives.
This runs on GitHub to avoid uploading large archives through a workstation.

Assembly creates a **new draft only**. Existing drafts and published releases
are never overwritten. Do not manually publish or delete its draft while the
assembly job is running: GitHub does not provide an atomic draft lock. The
workflow detects a changed release state and stops, but cannot prevent a human
from publishing between two requests.

An assembled candidate is not automatically approved. Review actual Windows
execution, media compatibility, source/binary correspondence, dependency
configuration and shipped notices before publication or application selection.
The candidate's `approvalStatus` deliberately prevents premature activation.
