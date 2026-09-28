import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { validateSourceRelease } from './source-release.mjs'
import { scriptNames } from './controlled-build.mjs'

const release = JSON.parse(readFileSync(new URL('../release-assets.json', import.meta.url)))
test('a new binary build reuses immutable source release .1 without weakening hashes', () => {
  assert.equal(release.releaseTag, 'sources-9.0.2-duskcut.1')
  assert.doesNotThrow(() => validateSourceRelease(release, release.sourceManifestSha256, release.supplementalManifestSha256))
  assert.throws(() => validateSourceRelease(release, '0'.repeat(64), release.supplementalManifestSha256), /integrity_mismatch/)
  assert.throws(() => validateSourceRelease(release, release.sourceManifestSha256, '0'.repeat(64)), /integrity_mismatch/)
})
test('source release identity cannot be relabeled, redirected, or use mutable tags', () => {
  for (const releaseTag of ['latest', 'sources-../bad', 'sources-9.0.2-duskcut.2']) {
    assert.throws(() => validateSourceRelease({...release, releaseTag}, release.sourceManifestSha256, release.supplementalManifestSha256))
  }
  const redirected = structuredClone(release)
  redirected.assets[0].url = 'https://example.invalid/source.tar'
  assert.throws(() => validateSourceRelease(redirected, release.sourceManifestSha256, release.supplementalManifestSha256))
})
test('executed MF C source and patch driver are copied with the controlled build', () => {
  for (const name of ['windows-codecs/mfdec.c', 'windows-codecs/apply.py', 'verify_codec_profile.py', 'apply_windows_codecs.py']) {
    assert.ok(scriptNames.includes(name), name)
  }
})
