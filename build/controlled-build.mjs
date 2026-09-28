/** Offline dependency/FFmpeg compilation using verified source inputs and an already-installed Docker engine. */
import { spawn, execFileSync } from 'node:child_process'
import { createWriteStream, existsSync, readFileSync } from 'node:fs'
import { copyFile, mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { hashFile, validateLock } from './validate-lock.mjs'

const scripts = dirname(fileURLToPath(import.meta.url))
export const scriptNames = ['container-generate.sh', 'container-compile.sh', 'safe_extract.py', 'capture_environment.py', 'prepare_recipe.py', 'profile.json',
  'apply_windows_codecs.py', 'verify_codec_profile.py', 'windows-codecs/apply.py', 'windows-codecs/mfdec.c']
export function publicImageRecord(info, reference) {
  if (!/^sha256:[a-f0-9]{64}$/.test(info?.Id || '')) throw new Error('invalid_image_identity')
  // Config.Env, Labels, History, author strings, private registry aliases,
  // Entrypoint and other arbitrary image fields are never exported.
  const record = { Id: info.Id, reference }
  for (const key of ['Architecture', 'Os', 'Variant']) {
    if (info[key] !== undefined) {
      if (!/^[a-z0-9_-]{1,32}$/.test(info[key])) throw new Error('unsafe_image_platform_metadata')
      record[key] = info[key]
    }
  }
  if (Number.isSafeInteger(info.Size) && info.Size >= 0) record.Size = info.Size
  return record
}
export function publicDockerVersion(info) {
  const formats = {
    Version: /^v?\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/,
    ApiVersion: /^\d+\.\d+$/, MinAPIVersion: /^\d+\.\d+$/,
    GitCommit: /^[a-f0-9]{7,40}$/i, GoVersion: /^go\d+\.\d+(?:\.\d+)?$/,
    Os: /^(?:linux|windows|darwin)$/, Arch: /^(?:amd64|arm64|arm|386|ppc64le|s390x|riscv64)$/,
  }
  const versions = {}
  for (const side of ['Client', 'Server']) {
    versions[side] = {}
    for (const key of ['Version', 'ApiVersion', 'MinAPIVersion', 'GitCommit', 'GoVersion', 'Os', 'Arch']) {
      const value = info[side]?.[key]
      if (typeof value === 'string' && formats[key].test(value)) versions[side][key] = value
    }
  }
  return versions
}
export function pinGeneratedDockerfile(contents, image) {
  const expected = 'FROM ghcr.io/btbn/ffmpeg-builds/base-win64:latest AS base-layer'
  if (contents.split(expected).length !== 2) throw new Error('unexpected_generated_base_image')
  const result = contents.replace(expected, `FROM ${image} AS base-layer`)
  for (const line of result.split('\n')) {
    if (!line.startsWith('FROM ')) continue
    const from = line.split(/\s+/)[1]
    if (from !== image && from !== 'base-layer') throw new Error(`unrecorded_external_build_image:${from}`)
  }
  // No mutable external COPY --from= registry source may slip into the recipe.
  if (/--from=[^\s]*[@/]/.test(result)) throw new Error('unrecorded_external_copy_image')
  return result
}
export function serializeDependencyStages(contents) {
  // Upstream models independent libraries as parallel Docker stages. On a
  // normal GitHub runner each library already consumes all available cores;
  // a small completion marker adds a dependency between successive stages.
  const lines = contents.split('\n')
  const output = []
  let previous
  let current
  for (const line of lines) {
    if (line.startsWith('FROM ')) {
      if (current) {
        output.push('RUN mkdir -p /duskcut-control && touch /duskcut-control/stage-complete')
        previous = current
      }
      const match = /^FROM base-layer AS ([a-z0-9-]+)$/.exec(line)
      current = match && match[1] !== 'combine-layer' ? match[1] : undefined
      output.push(line)
      if (current && previous) output.push(`COPY --from=${previous} /duskcut-control/stage-complete /duskcut-control/previous-stage`)
    } else output.push(line)
  }
  return output.join('\n')
}
async function run(command, args, logFile) {
  const output = createWriteStream(logFile, { flags: 'wx' })
  const child = spawn(command, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let outputError
  output.on('error', error => { outputError = error; child.kill() })
  child.stdout.pipe(output, { end: false })
  child.stderr.pipe(output, { end: false })
  let code
  try {
    code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
  } catch (error) {
    output.destroy()
    throw error
  }
  await new Promise((accept, reject) => { output.once('error', reject); output.end(accept) })
  if (outputError) throw outputError
  if (code !== 0) throw new Error(`command_failed:${command}:exit_${code}; details retained at ${logFile}`)
}
function dockerInspect(image) {
  const info = JSON.parse(execFileSync('docker', ['image', 'inspect', image], { encoding: 'utf8' }))[0]
  if (!info?.Id || !info?.RepoDigests?.includes(image)) throw new Error('local_toolchain_image_does_not_match_pinned_digest')
  return info
}
function containerArgs(image, inputs, work, phase) {
  return ['run', '--rm', '--pull=never', '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
    '--user', `${process.getuid()}:${process.getgid()}`, '--env', 'HOME=/tmp',
    '--mount', `type=bind,src=${inputs},dst=/inputs,readonly`, '--mount', `type=bind,src=${work},dst=/work`,
    '--workdir', '/work', image, 'bash', `/work/control/${phase}.sh`]
}
export async function build(lockFile, outputRoot) {
  if (process.platform !== 'linux') throw new Error('controlled_build_requires_linux_docker_host; no_OS_features_or_remote_jobs_enabled')
  const checked = await validateLock(lockFile)
  if (checked.lock.schemaVersion !== 2) throw new Error('selected_source_build_requires_schema_2_lock')
  if (await hashFile(resolve(scripts, 'profile.json')) !== checked.lock.profile.sha256) throw new Error('build_profile_integrity_mismatch')
  const output = resolve(outputRoot)
  if (existsSync(output)) throw new Error('output_exists; use_a_new_dedicated_directory')
  // Inspection never pulls or mutates the remote registry. The operator must have
  // provisioned this exact compiler image; mutable latest tags are not accepted.
  const image = dockerInspect(checked.lock.toolchain.image)
  const inputs = resolve(output, 'inputs')
  const work = resolve(output, 'work')
  await mkdir(resolve(work, 'control'), { recursive: true })
  await mkdir(inputs)
  await copyFile(lockFile, resolve(inputs, 'build-lock.json'))
  // Preserve public source archives exactly as reviewed. No API keys, local app
  // state, source-code repository secrets, or user media are accepted as inputs.
  for (const blob of checked.blobs.values()) {
    const dest = resolve(inputs, blob.file)
    await mkdir(dirname(dest), { recursive: true })
    await copyFile(blob.path, dest)
    if (await hashFile(dest) !== blob.sha256) throw new Error(`copied_input_changed:${blob.id}`)
  }
  for (const file of scriptNames) {
    const destination = resolve(work, 'control', file)
    await mkdir(dirname(destination), { recursive: true })
    await copyFile(resolve(scripts, file), destination)
  }
  await writeFile(resolve(work, 'control', 'lock.json'), JSON.stringify(checked.lock, null, 2) + '\n', { flag: 'wx' })
  await writeFile(resolve(work, 'toolchain-image.json'), JSON.stringify(publicImageRecord(image, checked.lock.toolchain.image), null, 2) + '\n', { flag: 'wx' })
  const versionInfo = JSON.parse(execFileSync('docker', ['version', '--format', '{{json .}}'], { encoding: 'utf8' }))
  await writeFile(resolve(work, 'docker-version.json'), JSON.stringify(publicDockerVersion(versionInfo), null, 2) + '\n', { flag: 'wx' })
  await run('docker', containerArgs(checked.lock.toolchain.image, inputs, work, 'container-generate'), resolve(work, 'generate.log'))
  const recipe = resolve(work, 'recipe')
  const generated = readFileSync(resolve(recipe, 'Dockerfile'), 'utf8')
  const executedDockerfile = serializeDependencyStages(pinGeneratedDockerfile(generated, checked.lock.toolchain.image))
  await writeFile(resolve(recipe, 'Dockerfile.duskcut'), executedDockerfile, { flag: 'wx' })
  await writeFile(resolve(work, 'configuration/Dockerfile.duskcut'), executedDockerfile, { flag: 'wx' })
  const dependencyTag = `duskcut-controlled-ffmpeg-deps:${checked.lockSha256.slice(0, 24)}`
  // Every dependency build RUN is offline and freshly executed. The generated
  // Dockerfile refers only to the verified local sources plus pinned toolchain.
  await run('docker', ['build', '--pull=false', '--network=none', '--no-cache', '--progress=plain',
    '--file', resolve(recipe, 'Dockerfile.duskcut'), '--tag', dependencyTag, recipe], resolve(work, 'dependencies.log'))
  const dependencyInfo = JSON.parse(execFileSync('docker', ['image', 'inspect', dependencyTag], { encoding: 'utf8' }))[0]
  await writeFile(resolve(work, 'dependency-image.json'), JSON.stringify(publicImageRecord(dependencyInfo, dependencyInfo.Id), null, 2) + '\n', { flag: 'wx' })
  // Use the resulting local content-addressed image ID, not the mutable local tag.
  await run('docker', containerArgs(dependencyInfo.Id, inputs, work, 'container-compile'), resolve(work, 'ffmpeg-build.log'))
  const report = JSON.parse(readFileSync(resolve(work, 'compile-result.json'), 'utf8'))
  if (report.ffmpegRevision !== checked.lock.ffmpeg.revision || report.recipeRevision !== checked.lock.recipe.revision) throw new Error('built_source_identity_mismatch')
  for (const file of ['ffmpeg.exe', 'ffprobe.exe']) {
    const path = resolve(work, 'binary', 'bin', file)
    if (!existsSync(path)) throw new Error(`missing_built_binary:${file}`)
  }
  const final = { schemaVersion: 1, status: 'built-not-approved-for-distribution', lockSha256: checked.lockSha256,
    networkPolicy: 'dependency compile and FFmpeg compile ran with networking disabled',
    binaryHashes: { 'ffmpeg.exe': await hashFile(resolve(work, 'binary/bin/ffmpeg.exe')), 'ffprobe.exe': await hashFile(resolve(work, 'binary/bin/ffprobe.exe')) },
    sourceInputs: [...checked.blobs.values()].map(({ path, ...blob }) => blob), ...report }
  await writeFile(resolve(output, 'BUILD-RESULT.json'), JSON.stringify(final, null, 2) + '\n', { flag: 'wx' })
  // Exact source inputs are already independent immutable source release assets.
  // Preserve executed build controls, settings, compiler records and logs here;
  // the many gigabytes of intermediate object trees are not part of the bundle.
  const archive = resolve(output, 'ffmpeg-build-evidence.tar.xz')
  await run('tar', ['-I', 'xz -T2', '-cf', archive, '-C', output,
    'BUILD-RESULT.json', 'inputs/build-lock.json', 'work/control', 'work/configuration',
    'work/toolchain-build-environment.json', 'work/toolchain-image.json', 'work/docker-version.json',
    'work/dependency-image.json', 'work/compile-result.json', 'work/selected-source-cache-files.txt',
    'work/generate.log', 'work/dependencies.log', 'work/ffmpeg-build.log'], resolve(output, 'bundle.log'))
  await writeFile(resolve(output, 'SHA256SUMS.txt'), `${await hashFile(archive)}  ffmpeg-build-evidence.tar.xz\n`, { flag: 'wx' })
  return final
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 4) throw new Error('Usage: node controlled-build.mjs <build-lock.json> <new-output-directory>')
  console.log(JSON.stringify(await build(process.argv[2], process.argv[3])))
}
