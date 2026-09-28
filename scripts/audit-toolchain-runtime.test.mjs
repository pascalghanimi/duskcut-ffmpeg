import assert from 'node:assert/strict'
import test from 'node:test'
import { BINDING, RUNTIMES, containerArguments, linkedArchivePaths, verifyIdentity, verifyRuntimeReport } from './audit-toolchain-runtime.mjs'

function fixture() {
  return { run: { id: BINDING.runId, repository: { full_name: BINDING.repository }, status: 'completed',
    conclusion: 'success', head_sha: BINDING.recipeCommit, path: '.github/workflows/build.yml' },
  result: { lockSha256: BINDING.lockSha256, ffmpegRevision: BINDING.ffmpegRevision, buildId: '9.0.2-duskcut.1',
    status: 'built-not-approved-for-distribution', binaryHashes: { ...BINDING.binaryHashes } },
  image: { reference: BINDING.image, Id: BINDING.imageId, Architecture: 'amd64', Os: 'linux' },
  environment: { CC: BINDING.compiler }, recipeCommit: BINDING.recipeCommit, compilerVersion: BINDING.compilerVersion }
}

test('audit binds the exact successful build and its original compiler identity', () => {
  assert.doesNotThrow(() => verifyIdentity(fixture()))
})

test('wrong run, failed run, changed binary or floating image is rejected', () => {
  for (const mutate of [x => { x.run.id++ }, x => { x.run.conclusion = 'failure' },
    x => { x.result.binaryHashes['ffmpeg.exe'] = 'f'.repeat(64) },
    x => { x.image.reference = 'ghcr.io/btbn/ffmpeg-builds/base-win64:latest' },
    x => { x.environment.CC = 'sh -c untrusted' }, x => { x.recipeCommit = 'a'.repeat(40) }]) {
    const data = fixture()
    mutate(data)
    assert.throws(() => verifyIdentity(data), /mismatch|unexpected/)
  }
})

function runtimeFixture() {
  const report = { compiler: BINDING.compiler, compilerVersion: BINDING.compilerVersion,
    archives: RUNTIMES.map(name => ({ name, path: '/opt/ct-ng/x86_64-w64-mingw32/lib/' + name, bytes: 42, sha256: 'a'.repeat(64) })) }
  return { report, linked: new Set(report.archives.map(row => row.path)) }
}

test('each requested runtime receives a content hash and actual linked archives are corroborated', () => {
  const { report, linked } = runtimeFixture()
  assert.doesNotThrow(() => verifyRuntimeReport(report, linked))
  linked.delete(report.archives[0].path)
  assert.throws(() => verifyRuntimeReport(report, linked), /actual_link_trace/)
})

test('a substituted or escaped compiler archive cannot enter the provenance record', () => {
  for (const mutate of [x => { x.archives[0].path = '/etc/private/libatomic.a' },
    x => { x.archives[0].path = '/opt/ct-ng/../../private/libatomic.a' },
    x => { x.archives[0].sha256 = 'not a digest' }, x => { x.archives[1] = x.archives[0] }]) {
    const { report, linked } = runtimeFixture()
    mutate(report)
    assert.throws(() => verifyRuntimeReport(report, linked), /invalid_runtime_archive/)
  }
})

test('inspection is offline, read-only, unprivileged and receives no host mounts or secrets', () => {
  const args = containerArguments(BINDING.image)
  for (const flag of ['--network=none', '--read-only', '--cap-drop=ALL', '--pull=never']) assert.ok(args.includes(flag))
  assert.equal(args[args.indexOf('--user') + 1], '65534:65534')
  assert.ok(!args.some(arg => ['--mount', '-v', '--env', '-e'].includes(arg)))
  assert.throws(() => containerArguments('different-image'), /unreviewed/)
})

test('actual GCC relative search paths normalize to canonical archive locations', () => {
  const trace = '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libatomic.a\n' +
    '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libgomp.a\n' +
    'gcc -o test /opt/ct-ng/not-a-trace.a\n'
  assert.deepEqual([...linkedArchivePaths(trace)], [
    '/opt/ct-ng/x86_64-w64-mingw32/lib/libatomic.a', '/opt/ct-ng/x86_64-w64-mingw32/lib/libgomp.a',
  ])
})
