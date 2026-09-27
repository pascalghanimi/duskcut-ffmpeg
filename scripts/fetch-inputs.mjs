/** Download only the public, immutable source assets declared by this repository. */
import { createHash } from 'node:crypto'
import { createReadStream, createWriteStream, existsSync, readFileSync } from 'node:fs'
import { mkdir, rename, rm, stat } from 'node:fs/promises'
import { basename, resolve } from 'node:path'
import { Readable, Transform } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { pathToFileURL } from 'node:url'

export function validateAssets(manifest) {
  if (manifest.schemaVersion !== 1 || manifest.extractTo !== '.' || !/^sources-[a-z0-9.-]+$/.test(manifest.releaseTag || '')) throw new Error('Unsupported source asset manifest')
  if (!Array.isArray(manifest.assets) || !manifest.assets.length) throw new Error('No source assets')
  const names = new Set()
  for (const asset of manifest.assets) {
    if (!/^[a-zA-Z0-9_.-]+\.tar(?:\.gz|\.xz)?$/.test(asset.file || '') || basename(asset.file) !== asset.file || names.has(asset.file)) throw new Error('Invalid or duplicate source archive name')
    names.add(asset.file)
    if (!Number.isSafeInteger(asset.bytes) || asset.bytes < 1 || asset.bytes >= 2 ** 31 || !/^[a-f0-9]{64}$/.test(asset.sha256 || '')) throw new Error('Source asset not size/hash pinned')
    const expected = `https://github.com/pascalghanimi/duskcut-ffmpeg/releases/download/${manifest.releaseTag}/${asset.file}`
    if (asset.url !== expected) throw new Error('Source asset URL must match the fixed public release')
  }
  return manifest.assets
}

async function digest(file) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(file)) hash.update(chunk)
  return hash.digest('hex')
}

export async function fetchInputs(manifestPath, directory) {
  const assets = validateAssets(JSON.parse(readFileSync(manifestPath, 'utf8')))
  const destination = resolve(directory)
  await mkdir(destination, { recursive: true })
  for (const asset of assets) {
    const file = resolve(destination, asset.file)
    if (existsSync(file)) {
      if ((await stat(file)).size !== asset.bytes || await digest(file) !== asset.sha256) throw new Error(`Existing input differs: ${asset.file}`)
      console.log(`Verified existing ${asset.file}`)
      continue
    }
    const temporary = `${file}.partial`
    if (existsSync(temporary)) throw new Error(`Incomplete download exists: ${asset.file}.partial`)
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 20 * 60 * 1000)
    let ownsPartial = false
    try {
      const response = await fetch(asset.url, { signal: controller.signal })
      if (!response.ok || !response.body) throw new Error(`Public source download failed: HTTP ${response.status}`)
      const final = new URL(response.url)
      if (final.protocol !== 'https:' || !['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'].includes(final.hostname)) throw new Error('Unexpected source download redirect')
      const hash = createHash('sha256')
      let bytes = 0
      const writer = createWriteStream(temporary, { flags: 'wx' })
      writer.once('open', () => { ownsPartial = true })
      await pipeline(Readable.fromWeb(response.body), new Transform({
        transform(chunk, _encoding, done) {
          bytes += chunk.length
          if (bytes > asset.bytes) return done(new Error('Source download exceeds pinned size'))
          hash.update(chunk)
          done(null, chunk)
        },
      }), writer)
      if (bytes !== asset.bytes || hash.digest('hex') !== asset.sha256) throw new Error(`Source integrity mismatch: ${asset.file}`)
      await rename(temporary, file)
      console.log(`Downloaded and verified ${asset.file} (${bytes} bytes)`)
    } catch (error) {
      // Only this exact, new partial download is removed; existing inputs stay intact.
      if (ownsPartial) await rm(temporary, { force: true })
      throw error
    } finally {
      clearTimeout(timeout)
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv.length !== 4) throw new Error('Usage: node scripts/fetch-inputs.mjs release-assets.json downloads')
  await fetchInputs(process.argv[2], process.argv[3])
}
