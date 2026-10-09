/** Synthetic-only seek regression: a decode that starts on an H.264 recovery point (a non-IDR
 * keyframe, typical for YouTube downloads and x264 --open-gop), mid-GOP in a transport stream, or
 * on an unmarked random access point must produce exactly the frames a native reference decoder
 * produces: same time base, dimensions, count, and dts/pts/duration/size/md5 per frame.
 *
 * Measured 29.09.2026 with 9.0.2-duskcut.2:
 * - open-GOP H.264 MP4: 0 frames after every seek. h264_mp4toannexb prepended SPS/PPS only to IDR
 *   slices, so Windows got no parameter sets until the next IDR;
 * - with a later IDR in reach (the YouTube pattern): DUSKCUT_MF_DYNAMIC_FORMAT_UNSUPPORTED, because
 *   the first header met was compared with the probe's NV12 OUTPUT format. A real 1920x1080
 *   YouTube MP4 failed on 41 of 113 seek points, every one on a recovery point;
 * - H.264/HEVC .ts: seeks failed with the same false marker; once decodable, the first picture
 *   carried a timestamp one frame early (Windows kept the time of a dropped tail picture), so an
 *   accurate seek trimmed it.
 * Review cases added: SEI-less TS (keyframes unflagged; decoding from the start must keep working),
 * an MP4 with two sample descriptions (parameter sets must follow the description, never the one
 * read at open), and a -stream_loop pass that reaches the decoder flush path.
 *
 * Usage: node test-recovery-seek.mjs reference.exe candidate.exe NEW-output-dir [released.exe]
 * With released.exe (the previous build), every row marked `defect` must FAIL on it and every
 * control row must pass, so the suite proves it detects the defects it guards against.
 * The reference is a QA tool, never an application fallback or a distributed component.
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { compareVideo, parseFrameMd5 } from './test-cut-timing.mjs'

const [reference, candidate, output, released] = process.argv.slice(2)
if (!reference || !candidate || !output) {
  throw new Error('Usage: node test-recovery-seek.mjs reference.exe candidate.exe NEW-output-dir [released.exe]')
}
const root = resolve(output)
if (existsSync(root)) throw new Error('Output directory must be new: ' + root)
mkdirSync(root, { recursive: true })

const call = (exe, args) => {
  const p = spawnSync(exe, ['-hide_banner', '-nostdin', ...args], { timeout: 90000, windowsHide: true, maxBuffer: 64 * 1024 * 1024 })
  return { ok: p.status === 0, status: p.status, error: p.error?.message, out: p.stdout?.toString() ?? '', stderr: p.stderr?.toString() ?? '' }
}
const make = (args) => {
  const p = call(reference, ['-v', 'error', '-y', ...args])
  if (!p.ok) throw new Error('Fixture: ' + (p.error || p.stderr))
}
const trace = (file) => call(reference, ['-v', 'info', '-i', file, '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'trace_headers', '-f', 'null', '-']).stderr
const count = (text, re) => (text.match(re) ?? []).length

const SOURCE = ['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-t', '10']
const COLOR = ['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv']
const GOP = 'keyint=25:min-keyint=25:scenecut=0:bframes=3'
const X264 = (extra) => ['-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-x264-params', GOP + extra]
const X265 = (extra) => ['-c:v', 'libx265', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-x265-params', GOP + ':log-level=error' + extra]
const file = (name) => join(root, name)
const SEEKS = [0, 1, 1.02, 1.5, 2.37, 3, 4.44, 4.5, 5.96, 7.1]

// Fixtures. `check` guards the fixture itself, so an encoder default that drifts cannot make a row vacuous.
make([...SOURCE, ...X264(':open-gop=1'), ...COLOR, file('h264-opengop.mp4')])
make([...SOURCE, ...X264(':open-gop=1'), '-force_key_frames', '5', '-forced-idr', '1', ...COLOR, file('h264-opengop-idr5.mp4')])
make([...SOURCE, ...X264(':open-gop=1:repeat-headers=1'), ...COLOR, '-f', 'mpegts', file('h264-opengop-repeat.ts')])
make([...SOURCE, ...X264(':repeat-headers=1'), ...COLOR, '-f', 'mpegts', file('h264-closedgop-repeat.ts')])
make([...SOURCE, ...X265(':open-gop=1:repeat-headers=1'), ...COLOR, '-f', 'mpegts', file('hevc-opengop-repeat.ts')])
make([...SOURCE, ...X264(''), ...COLOR, file('h264-closedgop.mp4')])
make([...SOURCE, ...X265(':open-gop=1'), '-tag:v', 'hvc1', ...COLOR, file('hevc-opengop.mp4')])
make(['-i', file('h264-opengop.mp4'), '-map', '0:v:0', '-c', 'copy', '-f', 'mpegts', file('h264-opengop-remux.ts')])
make(['-i', file('h264-opengop-repeat.ts'), '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'filter_units=remove_types=6', '-f', 'mpegts', file('h264-opengop-nosei.ts')])
make([...SOURCE, ...X264(':open-gop=1:repeat-headers=1'), ...COLOR, file('h264-opengop-repeat.mp4')])
// Starts exactly on a non-IDR I-frame that carries its SPS but no recovery SEI (unflagged).
make(['-ss', '2', '-i', file('h264-opengop-repeat.mp4'), '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'filter_units=remove_types=6', '-f', 'mpegts', file('h264-opengop-nosei-cut.ts')])
make(['-ss', '2', '-i', file('h264-opengop.mp4'), '-map', '0:v:0', '-c', 'copy', file('h264-opengop-cut.mp4')])
// MP4 sync samples without a recovery SEI: the demuxer's H.264 parser does not flag them as keyframes.
make(['-i', file('h264-opengop.mp4'), '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'filter_units=remove_types=6', file('h264-opengop-nosei.mp4')])
// In-band SPS at every recovery point, PPS only in avcC (the case the IDR rule's PPS branch exists for).
make(['-i', file('h264-opengop-repeat.mp4'), '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'filter_units=remove_types=8', file('h264-opengop-nopps.mp4')])
// Intra refresh: no I pictures, recovery points with recovery_frame_cnt > 0.
make([...SOURCE, '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-x264-params', 'keyint=25:intra-refresh=1:bframes=0', ...COLOR, file('h264-intra-refresh.mp4')])
// Two sample descriptions with different PPS (CABAC, then CAVLC), joined without re-encoding.
make(['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-t', '4', ...X264(':open-gop=1:repeat-headers=1'), ...COLOR, '-f', 'mpegts', file('part-cabac.ts')])
make(['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-ss', '4', '-t', '4', ...X264(':open-gop=1:repeat-headers=1:cabac=0'), ...COLOR, '-f', 'mpegts', file('part-cavlc.ts')])
writeFileSync(file('parts.txt'), "file 'part-cabac.ts'\nfile 'part-cavlc.ts'\n")
make(['-f', 'concat', '-safe', '0', '-i', file('parts.txt'), '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'extract_extradata=remove=1', file('h264-two-descriptions.mp4')])

const checks = {
  'h264-opengop.mp4': (t) => count(t, /nal_unit_type\s+\S+ = 5$/gm) === 1 && count(t, /recovery_frame_cnt/g) >= 5,
  'h264-opengop-idr5.mp4': (t) => count(t, /nal_unit_type\s+\S+ = 5$/gm) >= 2 && count(t, /recovery_frame_cnt/g) >= 5,
  'h264-opengop-remux.ts': (t) => count(t, /recovery_frame_cnt/g) >= 5,
  'h264-opengop-nosei.ts': (t) => count(t, /recovery_frame_cnt/g) === 0 && count(t, /nal_unit_type\s+\S+ = 5$/gm) === 1,
  'h264-opengop-nosei-cut.ts': (t) => count(t, /recovery_frame_cnt/g) === 0 && count(t, /nal_unit_type\s+\S+ = 5$/gm) === 0,
  // The only PPS is the avcC copy; every recovery point repeats its SPS in band.
  'h264-opengop-nopps.mp4': (t) => count(t, /recovery_frame_cnt/g) >= 5 && count(t, /nal_unit_type\s+\S+ = 8$/gm) === 1 &&
    count(t, /nal_unit_type\s+\S+ = 7$/gm) >= 5,
  'h264-intra-refresh.mp4': (t) => count(t, /recovery_frame_cnt\s+\S+ = [1-9]/g) >= 3 && count(t, /nal_unit_type\s+\S+ = 5$/gm) === 1,
  'h264-two-descriptions.mp4': () => count(readFileSync(file('h264-two-descriptions.mp4')).toString('latin1'), /avcC/g) >= 2,
}
for (const [name, ok] of Object.entries(checks)) if (!ok(trace(file(name)))) throw new Error('Fixture does not have the intended structure: ' + name)

// `defect`: failed on 9.0.2-duskcut.2 (measured); `control`: must pass on both builds.
const ROWS = [
  ...SEEKS.map((seek) => ({ name: 'h264-opengop.mp4', codec: 'h264', seek, defect: seek > 0 })),
  ...[1.5, 4.5, 7.5].map((seek) => ({ name: 'h264-opengop-idr5.mp4', codec: 'h264', seek, defect: true })),
  ...SEEKS.map((seek) => ({ name: 'h264-opengop-repeat.ts', codec: 'h264', seek, defect: seek > 0 })),
  ...SEEKS.map((seek) => ({ name: 'h264-closedgop-repeat.ts', codec: 'h264', seek, defect: seek > 0 })),
  ...SEEKS.map((seek) => ({ name: 'hevc-opengop-repeat.ts', codec: 'hevc', seek, defect: seek > 0 })),
  ...SEEKS.map((seek) => ({ name: 'h264-opengop-remux.ts', codec: 'h264', seek, defect: seek > 0 })),
  // No recovery SEI: FFmpeg's own decoder conservatively withholds some correct pictures after such
  // a start (measured: source frames 64 and 66, bit-exact on Windows). Judged by content instead.
  ...SEEKS.map((seek) => ({ name: 'h264-opengop-nosei.ts', codec: 'h264', seek, truth: 'h264-opengop-repeat.ts' })),
  { name: 'h264-opengop-nosei-cut.ts', codec: 'h264', seek: null, truth: 'h264-opengop-repeat.mp4' },
  ...[0, 1, 3.9, 4, 5, 5.5, 6, 6.37, 7].map((seek) => ({ name: 'h264-two-descriptions.mp4', codec: 'h264', seek })),
  { name: 'h264-opengop-cut.mp4', codec: 'h264', seek: null, loop: true },
  ...SEEKS.map((seek) => ({ name: 'h264-opengop-nosei.mp4', codec: 'h264', seek, truth: 'h264-opengop.mp4' })),
  ...SEEKS.map((seek) => ({ name: 'h264-opengop-nopps.mp4', codec: 'h264', seek })),
  // Windows must never show a picture that is still being refreshed.
  // Windows itself resumes an intra-refresh stream only at a later recovery point (measured: 3 s
  // instead of 1 s), so it cannot cover what the reference shows; it must show only complete pictures.
  ...SEEKS.map((seek) => ({ name: 'h264-intra-refresh.mp4', codec: 'h264', seek, truth: 'h264-intra-refresh.mp4', window: 30, coverage: false, nonEmpty: true })),
  ...SEEKS.map((seek) => ({ name: 'h264-closedgop.mp4', codec: 'h264', seek, control: true })),
  ...SEEKS.map((seek) => ({ name: 'hevc-opengop.mp4', codec: 'hevc', seek, control: true })),
]

const decode = (exe, row, decoder) => call(exe, ['-v', 'error', '-xerror',
  ...(row.loop ? ['-stream_loop', '1'] : []), ...(row.seek === null ? [] : ['-ss', String(row.seek)]),
  ...(decoder ? ['-c:v', decoder] : []), '-i', file(row.name), '-map', '0:v:0', '-t', String(row.window ?? (row.seek === null ? 30 : 1.2)),
  '-an', '-c:v', 'rawvideo', '-pix_fmt', 'yuv420p', '-fps_mode:v', 'passthrough', '-enc_time_base:v', 'demux', '-f', 'framemd5', '-'])

const judge = (exe, row) => {
  const native = decode(reference, row, null)
  const windows = decode(exe, row, row.codec + '_mf')
  // A truth row may legitimately be unreadable for the reference (it withholds such starts).
  if (!native.ok && !row.truth) return 'reference failed: ' + (native.error || native.stderr.slice(-500))
  if (!windows.ok || windows.error) return 'Windows decode failed: ' + (windows.error || windows.stderr.slice(-500))
  if (windows.stderr.includes('DUSKCUT_MF_')) return 'Windows decode reported ' + /DUSKCUT_MF_[A-Z_]+/.exec(windows.stderr)[0]
  const a = native.ok ? parseFrameMd5(native.out) : { frames: [] }
  const b = parseFrameMd5(windows.out)
  if (row.truth) {
    // The truth is the fully recovered decode of the unstripped, uncut encode. Windows must return an
    // unbroken run of its pictures, in order, covering every picture the reference returns.
    const truth = parseFrameMd5(call(reference, ['-v', 'error', '-i', file(row.truth), '-map', '0:v:0', '-an',
      '-c:v', 'rawvideo', '-pix_fmt', 'yuv420p', '-fps_mode:v', 'passthrough', '-f', 'framemd5', '-']).out)
    const index = new Map(truth.frames.map((f, i) => [f.md5, i]))
    const at = b.frames.map((f) => index.get(f.md5))
    // Both empty is legitimate: nothing in the window was decodable yet (intra refresh).
    if (!at.length) return a.frames.length || row.nonEmpty ? 'Windows produced no frames' : ''
    const bad = at.findIndex((v, i) => v === undefined || (i > 0 && v !== at[i - 1] + 1))
    if (bad >= 0) return `picture ${bad} is ${at[bad] === undefined ? 'not a picture of the source' : 'out of sequence'}`
    const have = new Set(at)
    const missing = row.coverage === false ? null : a.frames.find((f) => !have.has(index.get(f.md5)))
    return missing ? `a picture the reference returns is missing (${b.frames.length} vs ${a.frames.length} frames)` : ''
  }
  if (!a.frames.length) return 'reference produced no frames'
  const c = compareVideo(a, b)
  return c.identical ? '' : `differs: ${c.candidateFrames}/${c.referenceFrames} frames, first mismatch ${c.firstMismatch}` +
    (c.firstMismatch !== null && b.frames[c.firstMismatch] ? ` (pts ${b.frames[c.firstMismatch].pts} vs ${a.frames[c.firstMismatch]?.pts})` : '')
}

const probe = join(dirname(candidate), 'ffprobe.exe')
const extradata = {}
for (const name of new Set(ROWS.map((r) => r.name))) {
  const p = existsSync(probe) ? call(probe, ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=extradata_size', '-of', 'csv=p=0', file(name)]) : null
  extradata[name] = p?.ok ? Number(p.out.trim()) : null
}

const results = []
for (const row of ROWS) {
  const problem = judge(candidate, row)
  const result = { fixture: row.name, seek: row.seek, loop: !!row.loop, passed: !problem, problem }
  if (released && (row.defect || row.control)) {
    result.released = judge(released, row) || 'passed'
    // A control that fails on the previous build is a broken fixture, not a regression guard.
    if (row.control && result.released !== 'passed') { result.passed = false; result.problem += ' | control fails on the released build: ' + result.released }
  }
  results.push(result)
  console.log(JSON.stringify(result))
}
// Discrimination: on the previous build most defect rows of each fixture must fail, or the fixture
// no longer reproduces what it guards against (a landing exactly on an IDR legitimately passes).
const discrimination = {}
if (released) {
  for (const name of new Set(ROWS.filter((r) => r.defect).map((r) => r.name))) {
    const rows = results.filter((r, i) => ROWS[i].defect && r.fixture === name)
    const failed = rows.filter((r) => r.released !== 'passed').length
    discrimination[name] = { failedOnReleased: failed, defectRows: rows.length, ok: failed * 2 > rows.length }
  }
}
const passed = results.every((r) => r.passed) && Object.values(discrimination).every((d) => d.ok)
writeFileSync(join(root, 'recovery-seek-results.json'), JSON.stringify({ reference, candidate, released: released ?? null, extradata, discrimination, passed, results }, null, 2) + '\n')
if (!passed) process.exitCode = 1
