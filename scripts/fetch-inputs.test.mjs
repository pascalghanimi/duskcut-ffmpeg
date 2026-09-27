import { test } from 'node:test'
import assert from 'node:assert/strict'
import { validateAssets } from './fetch-inputs.mjs'

const valid = () => ({ schemaVersion: 1, releaseTag: 'sources-9.0.2-duskcut.1', extractTo: '.', assets: [{ file: 'sources.tar', bytes: 42, sha256: 'a'.repeat(64), url: 'https://github.com/pascalghanimi/duskcut-ffmpeg/releases/download/sources-9.0.2-duskcut.1/sources.tar' }] })
test('accepts fixed public source asset', () => assert.equal(validateAssets(valid()).length, 1))
test('rejects unpinned, private, duplicate and escaping input assets', () => {
  for (const mutate of [m => m.assets[0].bytes = 0, m => m.assets[0].sha256 = 'unknown', m => m.assets[0].file = '../sources.tar', m => m.assets[0].url += '?token=private', m => m.releaseTag = 'latest', m => m.assets.push(m.assets[0])]) {
    const manifest = valid()
    mutate(manifest)
    assert.throws(() => validateAssets(manifest))
  }
})
