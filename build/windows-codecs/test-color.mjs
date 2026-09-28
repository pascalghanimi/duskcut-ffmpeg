/** Independent synthetic video metadata checks. No proprietary source media is used. */
import { spawnSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve, join, dirname } from 'node:path'
const [reference, candidate, directory] = process.argv.slice(2)
if (!reference || !candidate || !directory) throw new Error('Usage: node test-color.mjs reference.exe candidate.exe output-directory')
const root = resolve(directory)
mkdirSync(root, { recursive: true })
const call = (exe, args, input) => {
  const p = spawnSync(exe, ['-hide_banner', ...(/ffprobe\.exe$/i.test(exe) ? [] : ['-nostdin']), ...args], { input, timeout: 45000, windowsHide: true, maxBuffer: 32 * 1024 * 1024 })
  return { ok: p.status === 0, status: p.status, error: p.error?.message, out: p.stdout ?? Buffer.alloc(0), stderr: p.stderr?.toString() ?? '' }
}
const checked = (p) => { if (!p.ok) throw new Error(p.error || p.stderr); return p }
const metadata = (p) => {
  const lines = p.stderr.split(/\r?\n/)
  const color = lines.find((x) => /color_range:/.test(x)) ?? ''
  const frame = lines.find((x) => /n:\s+0.*sar:/.test(x)) ?? ''
  return { ok: p.ok, color: color.slice(color.indexOf('color_range:')), sar: /sar:([^ ]+)/.exec(frame)?.[1],
    hdr: lines.filter((x) => /Mastering display metadata|Content light level|red_x|MaxCLL/.test(x)).map((x) => x.slice(x.indexOf(']') + 1).trim()),
    error: p.ok ? undefined : p.error || p.stderr.slice(-2500) }
}
const cases = [
  { name: 'h264-709-limited', codec: 'h264', range: 'tv', pri: 'bt709', trc: 'bt709', matrix: 'bt709' },
  { name: 'h264-srgb-full-sar', codec: 'h264', range: 'pc', pri: 'bt709', trc: 'iec61966-2-1', matrix: 'bt709', sar: '4/3' },
  { name: 'hevc-709-limited', codec: 'hevc', range: 'tv', pri: 'bt709', trc: 'bt709', matrix: 'bt709' },
  { name: 'hevc-srgb-full-sar', codec: 'hevc', range: 'pc', pri: 'bt709', trc: 'iec61966-2-1', matrix: 'bt709', sar: '4/3' },
  { name: 'hevc-pq2020-full-sar-hdr', codec: 'hevc', range: 'pc', pri: 'bt2020', trc: 'smpte2084', matrix: 'bt2020nc', ten: true, sar: '4/3', hdr: true },
  { name: 'hevc-hlg2020-limited', codec: 'hevc', range: 'tv', pri: 'bt2020', trc: 'arib-std-b67', matrix: 'bt2020nc', ten: true },
]
const decode = []
for (const c of cases) {
  const file = join(root, c.name + '.ts')
  const encode = c.codec === 'h264' ? ['-c:v', 'libx264'] : ['-c:v', 'libx265', '-x265-params',
    'log-level=error:pools=2' + (c.hdr ? ':master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50):max-cll=1000,400' : '')]
  checked(call(reference, ['-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30', '-frames:v', '6',
    '-vf', `setsar=${c.sar ?? '1/1'},setparams=range=${c.range}:color_primaries=${c.pri}:color_trc=${c.trc}:colorspace=${c.matrix}`, ...encode, '-pix_fmt', c.ten ? 'yuv420p10le' : 'yuv420p',
    '-color_range', c.range, '-color_primaries', c.pri, '-color_trc', c.trc, '-colorspace', c.matrix, '-f', 'mpegts', file]))
  const args = ['-i', file, '-vf', 'showinfo', '-frames:v', '1', '-c:v', 'rawvideo', '-f', 'null', '-']
  const native = metadata(call(reference, args))
  const windows = metadata(call(candidate, ['-c:v', c.codec + '_mf', ...args]))
  const row = { name: c.name, native, windows,
    passed: native.ok && windows.ok && native.color === windows.color && native.sar === windows.sar,
    staticHdrCoverage: c.hdr ? {
      mastering: windows.hdr.some((x) => /Mastering display metadata/.test(x)),
      contentLight: windows.hdr.some((x) => /Content light level/.test(x))
    } : undefined }
  decode.push(row)
  console.log(JSON.stringify(row))
}

// The prototype has no lavfi/setparams. Supply raw RGBA with the same explicit
// frame color tags and swscale conversion used by DuskCut's export pipeline.
const rgba = checked(call(reference, ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30', '-frames:v', '3', '-pix_fmt', 'rgba', '-f', 'rawvideo', '-'])).out
const encode = []
for (const codec of ['h264', 'hevc']) for (const hw of [false, true]) for (const policy of [
  { name: 'bt709-limited', range: 'tv', transfer: 'bt709' },
  { name: 'srgb-full', range: 'pc', transfer: 'iec61966-2-1' },
]) {
  const file = join(root, `export-${codec}-${hw ? 'hw' : 'sw'}-${policy.name}.mp4`)
  const tags = ['-color_range', policy.range, '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', policy.transfer]
  const p = call(candidate, ['-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', '1280x720', '-framerate', '30',
    ...tags, '-i', 'pipe:0', '-an', '-vf', `scale=out_color_matrix=bt709:out_range=${policy.range}:flags=accurate_rnd+full_chroma_int`,
    ...tags, '-c:v', codec + '_mf', '-hw_encoding', hw ? '1' : '0', '-pix_fmt', 'nv12', '-scenario', 'archive', '-bf', '0',
    '-rate_control', 'quality', '-quality', '80', '-profile:v', codec === 'h264' ? '100' : '1', ...(codec === 'hevc' ? ['-tag:v', 'hvc1'] : []), file], rgba)
  const probe = p.ok ? call(join(dirname(reference), 'ffprobe.exe'), ['-v', 'error', '-show_entries', 'stream=codec_name,width,height,pix_fmt,color_range,color_space,color_primaries,color_transfer', '-of', 'json', file]) : null
  const stream = probe?.ok ? JSON.parse(probe.out.toString()).streams?.[0] : null
  const row = { codec, hardware: hw, policy: policy.name, available: p.ok, stream, error: p.ok ? undefined : p.error || p.stderr.slice(-2000),
    passed: p.ok && stream?.codec_name === codec && stream?.color_range === policy.range && stream?.color_space === 'bt709' && stream?.color_primaries === 'bt709' && stream?.color_transfer === policy.transfer }
  encode.push(row)
  console.log(JSON.stringify(row))
}
const passed = decode.every((r) => r.passed) && encode.filter((r) => r.available).every((r) => r.passed) &&
  ['h264', 'hevc'].every((codec) => encode.some((r) => r.codec === codec && r.passed))
writeFileSync(join(root, 'color-results.json'), JSON.stringify({ reference, candidate, passed, decode, encode }, null, 2) + '\n')
if (!passed) process.exitCode = 1
