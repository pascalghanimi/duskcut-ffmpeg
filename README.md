# DuskCut FFmpeg

Source and build records for DuskCut's Windows x64 FFmpeg command-line tools.
The private DuskCut editor is not part of this repository.

## Status

Controlled build `9.0.2-duskcut.1` has passed source, runtime and native compatibility
review. Its [versioned release](https://github.com/pascalghanimi/duskcut-ffmpeg/releases/tag/9.0.2-duskcut.1)
provides the Windows tools and their matching corresponding-source archive.
See [the review and its limits](docs/9.0.2-duskcut.1-review.md).
This is a standalone FFmpeg component release, not a DuskCut editor installer.

The .1 release keeps the software codecs, audio/video filters, subtitle
support and hardware interfaces needed by DuskCut. It does not include DVD
optical-disc navigation libraries or DVD CSS decryption, and does not ship ffplay.
Ordinary DVD subtitle/MPEG formats and the DVD navigation data parser are not
decryption and remain supported.

The current Windows-codec component is `9.0.2-duskcut.3`, with H.264 and AAC
encode/decode and HEVC decode exclusively behind Windows Media Foundation; HEVC
encoding is disabled. It also removes ProRes and WMV/VC-1.
AV1/AVIF, VP8/VP9, MPEG-2 and all existing Rubber Band/vidstab effects remain.
This remains a GPL component. Its exact source/runtime review and the bounded
native/application tests are recorded in [the .3 review](docs/9.0.2-duskcut.3-review.md).
HEVC playback on an extension-enabled account and a new signed editor installer
are not established by that review.
Its custom Windows adapter, exact patch and build configuration are included in
the corresponding-source process. See `build/README.md`.

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
