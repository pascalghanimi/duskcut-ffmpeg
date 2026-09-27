# DuskCut FFmpeg

Source and build records for DuskCut's Windows x64 FFmpeg command-line tools.
The private DuskCut editor is not part of this repository.

## Status

The first controlled build is being prepared. This repository is not yet a
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
