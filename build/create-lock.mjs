/** Bind the reviewed public inputs to the deliberately selected build profile. */
import { createHash } from 'node:crypto'
import { readFileSync, statSync } from 'node:fs'
import { writeFile } from 'node:fs/promises'
import { basename, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { hashFile, validateLock } from './validate-lock.mjs'

const [manifestArg, buildId] = process.argv.slice(2)
if (!manifestArg || !buildId) throw new Error('Usage: node build/create-lock.mjs <unpacked-input-directory> <buildId>')
const directoryMode = statSync(resolve(manifestArg)).isDirectory()
const manifestFile = directoryMode ? resolve(manifestArg, 'source-manifest.json') : resolve(manifestArg)
const outputRoot = directoryMode ? resolve(manifestArg) : dirname(manifestFile)
const sourcePrefix = directoryMode ? 'sources/' : ''
const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'))
const supplementFile = resolve(outputRoot, 'sources/supplemental-manifest.json')
const supplement = JSON.parse(readFileSync(supplementFile, 'utf8'))
const release = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../release-assets.json'), 'utf8'))
const manifestSha256 = await hashFile(manifestFile)
const supplementSha256 = await hashFile(supplementFile)
if (release.releaseTag !== `sources-${buildId}` || manifestSha256 !== release.sourceManifestSha256 ||
    supplementSha256 !== release.supplementalManifestSha256) throw new Error('released_source_manifest_integrity_mismatch')
if (supplement.schemaVersion !== 1 || !Array.isArray(supplement.files) ||
    supplement.freetypeDlg?.id !== 'freetype-dlg' ||
    supplement.freetypeDlg.revision !== '395ccad2c1e0daae535c4d20bb0a3f2424648e17' ||
    supplement.freetypeDlg.parentRevision !== 'd333439633039de426f943f28a2926c7f97b5ae5') {
  throw new Error('missing_or_mismatched_freetype_submodule_manifest')
}
const profileFile = resolve(dirname(fileURLToPath(import.meta.url)), 'profile.json')
const profile = JSON.parse(readFileSync(profileFile, 'utf8'))
const stageNames = new Set(profile.sourceStages)
const roles = { 'dependency-source': 'source-cache', 'toolchain-recipe-source': 'source', 'runtime-license-evidence': 'license-evidence', 'runtime-review-evidence': 'review-evidence' }
const blobs = manifest.files.map(row => ({
  id: row.id, role: roles[row.role] || row.role,
  file: sourcePrefix + row.file, bytes: row.bytes, sha256: row.sha256, origin: row.origin,
})).filter(row => !row.file.startsWith(sourcePrefix + 'downloads/') || stageNames.has(row.id))
for (const row of supplement.files) {
  if (!row.file?.startsWith('sources/') || !['source', 'license-evidence', 'review-evidence'].includes(row.role)) throw new Error('invalid_supplement_file')
  blobs.push({ id: row.id, role: row.role, file: row.file, bytes: row.bytes, sha256: row.sha256, origin: row.origin })
}
const selected = manifest.files.filter(row => stageNames.has(row.stage))
if (selected.length !== stageNames.size || [...stageNames].some(stage => !selected.some(row => row.stage === stage))) throw new Error('selected_source_closure_missing_or_duplicate')
const noticeIds = new Map()
for (const notice of manifest.notices) {
  const id = `notice-${createHash('sha256').update(notice.file).digest('hex').slice(0, 20)}`
  noticeIds.set(notice.file, id)
  blobs.push({ id, role: 'license-evidence', file: sourcePrefix + notice.file, bytes: notice.bytes, sha256: notice.sha256,
    origin: `https://github.com/pascalghanimi/duskcut-ffmpeg/releases/tag/sources-${buildId}` })
}
const requireFile = id => { const row = blobs.find(b => b.id === id); if (!row) throw new Error(`missing_manifest_entry:${id}`); return row }
const mingwNotice = manifest.notices.find(n => n.component === '10-mingw' && /\/COPYING$/.test(n.file))
if (!mingwNotice) throw new Error('missing_mingw_license_notice')
const review = blobs.find(row => row.role === 'review-evidence')
if (!review) throw new Error('missing_runtime_review_evidence')
const runtime = (name, noticeId) => ({ name, version: manifest.toolchain.gccVersion,
  license: 'GPL-3.0-or-later WITH GCC-exception-3.1', noticeBlobId: requireFile(noticeId).id,
  exclusion: { basis: 'gcc-runtime-library-exception', evidenceBlobId: review.id,
    rationale: 'Covered runtime source headers grant GCC Runtime Library Exception 3.1. The recorded build uses the ordinary GCC C/C++ toolchain without a proprietary intermediate-representation plugin; exact exception text and component evidence are retained.' } })
const lock = {
  schemaVersion: 2, target: 'win64', variant: 'gpl', addin: '9.0', buildId,
  profile: { id: profile.id, sha256: await hashFile(profileFile) },
  recipe: { blobId: requireFile('btbn-build-recipe').id, revision: manifest.recipeRevision },
  ffmpeg: { blobId: requireFile('ffmpeg').id, revision: manifest.ffmpegRevision, sourceDateEpoch: 1790467200 },
  sourceCache: { members: selected.map(row => ({ blobId: row.id, cacheName: basename(row.file) })).sort((a, b) => a.cacheName.localeCompare(b.cacheName)) },
  oneVplPatch: { blobId: requireFile('onevpl-patch').id },
  freetypeDlg: { blobId: requireFile('freetype-dlg').id, revision: supplement.freetypeDlg.revision,
    parentRevision: supplement.freetypeDlg.parentRevision, noticeBlobId: requireFile('freetype-dlg-license').id },
  toolchain: { image: manifest.toolchain.image },
  runtimeComponents: [
    { name: 'MinGW-w64 CRT and winpthreads', version: '57b595039040eaa15bece85b7cc71d952281b269', license: 'MinGW-w64 licenses retained in corresponding source', noticeBlobId: noticeIds.get(mingwNotice.file), sourceBlobId: requireFile('10-mingw').id },
    runtime('GCC libgcc', 'gcc-libgcc2-c'), runtime('GCC libgomp', 'gcc-libgomp-h'), runtime('GCC libstdc++', 'gcc-new-op-cc'),
  ],
  blobs,
  sourceManifest: { file: basename(manifestFile), sha256: manifestSha256 },
  supplementalSourceManifest: { file: 'sources/supplemental-manifest.json', sha256: supplementSha256 },
  sourceDateEpochNote: 'Fixed release build timestamp, not a claim about the upstream commit author date.',
}
const target = resolve(outputRoot, 'build-lock.json')
await writeFile(target, JSON.stringify(lock, null, 2) + '\n', { flag: 'wx' })
const checked = await validateLock(target)
console.log(JSON.stringify({ lockFile: target, lockSha256: checked.lockSha256, sourceCount: selected.length, blobCount: blobs.length }))
