/** Release identity and bytes are pinned independently from the binary build ID. */
export function validateSourceRelease(release, manifestSha256, supplementSha256) {
  if (release?.schemaVersion !== 1 || !/^sources-[a-z0-9][a-z0-9.-]{0,79}$/.test(release.releaseTag || '') ||
      !/^[a-f0-9]{64}$/.test(manifestSha256) || !/^[a-f0-9]{64}$/.test(supplementSha256) ||
      manifestSha256 !== release.sourceManifestSha256 ||
      supplementSha256 !== release.supplementalManifestSha256 ||
      !Array.isArray(release.assets) || !release.assets.length ||
      release.assets.some(asset => asset.url !==
        `https://github.com/pascalghanimi/duskcut-ffmpeg/releases/download/${release.releaseTag}/${asset.file}` ||
        !/^[a-zA-Z0-9_.-]+\.tar(?:\.gz|\.xz)?$/.test(asset.file || '') ||
        !Number.isSafeInteger(asset.bytes) || asset.bytes < 1 || !/^[a-f0-9]{64}$/.test(asset.sha256 || ''))) {
    throw new Error('released_source_manifest_integrity_mismatch')
  }
}
