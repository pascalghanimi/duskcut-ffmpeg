/** Verify that unsafe format changes fail explicitly instead of losing quality.
 * First run test-color.mjs to create its synthetic fixtures in the same directory.
 */
import { spawnSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { resolve, join } from 'node:path'
const [reference, candidate, directory] = process.argv.slice(2)
if (!reference || !candidate || !directory) throw new Error('Usage: node test-dynamic-video.mjs reference.exe candidate.exe color-fixture-directory')
const root = resolve(directory)
const call = (exe, args) => {
  const p = spawnSync(exe, ['-hide_banner', '-nostdin', ...args], { timeout: 45000, windowsHide: true, maxBuffer: 32 * 1024 * 1024 })
  return { ok: p.status === 0, status: p.status, error: p.error?.message, out: p.stdout ?? Buffer.alloc(0), stderr: p.stderr?.toString() ?? '' }
}
const cases = [
  { name: 'hevc-depth-8-10-8', codec: 'hevc', parts: ['hevc-709-limited', 'hevc-pq2020-full-sar-hdr', 'hevc-709-limited'] },
  { name: 'hevc-color-sar', codec: 'hevc', parts: ['hevc-709-limited', 'hevc-srgb-full-sar'] },
  { name: 'h264-color-sar', codec: 'h264', parts: ['h264-709-limited', 'h264-srgb-full-sar'] },
]
const results = []
for (const c of cases) {
  const parts = c.parts.map((name) => {
    const p = call(reference, ['-v', 'error', '-i', join(root, name + '.ts'), '-map', '0:v:0', '-c:v', 'copy', '-f', c.codec, '-'])
    if (!p.ok) throw new Error(p.error || p.stderr)
    return p.out
  })
  const file = join(root, 'dynamic-' + c.name + '.' + c.codec)
  writeFileSync(file, Buffer.concat(parts))
  const args = ['-xerror', '-i', file, '-vf', 'showinfo', '-c:v', 'rawvideo', '-f', 'null', '-']
  const native = call(reference, args)
  const windows = call(candidate, ['-c:v', c.codec + '_mf', ...args])
  const guard = windows.stderr.includes('DUSKCUT_MF_DYNAMIC_FORMAT_UNSUPPORTED:')
  const row = { name: c.name, nativeStatus: native.status, windowsStatus: windows.status, guard,
    passed: native.ok && !windows.ok && guard && !windows.error,
    windowsError: windows.error || windows.stderr.slice(-3500) }
  results.push(row)
  writeFileSync(join(root, 'dynamic-' + c.name + '-native.log'), native.stderr)
  writeFileSync(join(root, 'dynamic-' + c.name + '-windows.log'), windows.stderr)
  console.log(JSON.stringify(row))
}
const passed = results.every((r) => r.passed)
writeFileSync(join(root, 'dynamic-results.json'), JSON.stringify({ reference, candidate, passed, results }, null, 2) + '\n')
if (!passed) process.exitCode = 1
