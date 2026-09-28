/** Synthetic unsupported-media fixtures. Reference encoder is QA-only, never packaged. */
import { mkdirSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

const [reference, output] = process.argv.slice(2)
if (!reference || !output) throw new Error('Usage: create-negative-fixtures.mjs old-reference-ffmpeg.exe output-dir')
mkdirSync(resolve(output), { recursive: true })
for (const [name, codec] of [
  ['h264-422.mp4', ['-c:v', 'libx264', '-pix_fmt', 'yuv422p10le']],
  ['prores.mov', ['-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuva444p10le']],
  ['wmv.wmv', ['-c:v', 'wmv2', '-pix_fmt', 'yuv420p']],
]) {
  const path = resolve(output, name)
  if (existsSync(path)) continue
  execFileSync(resolve(reference), ['-hide_banner', '-v', 'error', '-nostdin', '-n',
    '-f', 'lavfi', '-i', 'testsrc2=size=128x72:rate=10', '-t', '0.5', ...codec, path],
  { windowsHide: true, timeout: 30000, stdio: 'pipe' })
  console.log(name)
}
