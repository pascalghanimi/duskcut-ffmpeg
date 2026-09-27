import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import test from 'node:test'
import { inside, validateLock } from './validate-lock.mjs'
import { build, pinGeneratedDockerfile, serializeDependencyStages, publicImageRecord, publicDockerVersion } from './controlled-build.mjs'

async function fixture(fn) {
  const root = await mkdtemp(resolve(tmpdir(), 'duskcut-ffmpeg-lock-'))
  const contents = ['recipe', 'cache', 'ffmpeg', 'notice', 'exception evidence']
  const ids = ['recipe', 'cache', 'ffmpeg', 'notice', 'exception']
  const roles = ['recipe', 'source-cache', 'ffmpeg-source', 'license-evidence', 'review-evidence']
  const blobs = []
  for (let i = 0; i < contents.length; i++) {
    const file = `${ids[i]}.txt`
    await writeFile(resolve(root, file), contents[i])
    blobs.push({ id: ids[i], role: roles[i], file, bytes: Buffer.byteLength(contents[i]), sha256: createHash('sha256').update(contents[i]).digest('hex'), origin: `https://example.com/${file}` })
  }
  const lock = { schemaVersion: 1, target: 'win64', variant: 'gpl', addin: '9.0',
    recipe: { blobId: 'recipe', revision: 'a'.repeat(40) },
    ffmpeg: { blobId: 'ffmpeg', revision: 'b'.repeat(40), sourceDateEpoch: 12345 },
    downloadCache: { blobId: 'cache', innerSha256: 'c'.repeat(64) },
    toolchain: { image: `ghcr.io/btbn/ffmpeg-builds/base-win64@sha256:${'d'.repeat(64)}` }, blobs,
    runtimeComponents: [{ name: 'runtime', version: '1.2.3', license: 'GPL-3.0-with-GCC-exception', noticeBlobId: 'notice',
      exclusion: { basis: 'gcc-runtime-library-exception', evidenceBlobId: 'exception', rationale: 'Specific documented component exception and applicable eligibility reviewed.' } }] }
  const path = resolve(root, 'build-lock.json')
  const save = () => writeFile(path, JSON.stringify(lock))
  await save()
  try { await fn({ root, lock, path, save }) } finally { await rm(root, { recursive: true, force: true }) }
}
test('valid input integrity does not imply legal or source approval', () => fixture(async ({ path }) => {
  const result = await validateLock(path)
  assert.equal(result.status, 'inputs-integrity-verified-not-source-or-legal-approval')
  assert.equal(result.blobs.size, 5)
}))
test('paths cannot escape lock root', () => {
  for (const bad of ['../secret', 'x/../../secret', 'x\\secret', '/tmp/secret']) assert.throws(() => inside(process.cwd(), bad))
})
test('content hash mismatches stop before any build', () => fixture(async ({ root, path }) => {
  await writeFile(resolve(root, 'recipe.txt'), 'new recipe')
  await assert.rejects(validateLock(path), /blob_integrity_mismatch/)
}))
test('mutable compiler image tags are refused', () => fixture(async ({ lock, path, save }) => {
  lock.toolchain.image = 'ghcr.io/btbn/ffmpeg-builds/base-win64:latest'
  await save()
  await assert.rejects(validateLock(path), /toolchain_image_not_digest_pinned/)
}))
test('a download recipe/cache-key hash is not a replacement for cache content hash', () => fixture(async ({ lock, path, save }) => {
  delete lock.downloadCache.innerSha256
  await save()
  await assert.rejects(validateLock(path), /inner_cache_not_content_pinned/)
}))
test('runtime exception needs component identity, notice and review evidence', () => fixture(async ({ lock, path, save }) => {
  lock.runtimeComponents[0].exclusion.evidenceBlobId = 'approved'
  await save()
  await assert.rejects(validateLock(path), /missing_review-evidence_blob/)
}))
test('generic runtime source-exempt boolean is rejected', () => fixture(async ({ lock, path, save }) => {
  lock.runtimeComponents[0].exclusion = { approved: true }
  await save()
  await assert.rejects(validateLock(path), /runtime_source_or_specific_exclusion_missing/)
}))
test('generated recipe external base is replaced once and all other references stay local', () => {
  const image = `ghcr.io/btbn/ffmpeg-builds/base-win64@sha256:${'d'.repeat(64)}`
  const source = 'FROM ghcr.io/btbn/ffmpeg-builds/base-win64:latest AS base-layer\nFROM base-layer AS x264\nFROM base-layer\nCOPY --from=x264 /opt/ffbuild /opt/ffbuild\n'
  assert.ok(pinGeneratedDockerfile(source, image).includes(`FROM ${image} AS base-layer`))
  assert.throws(() => pinGeneratedDockerfile(source + 'FROM ubuntu:latest\n', image), /unrecorded_external_build_image/)
  assert.throws(() => pinGeneratedDockerfile(source + 'COPY --from=ghcr.io/other/base:latest /foo /bar\n', image), /unrecorded_external_copy_image/)
})
test('foreign recipe format cannot silently pass pinning', () => {
  assert.throws(() => pinGeneratedDockerfile('FROM ubuntu:latest\n', 'pinned'), /unexpected_generated_base_image/)
})
test('Windows build fails without installing or activating Linux features', { skip: process.platform === 'linux' }, async () => {
  await assert.rejects(build('missing-lock.json', 'should-not-be-created'), /controlled_build_requires_linux_docker_host/)
})
test('image record excludes environment, labels and private aliases', () => {
  const info = { Id: `sha256:${'a'.repeat(64)}`, Architecture: 'amd64', Os: 'linux', Size: 12,
    Config: { Env: ['OPENAI_API_KEY=private-key'], Labels: { owner: 'private-customer' } },
    RepoTags: ['private-customer/secret:latest'], RepoDigests: ['private-customer/secret@sha256:123'], Author: 'private-user' }
  const record = publicImageRecord(info, 'public-pinned-image')
  assert.deepEqual(Object.keys(record).sort(), ['Architecture', 'Id', 'Os', 'Size', 'reference'].sort())
  assert.ok(!JSON.stringify(record).includes('private'))
})
test('Docker version record excludes contexts, endpoints and plugin paths', () => {
  const record = publicDockerVersion({ Client: { Version: '29.0.0', ApiVersion: '1.53', Context: 'private-customer', Plugins: ['secret-location'] },
    Server: { Version: '29.0.0', Components: [{ Details: { key: 'password' } }], Endpoint: 'https://private-user:password@server' } })
  assert.deepEqual(record, { Client: { Version: '29.0.0', ApiVersion: '1.53' }, Server: { Version: '29.0.0' } })
})
test('authenticated or signed source origins cannot enter public bundle', () => fixture(async ({ lock, path, save }) => {
  lock.blobs[0].origin = 'https://user:password@example.com/source'
  await save()
  await assert.rejects(validateLock(path), /nonpublic_or_credential_origin/)
  lock.blobs[0].origin = 'https://example.com/source?token=private'
  await save()
  await assert.rejects(validateLock(path), /nonpublic_or_credential_origin/)
}))
test('selected-source lock rejects DVD/CSS sources before a compiler starts', () => fixture(async ({ lock, path, save }) => {
  lock.schemaVersion = 2
  lock.buildId = '9.0.2-duskcut.1'
  lock.profile = { id: 'duskcut-win64-gpl-no-dvd-v1', sha256: '1'.repeat(64) }
  lock.oneVplPatch = { blobId: 'recipe' }
  lock.blobs[0].role = 'patch'
  lock.recipe.blobId = 'ffmpeg'
  lock.blobs[2].role = 'recipe'
  lock.ffmpeg.blobId = 'notice'
  lock.blobs[3].role = 'ffmpeg-source'
  lock.sourceCache = { members: [{ blobId: 'cache', cacheName: `50-libdvdcss_${'a'.repeat(64)}.tar.xz` }] }
  await save()
  await assert.rejects(validateLock(path), /invalid_duplicate_or_forbidden_cache_member/)
}))
test('dependency stages serialize without a circular link to combination/final stages', () => {
  const generated = 'FROM pinned AS base-layer\nFROM base-layer AS mingw\nRUN build-mingw\nFROM base-layer AS x264\nRUN build-x264\nFROM base-layer AS combine-layer\nCOPY --from=x264 /out /out\nFROM base-layer\nCOPY --from=combine-layer /out /out\n'
  const fixed = serializeDependencyStages(generated)
  assert.ok(fixed.includes('FROM base-layer AS x264\nCOPY --from=mingw /duskcut-control/stage-complete'))
  assert.equal((fixed.match(/touch \/duskcut-control\/stage-complete/g) || []).length, 2)
  assert.ok(!fixed.includes('COPY --from=combine-layer /duskcut-control'))
  assert.ok(!fixed.includes('COPY --from=x264 /duskcut-control'))
})
