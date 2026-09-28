/** Read-only post-build runtime provenance; does not rebuild or publish binaries. */
import { createHash } from 'node:crypto'
import { createReadStream, existsSync, readFileSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { posix, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

export const BINDING = Object.freeze({
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

export function verifyIdentity({ run, result, image, environment, recipeCommit, compilerVersion }) {
  if (run.id !== BINDING.runId || run.repository?.full_name !== BINDING.repository ||
      run.status !== 'completed' || run.conclusion !== 'success' || run.head_sha !== BINDING.recipeCommit ||
      run.path !== '.github/workflows/build.yml') throw new Error('unexpected_or_unsuccessful_build_run')
  if (recipeCommit !== BINDING.recipeCommit || result.lockSha256 !== BINDING.lockSha256 ||
      result.ffmpegRevision !== BINDING.ffmpegRevision || result.buildId !== '9.0.2-duskcut.1' ||
      result.status !== 'built-not-approved-for-distribution') throw new Error('build_identity_mismatch')
  for (const [name, hash] of Object.entries(BINDING.binaryHashes)) {
    if (result.binaryHashes?.[name] !== hash) throw new Error('binary_identity_mismatch')
  }
  if (image.reference !== BINDING.image || image.Id !== BINDING.imageId || image.Os !== 'linux' ||
      image.Architecture !== 'amd64') throw new Error('compiler_image_identity_mismatch')
  if (environment.CC !== BINDING.compiler || compilerVersion !== BINDING.compilerVersion) throw new Error('compiler_identity_mismatch')
}

export function verifyRuntimeReport(report, linkedPaths) {
  if (report.compiler !== BINDING.compiler || report.compilerVersion !== BINDING.compilerVersion ||
      !Array.isArray(report.archives) || report.archives.length !== RUNTIMES.length) throw new Error('runtime_report_identity_mismatch')
  const names = new Set()
  for (const entry of report.archives) {
    if (!RUNTIMES.includes(entry.name) || names.has(entry.name) ||
        !Number.isSafeInteger(entry.bytes) || entry.bytes < 8 || entry.bytes > 100 * 1024 * 1024 ||
        !/^[a-f0-9]{64}$/.test(entry.sha256) || !entry.path?.startsWith('/opt/ct-ng/') ||
        entry.path.split('/').includes('..') || !entry.path.endsWith('/' + entry.name)) throw new Error('invalid_runtime_archive_record')
    names.add(entry.name)
    if (['libatomic.a', 'libgomp.a'].includes(entry.name) && !linkedPaths.has(entry.path)) throw new Error('runtime_not_found_in_actual_link_trace')
  }
}

export function containerArguments(image) {
  if (image !== BINDING.image) throw new Error('unreviewed_compiler_image')
  return ['run', '--rm', '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
    '--security-opt=no-new-privileges', '--user', '65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
    '-i', image, 'python3', '-', BINDING.compiler, ...RUNTIMES]
}

export function linkedArchivePaths(log) {
  return new Set(log.split(/\r?\n/).map(line => line.trim())
    .filter(line => /^\/opt\/ct-ng\/[^\s]+\.a$/.test(line)).map(line => posix.normalize(line)))
}

const INSPECT = `import hashlib,json,pathlib,subprocess,sys
cc=sys.argv[1]
records=[]
for name in sys.argv[2:]:
    path=pathlib.Path(subprocess.check_output([cc,'-print-file-name='+name],text=True).strip()).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to('/opt/ct-ng') or path.name!=name:
        raise SystemExit('Runtime archive is outside the fixed compiler tree')
    with path.open('rb') as stream:
        if stream.read(8)!=b'!<arch>\\n': raise SystemExit('Expected an ar archive')
        stream.seek(0)
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    records.append({'name':name,'path':str(path),'bytes':path.stat().st_size,'sha256':digest})
print(json.dumps({'compiler':cc,'compilerVersion':subprocess.check_output([cc,'--version'],text=True).splitlines()[0],'archives':records}))
`

async function hash(path) {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(path)) digest.update(chunk)
  return digest.digest('hex')
}

export async function audit(artifactArg, runArg, outputArg) {
  if (process.platform !== 'linux') throw new Error('audit_requires_linux_docker_host')
  const artifact = resolve(artifactArg)
  const readJson = file => JSON.parse(readFileSync(resolve(artifact, file), 'utf8'))
  const result = readJson('BUILD-RESULT.json')
  const image = readJson('work/toolchain-image.json')
  const run = JSON.parse(readFileSync(runArg, 'utf8'))
  verifyIdentity({ run, result, image, environment: readJson('work/configuration/build-environment.json'),
    recipeCommit: readFileSync(resolve(artifact, 'BUILD-RECIPE-COMMIT.txt'), 'utf8').trim(),
    compilerVersion: readFileSync(resolve(artifact, 'work/configuration/compiler-version.txt'), 'utf8').split(/\r?\n/)[0] })
  if (readFileSync(resolve(artifact, 'BUILD-RECIPE-STATUS.txt'), 'utf8').trim()) throw new Error('dirty_build_recipe')
  for (const [name, expected] of Object.entries(BINDING.binaryHashes)) {
    if (await hash(resolve(artifact, 'work/binary/bin', name)) !== expected) throw new Error('actual_binary_hash_mismatch')
  }
  const evidence = resolve(artifact, 'ffmpeg-build-evidence.tar.xz')
  const evidenceSha256 = await hash(evidence)
  const sums = readFileSync(resolve(artifact, 'SHA256SUMS.txt'), 'utf8').trim().split(/\r?\n/)
  if (!sums.includes(evidenceSha256 + '  ffmpeg-build-evidence.tar.xz')) throw new Error('build_evidence_hash_mismatch')
  const lockBytes = execFileSync('tar', ['-xOf', evidence, 'inputs/build-lock.json'])
  if (createHash('sha256').update(lockBytes).digest('hex') !== BINDING.lockSha256) throw new Error('archived_build_lock_mismatch')
  const compileLog = execFileSync('tar', ['-xOf', evidence, 'work/ffmpeg-build.log'], { encoding: 'utf8', maxBuffer: 128 * 1024 * 1024 })
  // GNU ld --trace emits one archive path per line. Compiler commands may also
  // contain these paths, but only the standalone trace records count here.
  const linkedPaths = linkedArchivePaths(compileLog)
  const inspected = JSON.parse(execFileSync('docker', ['image', 'inspect', BINDING.image], { encoding: 'utf8' }))[0]
  if (inspected.Id !== BINDING.imageId || !inspected.RepoDigests?.includes(BINDING.image)) throw new Error('local_image_does_not_match_build_toolchain')
  const report = JSON.parse(execFileSync('docker', containerArguments(BINDING.image), { input: INSPECT, encoding: 'utf8', maxBuffer: 1024 * 1024 }))
  verifyRuntimeReport(report, linkedPaths)
  const output = resolve(outputArg)
  if (existsSync(output)) throw new Error('audit_output_already_exists')
  await mkdir(output, { recursive: true })
  const record = {
    schemaVersion: 1, kind: 'post-build-toolchain-runtime-provenance', recordedAt: new Date().toISOString(),
    build: { repository: BINDING.repository, runId: run.id, recipeCommit: BINDING.recipeCommit,
      lockSha256: BINDING.lockSha256, binaryHashes: BINDING.binaryHashes, evidenceSha256 },
    toolchain: { image: BINDING.image, imageId: BINDING.imageId, compiler: report.compiler, compilerVersion: report.compilerVersion },
    archives: report.archives.map(entry => ({ ...entry, presentInBuildLinkTrace: linkedPaths.has(entry.path) })),
    inspection: { network: 'none', imageFilesystem: 'read-only', unprivilegedUid: 65534, mountedHostPaths: [] },
    scope: 'Records exact runtime archives from the original pinned compiler image. Does not modify the original build, source lock, or binaries; is not a legal approval.',
  }
  const file = resolve(output, 'libatomic-toolchain-audit.json')
  await writeFile(file, JSON.stringify(record, null, 2) + '\n', { flag: 'wx' })
  await writeFile(resolve(output, 'SHA256SUMS.txt'), `${await hash(file)}  libatomic-toolchain-audit.json\n`, { flag: 'wx' })
  return { file, sha256: await hash(file), archives: report.archives.length }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 5) throw new Error('Usage: node scripts/audit-toolchain-runtime.mjs <build-artifact-dir> <run-metadata.json> <new-output-dir>')
  console.log(JSON.stringify(await audit(...process.argv.slice(2))))
}
