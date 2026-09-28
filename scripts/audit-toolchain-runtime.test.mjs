import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { BINDING, RUNTIMES, containerArguments, deriveBinding, linkedArchivePaths, readBuildBinding, validateBinding, verifyIdentity, verifyRuntimeReport } from './audit-toolchain-runtime.mjs'

function fixture() {
  return { run: { id: BINDING.runId, repository: { full_name: BINDING.repository }, status: 'completed',
    conclusion: 'success', head_sha: BINDING.recipeCommit, path: '.github/workflows/build.yml', head_branch: 'main', event: 'workflow_dispatch' },
  result: { schemaVersion: 1, lockSha256: BINDING.lockSha256, ffmpegRevision: BINDING.ffmpegRevision, buildId: '9.0.2-duskcut.1',
    status: 'built-not-approved-for-distribution', binaryHashes: { ...BINDING.binaryHashes } },
  image: { reference: BINDING.image, Id: BINDING.imageId, Architecture: 'amd64', Os: 'linux' },
  environment: { CC: BINDING.compiler }, recipeCommit: BINDING.recipeCommit, compilerVersion: BINDING.compilerVersion }
}

test('audit binds the exact successful build and its original compiler identity', () => {
  assert.doesNotThrow(() => verifyIdentity(fixture(), BINDING))
})

test('wrong run, failed run, changed binary or floating image is rejected', () => {
  for (const mutate of [x => { x.run.id++ }, x => { x.run.conclusion = 'failure' },
    x => { x.result.binaryHashes['ffmpeg.exe'] = 'f'.repeat(64) },
    x => { x.image.reference = 'ghcr.io/btbn/ffmpeg-builds/base-win64:latest' },
    x => { x.environment.CC = 'sh -c untrusted' }, x => { x.recipeCommit = 'a'.repeat(40) }]) {
    const data = fixture()
    mutate(data)
    assert.throws(() => verifyIdentity(data, BINDING), /mismatch|unexpected/)
  }
})

function runtimeFixture() {
  const report = { compiler: BINDING.compiler, compilerVersion: BINDING.compilerVersion,
    archives: RUNTIMES.map(name => ({ name, path: '/opt/ct-ng/x86_64-w64-mingw32/lib/' + name, bytes: 42, sha256: 'a'.repeat(64),
      linkTraceFiles: [{ recordedPath: '/opt/ct-ng/x86_64-w64-mingw32/lib/' + name, resolvedPath: '/opt/ct-ng/x86_64-w64-mingw32/lib/' + name, bytes: 42, sha256: 'a'.repeat(64) }] })) }
  return { report, linked: new Set(report.archives.map(row => row.path)) }
}

test('each requested runtime receives a content hash and actual linked archives are corroborated', () => {
  const { report, linked } = runtimeFixture()
  assert.doesNotThrow(() => verifyRuntimeReport(report, linked, BINDING))
  linked.delete(report.archives[0].path)
  assert.throws(() => verifyRuntimeReport(report, linked, BINDING), /link_trace_archive/)
})

test('a substituted or escaped compiler archive cannot enter the provenance record', () => {
  for (const mutate of [x => { x.archives[0].path = '/etc/private/libatomic.a' },
    x => { x.archives[0].path = '/opt/ct-ng/../../private/libatomic.a' },
    x => { x.archives[0].sha256 = 'not a digest' }, x => { x.archives[1] = x.archives[0] }]) {
    const { report, linked } = runtimeFixture()
    mutate(report)
    assert.throws(() => verifyRuntimeReport(report, linked, BINDING), /invalid_runtime_archive/)
  }
})

test('inspection is offline, read-only, unprivileged and receives no host mounts or secrets', () => {
  const args = containerArguments(BINDING.image, new Set(), BINDING)
  for (const flag of ['--network=none', '--read-only', '--cap-drop=ALL', '--pull=never']) assert.ok(args.includes(flag))
  assert.equal(args[args.indexOf('--user') + 1], '65534:65534')
  assert.ok(!args.some(arg => ['--mount', '-v', '--env', '-e'].includes(arg)))
  assert.throws(() => containerArguments('different-image', new Set(), BINDING), /unreviewed/)
})

test('actual GCC paths are preserved for symlink-aware resolution inside the original image', () => {
  const trace = '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libatomic.a\n' +
    '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libgomp.a\n' +
    'gcc -o test /opt/ct-ng/not-a-trace.a\n'
  assert.deepEqual([...linkedArchivePaths(trace)], [
    '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libatomic.a',
    '/opt/ct-ng/lib/gcc/x86_64-w64-mingw32/16.2.0/../../../../x86_64-w64-mingw32/lib/../lib/libgomp.a',
  ])
  assert.doesNotThrow(() => containerArguments(BINDING.image, linkedArchivePaths(trace), BINDING))
})

test('symlinked compiler archives match only after resolving and hashing actual link paths in the pinned image', () => {
  const { report, linked } = runtimeFixture()
  const entry = report.archives[0]
  entry.path = '/opt/ct-ng/x86_64-w64-mingw32/runtime/lib/libatomic.a'
  entry.linkTraceFiles[0].resolvedPath = entry.path
  assert.doesNotThrow(() => verifyRuntimeReport(report, linked, BINDING))
  entry.linkTraceFiles[0].sha256 = 'b'.repeat(64)
  assert.throws(() => verifyRuntimeReport(report, linked, BINDING), /link_trace_archive_does_not_match/)
})

test('untrusted host paths cannot enter the read-only container inspection', () => {
  assert.throws(() => containerArguments(BINDING.image, new Set(['/etc/libatomic.a']), BINDING), /unsafe_link_trace_path/)
  assert.throws(() => containerArguments(BINDING.image, new Set(['/opt/ct-ng/../../libatomic.a']), BINDING), /unsafe_link_trace_path/)
})
test('future build binding comes from that build without mutating historical audit identity', () => {
  const data = fixture()
  data.run.id = 123
  data.recipeCommit = data.run.head_sha = 'd'.repeat(40)
  data.result.buildId = 'unit-test-new.2'
  data.result.lockSha256 = 'a'.repeat(64)
  data.result.recipeRevision = 'b'.repeat(40)
  data.result.binaryHashes = {'ffmpeg.exe': 'c'.repeat(64), 'ffprobe.exe': 'd'.repeat(64)}
  const lock = {schemaVersion: 2, buildId: data.result.buildId, ffmpeg: {revision: data.result.ffmpegRevision},
    recipe: {revision: data.result.recipeRevision}, toolchain: {image: data.image.reference}}
  const binding = deriveBinding(data, lock, data.result.lockSha256)
  assert.equal(binding.buildId, 'unit-test-new.2')
  assert.equal(binding.runId, 123)
  assert.equal(BINDING.buildId, '9.0.2-duskcut.1')
  assert.doesNotThrow(() => verifyIdentity(data, binding))
  assert.throws(() => verifyIdentity(data, BINDING), /unexpected/)
  assert.throws(() => deriveBinding(data, lock, '0'.repeat(64)), /archived_build_lock/)
  assert.throws(() => deriveBinding(data, {...lock, toolchain:{image:'different'}}, data.result.lockSha256), /archived_build_lock/)
})
test('an explicit validated binding is mandatory and cannot launch another image or executable', () => {
  for (const changed of [undefined, {...BINDING, image: 'ubuntu:latest'},
    {...BINDING, compiler: 'sh -c command'}, {...BINDING, runId: -1},
    {...BINDING, recipeCommit: 'main'}, {...BINDING, binaryHashes: {}}]) {
    assert.throws(() => validateBinding(changed), /invalid_runtime_audit_binding/)
  }
  assert.throws(() => verifyIdentity(fixture()), /invalid_runtime_audit_binding/)
})
test('all actual runtime link-trace paths must be corroborated, but unused libraries are not invented', () => {
  const {report, linked} = runtimeFixture()
  report.archives[0].linkTraceFiles = []
  assert.throws(() => verifyRuntimeReport(report, linked, BINDING), /runtime_not_found/)
  linked.delete(report.archives[0].path)
  assert.doesNotThrow(() => verifyRuntimeReport(report, linked, BINDING))
})

async function artifactFixture(fn) {
  const root = await mkdtemp(resolve(tmpdir(), 'duskcut-runtime-audit-'))
  const artifact = resolve(root, 'artifact')
  const archived = resolve(root, 'archived')
  const bytes = value => Buffer.from(JSON.stringify(value, null, 2) + '\n')
  const sha = value => createHash('sha256').update(value).digest('hex')
  const put = async (base, name, value) => {
    const file = resolve(base, name)
    await mkdir(dirname(file), {recursive:true})
    await writeFile(file, value)
  }
  const run = {...fixture().run, id:123, head_sha:'c'.repeat(40)}
  const lock = {schemaVersion:2, buildId:'unit-test.2', ffmpeg:{revision:'a'.repeat(40)},
    recipe:{revision:'b'.repeat(40)}, toolchain:{image:BINDING.image}}
  const result = {schemaVersion:1, buildId:lock.buildId, ffmpegRevision:lock.ffmpeg.revision,
    recipeRevision:lock.recipe.revision, lockSha256:sha(bytes(lock)), status:'built-not-approved-for-distribution',
    binaryHashes:{'ffmpeg.exe':sha('MZ ffmpeg test'), 'ffprobe.exe':sha('MZ ffprobe test')}}
  try {
    const evidenceFiles = {
      'BUILD-RESULT.json':bytes(result), 'inputs/build-lock.json':bytes(lock),
      'work/toolchain-image.json':bytes({reference:BINDING.image,Id:BINDING.imageId,Os:'linux',Architecture:'amd64'}),
      'work/configuration/build-environment.json':bytes({CC:BINDING.compiler}),
      'work/configuration/compiler-version.txt':Buffer.from(BINDING.compilerVersion+'\n')}
    for(const [file,data] of Object.entries(evidenceFiles)) {
      await put(archived,file,data)
      await put(artifact,file,data)
    }
    await put(artifact,'BUILD-RECIPE-COMMIT.txt',run.head_sha+'\n')
    await put(artifact,'BUILD-RECIPE-STATUS.txt','')
    await put(artifact,'work/binary/bin/ffmpeg.exe','MZ ffmpeg test')
    await put(artifact,'work/binary/bin/ffprobe.exe','MZ ffprobe test')
    const evidence = resolve(artifact,'ffmpeg-build-evidence.tar.xz')
    execFileSync('tar',['-cJf',evidence,'-C',archived,...Object.keys(evidenceFiles)])
    await put(artifact,'SHA256SUMS.txt',sha(await readFile(evidence))+'  ffmpeg-build-evidence.tar.xz\n')
    const runFile = resolve(root,'run.json')
    await writeFile(runFile,bytes(run))
    await fn({artifact,runFile,result,put,bytes})
  } finally {await rm(root,{recursive:true,force:true})}
}
test('audit binding derives from actual binary bytes and identical archived records', () => artifactFixture(async({artifact,runFile})=>{
  const {binding}=await readBuildBinding(artifact,runFile)
  assert.equal(binding.buildId,'unit-test.2')
  assert.equal(binding.runId,123)
  assert.notDeepEqual(binding.binaryHashes,BINDING.binaryHashes)
}))
test('changed standalone build records are rejected even if internally consistent', () => artifactFixture(async({artifact,runFile,put,bytes})=>{
  await put(artifact,'work/toolchain-image.json',bytes({reference:BINDING.image,Id:'sha256:'+'f'.repeat(64),Os:'linux',Architecture:'amd64'}))
  await assert.rejects(readBuildBinding(artifact,runFile),/standalone_and_archived/)
}))
test('mutated executable bytes and dirty build recipes never yield an audit binding', () => artifactFixture(async({artifact,runFile,put})=>{
  await put(artifact,'work/binary/bin/ffmpeg.exe','MZ modified executable')
  await assert.rejects(readBuildBinding(artifact,runFile),/actual_binary_hash/)
  await put(artifact,'BUILD-RECIPE-STATUS.txt',' M build/profile.json')
  await assert.rejects(readBuildBinding(artifact,runFile),/dirty_build_recipe/)
}))
