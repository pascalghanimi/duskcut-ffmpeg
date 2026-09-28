/** Download one successful public build and derive its immutable audit binding.
 * No guessed release hashes, mutable compiler tags, rebuild, or release mutation.
 */
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { readBuildBinding, REPOSITORY } from './audit-toolchain-runtime.mjs'

export function parseRunId(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]{0,15}$/.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new Error('build_run_must_be_a_positive_safe_integer')
  }
  return Number(value)
}

export function validateRunMetadata(run, runId) {
  if (run?.id !== runId || run.repository?.full_name !== REPOSITORY || run.status !== 'completed' ||
      run.conclusion !== 'success' || run.event !== 'workflow_dispatch' || run.head_branch !== 'main' ||
      run.path !== '.github/workflows/build.yml' || !/^[a-f0-9]{40}$/.test(run.head_sha || '')) {
    throw new Error('unexpected_or_unsuccessful_build_run')
  }
}

export function selectBuildArtifact(list, run) {
  if (!Array.isArray(list?.artifacts) || list.total_count !== list.artifacts.length) throw new Error('incomplete_artifact_listing')
  const rows = list.artifacts.filter(row => new RegExp('^duskcut-ffmpeg-([A-Za-z0-9][A-Za-z0-9_.-]{0,99})-' + run.id + '$').test(row.name || ''))
  if (rows.length !== 1) throw new Error('missing_or_duplicate_build_artifact')
  const row = rows[0]
  if (!Number.isSafeInteger(row.id) || row.id < 1 || row.expired !== false ||
      row.workflow_run?.id !== run.id || row.workflow_run?.head_sha !== run.head_sha) {
    throw new Error('expired_or_wrong_build_artifact')
  }
  return { ...row, buildId: row.name.slice('duskcut-ffmpeg-'.length, -String(run.id).length - 1) }
}

export async function prepare(runValue = process.env.BUILD_RUN) {
  const runId = parseRunId(runValue)
  for (const path of ['audited-build', 'run-metadata.json', 'runtime-audit-binding.json']) {
    if (existsSync(path)) throw new Error('audit_preparation_output_already_exists:' + path)
  }
  const api = suffix => JSON.parse(execFileSync('gh', ['api', `repos/${REPOSITORY}/actions/runs/${runId}${suffix}`],
    { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }))
  const run = api('')
  validateRunMetadata(run, runId)
  const artifact = selectBuildArtifact(api('/artifacts?per_page=100'), run)
  await mkdir('audited-build')
  await writeFile('run-metadata.json', JSON.stringify(run, null, 2) + '\n', { flag: 'wx' })
  execFileSync('gh', ['run', 'download', String(runId), '--repo', REPOSITORY,
    '--name', artifact.name, '--dir', 'audited-build'], { stdio: 'inherit' })
  const { binding } = await readBuildBinding('audited-build', 'run-metadata.json')
  if (binding.buildId !== artifact.buildId) throw new Error('artifact_name_and_build_identity_differ')
  await writeFile('runtime-audit-binding.json', JSON.stringify(binding, null, 2) + '\n', { flag: 'wx' })
  return { buildId: binding.buildId, runId, image: binding.image, binding: 'runtime-audit-binding.json' }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  console.log(JSON.stringify(await prepare()))
}
