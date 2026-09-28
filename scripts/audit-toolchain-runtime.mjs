/** Read-only post-build runtime provenance; does not rebuild or publish binaries. */
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, readFileSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { posix, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

export const REPOSITORY = 'pascalghanimi/duskcut-ffmpeg'
// Retained solely as a regression fixture for the original completed .1 audit.
// New runs derive a distinct binding from their successful GitHub build evidence.
export const BINDING = Object.freeze({
  buildId: '9.0.2-duskcut.1',
  repository: 'pascalghanimi/duskcut-ffmpeg', runId: 36355675824,
  recipeCommit: '323eb1d63d630b4bd4e8ae4c89650266534731e8',
  lockSha256: '9dc31b6bf68dc0f83f544efe9c601266c53ed1556521a10f8c331adf9598f848',
  ffmpegRevision: 'c867e135494d97b27158d17837d254bbbb8d4f7b',
  image: 'ghcr.io/btbn/ffmpeg-builds/base-win64@sha256:ca6aaa981df5fe20d341f2f7e681875266adddabe117add1367765471d7d30ea',
  imageId: 'sha256:f3efcaafafeed4d2cf00c551a0c492b38379a08934f20f6be99fa5584851572e',
  compiler: 'x86_64-w64-mingw32-gcc',
  compilerVersion: 'x86_64-w64-mingw32-gcc (crosstool-NG 1.29.0.7_b1a94f6) 16.2.0',
  binaryHashes: Object.freeze({
    'ffmpeg.exe': '77ea4c94db1676801cc372302c4a18422e70213e9c915207a798bc05390ceab5',
    'ffprobe.exe': '923dc6aa82ba8de3a1b25ccfb2c5809d5e5954e6e040abb40c2817213c1272da',
  }),
})
export const RUNTIMES = Object.freeze(['libatomic.a', 'libgomp.a', 'libgcc.a', 'libgcc_eh.a', 'libstdc++.a'])

export function validateBinding(binding) {
  if (binding?.repository !== REPOSITORY || !Number.isSafeInteger(binding.runId) || binding.runId < 1 ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(binding.buildId || '') ||
      !/^[a-f0-9]{40}$/.test(binding.recipeCommit || '') || !/^[a-f0-9]{40}$/.test(binding.ffmpegRevision || '') ||
      !/^[a-f0-9]{64}$/.test(binding.lockSha256 || '') ||
      !/^ghcr\.io\/btbn\/ffmpeg-builds\/base-win64@sha256:[a-f0-9]{64}$/.test(binding.image || '') ||
      !/^sha256:[a-f0-9]{64}$/.test(binding.imageId || '') ||
      binding.compiler !== 'x86_64-w64-mingw32-gcc' ||
      typeof binding.compilerVersion !== 'string' || binding.compilerVersion.length > 200 ||
      !/^x86_64-w64-mingw32-gcc \([A-Za-z0-9 ._+-]+\) [0-9]+\.[0-9]+\.[0-9]+$/.test(binding.compilerVersion) ||
      Object.keys(binding.binaryHashes || {}).sort().join(',') !== 'ffmpeg.exe,ffprobe.exe' ||
      Object.values(binding.binaryHashes || {}).some(value => !/^[a-f0-9]{64}$/.test(value))) {
    throw new Error('invalid_runtime_audit_binding')
  }
  return binding
}

export function verifyIdentity({ run, result, image, environment, recipeCommit, compilerVersion }, binding) {
  validateBinding(binding)
  if (run.id !== binding.runId || run.repository?.full_name !== binding.repository ||
      run.status !== 'completed' || run.conclusion !== 'success' || run.head_sha !== binding.recipeCommit ||
      run.head_branch !== 'main' || run.event !== 'workflow_dispatch' ||
      run.path !== '.github/workflows/build.yml') throw new Error('unexpected_or_unsuccessful_build_run')
  if (recipeCommit !== binding.recipeCommit || result.schemaVersion !== 1 || result.lockSha256 !== binding.lockSha256 ||
      result.ffmpegRevision !== binding.ffmpegRevision || result.buildId !== binding.buildId ||
      result.status !== 'built-not-approved-for-distribution') throw new Error('build_identity_mismatch')
  for (const [name, hash] of Object.entries(binding.binaryHashes)) {
    if (result.binaryHashes?.[name] !== hash) throw new Error('binary_identity_mismatch')
  }
  if (image.reference !== binding.image || image.Id !== binding.imageId || image.Os !== 'linux' ||
      image.Architecture !== 'amd64') throw new Error('compiler_image_identity_mismatch')
  if (environment.CC !== binding.compiler || compilerVersion !== binding.compilerVersion) throw new Error('compiler_identity_mismatch')
}

export function deriveBinding(data, lock, lockSha256) {
  const { run, result, image, environment, recipeCommit, compilerVersion } = data
  const binding = validateBinding({ repository: REPOSITORY, runId: run.id, buildId: result.buildId,
    recipeCommit, lockSha256: result.lockSha256, ffmpegRevision: result.ffmpegRevision,
    image: image.reference, imageId: image.Id, compiler: environment.CC, compilerVersion,
    binaryHashes: { ...result.binaryHashes } })
  verifyIdentity(data, binding)
  if (lock.schemaVersion !== 2 || lock.buildId !== binding.buildId || lockSha256 !== binding.lockSha256 ||
      lock.ffmpeg?.revision !== binding.ffmpegRevision || lock.recipe?.revision !== result.recipeRevision ||
      lock.toolchain?.image !== binding.image) throw new Error('archived_build_lock_identity_mismatch')
  return binding
}

export function verifyRuntimeReport(report, linkedPaths, binding) {
  validateBinding(binding)
  if (report.compiler !== binding.compiler || report.compilerVersion !== binding.compilerVersion ||
      !Array.isArray(report.archives) || report.archives.length !== RUNTIMES.length) throw new Error('runtime_report_identity_mismatch')
  const names = new Set()
  for (const entry of report.archives) {
    if (!RUNTIMES.includes(entry.name) || names.has(entry.name) ||
        !Number.isSafeInteger(entry.bytes) || entry.bytes < 8 || entry.bytes > 100 * 1024 * 1024 ||
        !/^[a-f0-9]{64}$/.test(entry.sha256) || !entry.path?.startsWith('/opt/ct-ng/') ||
        entry.path.split('/').includes('..') || !entry.path.endsWith('/' + entry.name)) throw new Error('invalid_runtime_archive_record')
    names.add(entry.name)
    if (!Array.isArray(entry.linkTraceFiles)) throw new Error('missing_resolved_link_trace')
    for (const traced of entry.linkTraceFiles) {
      if (!linkedPaths.has(traced.recordedPath) || posix.basename(traced.recordedPath) !== entry.name ||
          traced.resolvedPath !== entry.path || traced.bytes !== entry.bytes || traced.sha256 !== entry.sha256) throw new Error('link_trace_archive_does_not_match_compiler_archive')
    }
    const expectedPaths = [...linkedPaths].filter(path => posix.basename(path) === entry.name)
    if (new Set(entry.linkTraceFiles.map(trace => trace.recordedPath)).size !== entry.linkTraceFiles.length ||
        expectedPaths.some(path => !entry.linkTraceFiles.some(trace => trace.recordedPath === path))) {
      throw new Error('runtime_not_found_in_actual_link_trace')
    }
  }
}

export function containerArguments(image, linkedPaths, binding) {
  validateBinding(binding)
  if (image !== binding.image) throw new Error('unreviewed_compiler_image')
  const paths = [...linkedPaths].filter(path => RUNTIMES.includes(posix.basename(path)))
  if (paths.length > 100 || paths.some(path => !/^\/opt\/ct-ng\/[A-Za-z0-9_./+-]+\.a$/.test(path) || !posix.normalize(path).startsWith('/opt/ct-ng/'))) throw new Error('unsafe_link_trace_path')
  return ['run', '--rm', '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
    '--security-opt=no-new-privileges', '--user', '65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
    '-i', image, 'python3', '-', binding.compiler, JSON.stringify(paths), ...RUNTIMES]
}

export function linkedArchivePaths(log) {
  // Preserve the exact ld path: lexical '..' normalization before resolving a
  // symlink can select a different file. Resolve inside the original image.
  return new Set(log.split(/\r?\n/).map(line => line.trim())
    .filter(line => /^\/opt\/ct-ng\/[^\s]+\.a$/.test(line)))
}

const INSPECT = `import hashlib,json,pathlib,subprocess,sys
cc=sys.argv[1]
linked_paths=json.loads(sys.argv[2])
records=[]
def identity(raw_path,name):
    path=pathlib.Path(raw_path).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to('/opt/ct-ng') or path.name!=name:
        raise ValueError('Runtime archive is outside the fixed compiler tree')
    with path.open('rb') as stream:
        if stream.read(8)!=b'!<arch>\\n': raise ValueError('Expected an ar archive')
        stream.seek(0)
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'path':str(path),'bytes':path.stat().st_size,'sha256':digest}
for name in sys.argv[3:]:
    entry={'name':name,'linkTraceFiles':[]}
    try:
        reported_path=subprocess.check_output([cc,'-print-file-name='+name],text=True).strip()
        entry.update({'reportedPath':reported_path,**identity(reported_path,name)})
        for linked in linked_paths:
            if pathlib.PurePosixPath(linked).name==name:
                try:
                    info=identity(linked,name)
                    entry['linkTraceFiles'].append({'recordedPath':linked,'resolvedPath':info['path'],'bytes':info['bytes'],'sha256':info['sha256']})
                except Exception as error:
                    entry['linkTraceFiles'].append({'recordedPath':linked,'errorType':type(error).__name__})
    except Exception as error:
        entry['errorType']=type(error).__name__
    records.append(entry)
print(json.dumps({'compiler':cc,'compilerVersion':subprocess.check_output([cc,'--version'],text=True).splitlines()[0],'archives':records}))
`

async function hash(path) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(path)) digest.update(chunk)
  return digest.digest('hex')
}

export async function readBuildBinding(artifactArg, runArg) {
  const artifact = resolve(artifactArg)
  const readJson = file => JSON.parse(readFileSync(resolve(artifact, file), 'utf8'))
  const result = readJson('BUILD-RESULT.json')
  const image = readJson('work/toolchain-image.json')
  const run = JSON.parse(readFileSync(runArg, 'utf8'))
  const data = { run, result, image, environment: readJson('work/configuration/build-environment.json'),
    recipeCommit: readFileSync(resolve(artifact, 'BUILD-RECIPE-COMMIT.txt'), 'utf8').trim(),
    compilerVersion: readFileSync(resolve(artifact, 'work/configuration/compiler-version.txt'), 'utf8').split(/\r?\n/)[0] }
  if (readFileSync(resolve(artifact, 'BUILD-RECIPE-STATUS.txt'), 'utf8').trim()) throw new Error('dirty_build_recipe')
  const evidence = resolve(artifact, 'ffmpeg-build-evidence.tar.xz')
  const evidenceSha256 = await hash(evidence)
  const sums = readFileSync(resolve(artifact, 'SHA256SUMS.txt'), 'utf8').trim().split(/\r?\n/)
  if (!sums.includes(evidenceSha256 + '  ffmpeg-build-evidence.tar.xz')) throw new Error('build_evidence_hash_mismatch')
  const lockBytes = execFileSync('tar', ['-xOf', evidence, 'inputs/build-lock.json'])
  const lockSha256 = createHash('sha256').update(lockBytes).digest('hex')
  const binding = deriveBinding(data, JSON.parse(lockBytes), lockSha256)
  for (const file of ['BUILD-RESULT.json', 'work/toolchain-image.json',
    'work/configuration/build-environment.json', 'work/configuration/compiler-version.txt']) {
    const archived = execFileSync('tar', ['-xOf', evidence, file], { maxBuffer: 8 * 1024 * 1024 })
    if (!archived.equals(readFileSync(resolve(artifact, file)))) throw new Error('standalone_and_archived_build_evidence_differ:' + file)
  }
  for (const [name, expected] of Object.entries(binding.binaryHashes)) {
    if (await hash(resolve(artifact, 'work/binary/bin', name)) !== expected) throw new Error('actual_binary_hash_mismatch')
  }
  return { binding, evidence, evidenceSha256 }
}

export async function audit(artifactArg, runArg, bindingArg, outputArg) {
  if (process.platform !== 'linux') throw new Error('audit_requires_linux_docker_host')
  const expected = validateBinding(JSON.parse(readFileSync(bindingArg, 'utf8')))
  const { binding, evidence, evidenceSha256 } = await readBuildBinding(artifactArg, runArg)
  if (JSON.stringify(binding) !== JSON.stringify(expected)) throw new Error('prepared_audit_binding_changed')
  const compileLog = execFileSync('tar', ['-xOf', evidence, 'work/ffmpeg-build.log'], { encoding: 'utf8', maxBuffer: 128 * 1024 * 1024 })
  // GNU ld --trace emits one archive path per line. Compiler commands may also
  // contain these paths, but only the standalone trace records count here.
  const linkedPaths = linkedArchivePaths(compileLog)
  const inspected = JSON.parse(execFileSync('docker', ['image', 'inspect', binding.image], { encoding: 'utf8' }))[0]
  if (inspected.Id !== binding.imageId || !inspected.RepoDigests?.includes(binding.image)) throw new Error('local_image_does_not_match_build_toolchain')
  const report = JSON.parse(execFileSync('docker', containerArguments(binding.image, linkedPaths, binding), { input: INSPECT, encoding: 'utf8', maxBuffer: 1024 * 1024 }))
  const output = resolve(outputArg)
  if (existsSync(output)) throw new Error('audit_output_already_exists')
  await mkdir(output, { recursive: true })
  // Keep public, bounded compiler-path/hash evidence even if the final linkage
  // check fails. It is explicitly not a completed or approved audit record.
  await writeFile(resolve(output, 'runtime-audit-diagnostics.json'), JSON.stringify({
    schemaVersion: 1, status: 'captured-before-linkage-verification', buildRunId: binding.runId,
    lockSha256: binding.lockSha256, image: binding.image, imageId: binding.imageId,
    linkedArchivePaths: [...linkedPaths], inspection: report,
  }, null, 2) + '\n', { flag: 'wx' })
  verifyRuntimeReport(report, linkedPaths, binding)
  const record = {
    schemaVersion: 1, kind: 'post-build-toolchain-runtime-provenance', recordedAt: new Date().toISOString(),
    build: { repository: binding.repository, runId: binding.runId, buildId: binding.buildId, recipeCommit: binding.recipeCommit,
      lockSha256: binding.lockSha256, binaryHashes: binding.binaryHashes, evidenceSha256 },
    toolchain: { image: binding.image, imageId: binding.imageId, compiler: report.compiler, compilerVersion: report.compilerVersion },
    archives: report.archives.map(entry => ({ ...entry, presentInBuildLinkTrace: entry.linkTraceFiles.length > 0 })),
    inspection: { network: 'none', imageFilesystem: 'read-only', unprivilegedUid: 65534, mountedHostPaths: [] },
    scope: 'Records exact runtime archives from the original pinned compiler image. Does not modify the original build, source lock, or binaries; is not a legal approval.',
  }
  const file = resolve(output, 'libatomic-toolchain-audit.json')
  await writeFile(file, JSON.stringify(record, null, 2) + '\n', { flag: 'wx' })
  await writeFile(resolve(output, 'runtime-audit-binding.json'), JSON.stringify(binding, null, 2) + '\n', { flag: 'wx' })
  await writeFile(resolve(output, 'SHA256SUMS.txt'), `${await hash(file)}  libatomic-toolchain-audit.json\n`, { flag: 'wx' })
  return { file, sha256: await hash(file), archives: report.archives.length }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 6) throw new Error('Usage: node scripts/audit-toolchain-runtime.mjs <build-artifact-dir> <run-metadata.json> <prepared-binding.json> <new-output-dir>')
  console.log(JSON.stringify(await audit(...process.argv.slice(2))))
}
