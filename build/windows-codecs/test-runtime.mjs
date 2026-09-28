/** Synthetic fixtures only. Compare installed Windows codecs with a reference decoder. */
import { spawnSync } from 'node:child_process'
import { mkdirSync, existsSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'

const [reference, candidate, directory] = process.argv.slice(2)
if (!reference || !candidate || !directory) throw new Error('Usage: node test-runtime.mjs reference.exe candidate.exe new-output-dir')
const root = resolve(directory)
mkdirSync(root, { recursive: true })
const invoke = (binary, args) => {
  const result = spawnSync(binary, ['-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror', ...args], {
    encoding: null, windowsHide: true, timeout: 30000, maxBuffer: 64 * 1024 * 1024,
  })
  return { ok: result.status === 0, status: result.status, error: result.error?.message,
    stderr: result.stderr?.toString().slice(-6000), bytes: result.stdout || Buffer.alloc(0) }
}
function wavePayload(bytes) {
  if (bytes.length < 12 || bytes.toString('ascii', 0, 4) !== 'RIFF') return bytes
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const length = bytes.readUInt32LE(offset + 4)
    if (bytes.toString('ascii', offset, offset + 4) === 'data') return bytes.subarray(offset + 8, offset + 8 + length)
    offset += 8 + length + (length & 1)
  }
  return Buffer.alloc(0)
}
const fixtures = [
  { name: 'h264-420', video: true, codec: 'h264_mf', encode: ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-bf', '3'] },
  { name: 'h264-422-10bit', video: true, codec: 'h264_mf', unsupported: true, encode: ['-c:v', 'libx264', '-pix_fmt', 'yuv422p10le'] },
  { name: 'hevc-main', video: true, codec: 'hevc_mf', encode: ['-c:v', 'libx265', '-pix_fmt', 'yuv420p', '-x265-params', 'log-level=error:pools=2'] },
  { name: 'hevc-main10', video: true, codec: 'hevc_mf', encode: ['-c:v', 'libx265', '-pix_fmt', 'yuv420p10le', '-x265-params', 'log-level=error:pools=2'] },
  { name: 'aac-stereo-48000', rate: 48000, channels: 2 },
  { name: 'aac-mono-44100', rate: 44100, channels: 1 },
  { name: 'aac-surround-48000', rate: 48000, channels: 6 },
  { name: 'aac-adts-48000', rate: 48000, channels: 2, adts: true },
  { name: 'aac-latm-48000', rate: 48000, channels: 2, latm: true, codec: 'aac_latm_mf' },
]
const results = []
for (const f of fixtures) {
  if (process.argv[5] && !process.argv[5].split(',').includes(f.name)) continue
  const path = resolve(root, f.name + (f.adts ? '.aac' : f.latm ? '.ts' : '.mp4'))
  if (!existsSync(path)) {
    const args = f.video
      ? ['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30', '-t', '2.2', ...f.encode, path]
      : ['-f', 'lavfi', '-i', `aevalsrc=0.25*sin(2*PI*(440+200*t)*t):s=${f.rate}:d=2.2`, '-ac', String(f.channels), '-c:a', 'aac', '-b:a', f.channels > 2 ? '384k' : '160k', ...(f.latm ? ['-mpegts_flags', '+latm'] : []), path]
    const made = invoke(reference, args)
    if (!made.ok) throw new Error(`Fixture ${f.name}: ${made.stderr}`)
  }
  for (const seek of [0, 0.733]) {
    // Standalone AAC transport seeks need decoder history. Match the editor:
    // decode from the initial configuration and trim decoded PCM. MP4/video
    // remain on the fast, indexed input-seek path.
    const decodedTrim = seek && (f.adts || f.latm)
    const options = [...(seek && !decodedTrim ? ['-ss', String(seek)] : []), '-i', path,
      ...(decodedTrim ? ['-ss', String(seek)] : [])]
    const out = f.video ? ['-an', '-pix_fmt', f.name.includes('10') ? 'yuv420p10le' : 'yuv420p', '-f', 'rawvideo', '-'] : ['-vn', '-c:a', 'pcm_f32le', '-f', 'wav', '-']
    const native = invoke(reference, [...options, ...out])
    const system = invoke(candidate, ['-c:' + (f.video ? 'v' : 'a'), f.codec || 'aac_mf', ...options, ...out])
    if (!f.video) { native.bytes = wavePayload(native.bytes); system.bytes = wavePayload(system.bytes) }
    const row = { fixture: f.name, seek, seekMode: decodedTrim ? 'decoded-trim' : 'indexed-input', referenceOk: native.ok, ok: system.ok, referenceBytes: native.bytes.length,
      systemBytes: system.bytes.length, error: system.error, stderr: system.stderr,
      identical: native.bytes.equals(system.bytes), sha256: createHash('sha256').update(system.bytes).digest('hex') }
    if (!f.video && native.ok && system.ok && native.bytes.length && system.bytes.length) {
      let square = 0, peak = 0
      const n = Math.min(native.bytes.length, system.bytes.length) / 4
      for (let i = 0; i < n; i++) {
        const diff = native.bytes.readFloatLE(i * 4) - system.bytes.readFloatLE(i * 4)
        square += diff * diff
        peak = Math.max(peak, Math.abs(diff))
      }
      row.audioRmse = Math.sqrt(square / n)
      row.audioMaxError = peak
    }
    if (f.video && native.ok && system.ok && native.bytes.length === system.bytes.length) {
      const step = f.name.includes('10') ? 2 : 1
      let maxError = 0, count = 0
      for (let i = 0; i < native.bytes.length; i += step) {
        const diff = Math.abs((step === 2 ? native.bytes.readUInt16LE(i) : native.bytes[i]) -
          (step === 2 ? system.bytes.readUInt16LE(i) : system.bytes[i]))
        maxError = Math.max(maxError, diff); if (diff) count++
      }
      row.videoMaxError = maxError; row.differingSamples = count
    }
    row.passed = native.ok && (f.unsupported
      ? !system.ok && !system.error && system.bytes.length === 0 && /DUSKCUT_MF_PROFILE_UNSUPPORTED/.test(system.stderr)
      : system.ok && native.bytes.length === system.bytes.length && (f.video ? row.identical : row.audioRmse < 0.0001))
    results.push(row)
    console.log(JSON.stringify(row))
  }
  if (f.latm) {
    const seeked = invoke(candidate, ['-c:a', 'aac_latm_mf', '-ss', '0.733', '-i', path,
      '-vn', '-c:a', 'pcm_f32le', '-f', 'wav', '-'])
    const row = { fixture: f.name, seek: 0.733, seekMode: 'unsupported-raw-input',
      passed: !seeked.ok && !seeked.error && /DUSKCUT_MF_LATM_SEEK_UNSUPPORTED/.test(seeked.stderr),
      error: seeked.error, stderr: seeked.stderr }
    results.push(row)
    console.log(JSON.stringify(row))
  }
}
writeFileSync(resolve(root, 'results.json'), JSON.stringify({ candidate, reference, results }, null, 2) + '\n')
if (results.some((row) => !row.passed)) process.exitCode = 1
