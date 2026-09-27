/** Strict input verification for an optional controlled FFmpeg build; not a legal approval. */
import { createHash } from 'node:crypto'
import { createReadStream, lstatSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const sha = /^[a-f0-9]{64}$/
const commit = /^[a-f0-9]{40}$/
const roles = new Set(['recipe', 'source-cache', 'ffmpeg-source', 'license-evidence', 'source', 'review-evidence', 'patch'])
export function inside(root, file) {
  if (typeof file !== 'string' || !file || file.includes('\\') || isAbsolute(file)) throw new Error('invalid_relative_path')
  const path = resolve(root, file)
  const rel = relative(root, path)
  if (!rel || isAbsolute(rel) || rel === '..' || rel.startsWith(`..${sep}`)) throw new Error('path_outside_root')
  return path
}
export async function hashFile(file) {
  const hash = createHash('sha256')
  for await (const bytes of createReadStream(file)) hash.update(bytes)
  return hash.digest('hex')
}
function actualInside(root, file) {
  const info = lstatSync(file)
  const rel = relative(realpathSync(root), realpathSync(file))
  if (!info.isFile() || info.isSymbolicLink() || isAbsolute(rel) || rel === '..' || rel.startsWith(`..${sep}`)) throw new Error('unsafe_input_file')
}
function requiredBlob(blobs, id, role) {
  const blob = blobs.get(id)
  if (!blob || (role && blob.role !== role)) throw new Error(`missing_${role || 'evidence'}_blob:${id}`)
  return blob
}
export async function validateLock(lockFile) {
  const root = dirname(resolve(lockFile))
  const lock = JSON.parse(readFileSync(lockFile, 'utf8'))
  if (![1, 2].includes(lock.schemaVersion) || lock.target !== 'win64' || lock.variant !== 'gpl' || lock.addin !== '9.0') throw new Error('unsupported_build_profile')
  if (!commit.test(lock.recipe?.revision) || !commit.test(lock.ffmpeg?.revision)) throw new Error('unresolved_source_revision')
  if (!Number.isSafeInteger(lock.ffmpeg?.sourceDateEpoch) || lock.ffmpeg.sourceDateEpoch < 1) throw new Error('missing_source_date_epoch')
  if (typeof lock.toolchain?.image !== 'string' || !/^[a-z0-9./_-]+@sha256:[a-f0-9]{64}$/.test(lock.toolchain.image)) throw new Error('toolchain_image_not_digest_pinned')
  if (lock.schemaVersion === 1 && !sha.test(lock.downloadCache?.innerSha256)) throw new Error('inner_cache_not_content_pinned')
  if (lock.schemaVersion === 2 && (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(lock.buildId || '') || !sha.test(lock.profile?.sha256) || lock.profile?.id !== 'duskcut-win64-gpl-no-dvd-v1')) throw new Error('invalid_build_identity_or_profile')
  if (!Array.isArray(lock.blobs) || lock.blobs.length < 4) throw new Error('missing_inputs')
  const blobs = new Map()
  const seenPaths = new Set()
  for (const blob of lock.blobs) {
    if (!/^[a-z0-9][a-z0-9_-]*$/.test(blob.id || '') || blobs.has(blob.id)) throw new Error('invalid_or_duplicate_blob_id')
    if (!roles.has(blob.role) || !sha.test(blob.sha256) || !Number.isSafeInteger(blob.bytes) || blob.bytes < 1) throw new Error(`unpinned_blob:${blob.id}`)
    const path = inside(root, blob.file)
    if (seenPaths.has(path.toLowerCase())) throw new Error('duplicate_blob_path')
    seenPaths.add(path.toLowerCase())
    actualInside(root, path)
    if (statSync(path).size !== blob.bytes || await hashFile(path) !== blob.sha256) throw new Error(`blob_integrity_mismatch:${blob.id}`)
    if (typeof blob.origin !== 'string' || !blob.origin.startsWith('https://')) throw new Error(`missing_public_origin:${blob.id}`)
    let origin
    try { origin = new URL(blob.origin) } catch { throw new Error(`invalid_public_origin:${blob.id}`) }
    if (origin.username || origin.password || origin.search || origin.hash) throw new Error(`nonpublic_or_credential_origin:${blob.id}`)
    blobs.set(blob.id, { ...blob, path })
  }
  requiredBlob(blobs, lock.recipe.blobId, 'recipe')
  requiredBlob(blobs, lock.ffmpeg.blobId, 'ffmpeg-source')
  if (lock.schemaVersion === 1) requiredBlob(blobs, lock.downloadCache.blobId, 'source-cache')
  else {
    requiredBlob(blobs, lock.oneVplPatch?.blobId, 'patch')
    requiredBlob(blobs, lock.freetypeDlg?.blobId, 'source')
    requiredBlob(blobs, lock.freetypeDlg?.noticeBlobId, 'license-evidence')
    if (lock.freetypeDlg.revision !== '395ccad2c1e0daae535c4d20bb0a3f2424648e17' ||
        lock.freetypeDlg.parentRevision !== 'd333439633039de426f943f28a2926c7f97b5ae5') throw new Error('unpinned_freetype_submodule')
    if (!Array.isArray(lock.sourceCache?.members) || !lock.sourceCache.members.length) throw new Error('missing_selected_sources')
    const caches = new Set()
    for (const member of lock.sourceCache.members) {
      if (!/^\d{2}-[a-z0-9-]+_[a-f0-9]{64}\.tar\.xz$/.test(member.cacheName || '') || /dvd|css|rav1e|rust|rsvg|jxl|lcevc|vulkan|placebo|whisper/.test(member.cacheName) || caches.has(member.cacheName)) throw new Error('invalid_duplicate_or_forbidden_cache_member')
      caches.add(member.cacheName)
      requiredBlob(blobs, member.blobId, 'source-cache')
    }
  }
  // This is a component-specific review, not a demand for all compiler sources.
  // A GPL System Library/general-tool exclusion or GCC Runtime Library Exception
  // can be recorded instead, but not as an unevidenced boolean "approved".
  if (!Array.isArray(lock.runtimeComponents) || !lock.runtimeComponents.length) throw new Error('runtime_review_missing')
  const runtimeNames = new Set()
  for (const runtime of lock.runtimeComponents) {
    if (typeof runtime.name !== 'string' || !runtime.name || runtimeNames.has(runtime.name)) throw new Error('runtime_identity_missing_or_duplicate')
    runtimeNames.add(runtime.name)
    if (typeof runtime.version !== 'string' || !runtime.version || typeof runtime.license !== 'string' || !runtime.license) throw new Error('runtime_license_or_version_missing')
    requiredBlob(blobs, runtime.noticeBlobId, 'license-evidence')
    if (runtime.sourceBlobId) {
      const source = requiredBlob(blobs, runtime.sourceBlobId)
      if (!['source', 'source-cache'].includes(source.role)) throw new Error('runtime_source_blob_wrong_role')
    }
    else {
      if (!['gcc-runtime-library-exception', 'gpl-system-library', 'gpl-general-build-tool'].includes(runtime.exclusion?.basis)) throw new Error('runtime_source_or_specific_exclusion_missing')
      requiredBlob(blobs, runtime.exclusion.evidenceBlobId, 'review-evidence')
      if (typeof runtime.exclusion.rationale !== 'string' || runtime.exclusion.rationale.trim().length < 30) throw new Error('runtime_exclusion_rationale_missing')
    }
  }
  return { lock, root, blobs, lockSha256: await hashFile(lockFile), status: 'inputs-integrity-verified-not-source-or-legal-approval' }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 3) throw new Error('Usage: node validate-lock.mjs <build-lock.json>')
  const result = await validateLock(process.argv[2])
  console.log(JSON.stringify({ status: result.status, lockSha256: result.lockSha256, inputCount: result.blobs.size }))
}
