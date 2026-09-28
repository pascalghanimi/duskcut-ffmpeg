/** Synthetic-only cut regression: native reference vs Windows API video/AAC decoding.
 * The reference is a QA tool, never an application fallback or a distributed component.
 * Every invocation needs a NEW output directory so evidence cannot overwrite an earlier run.
 */
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const RATE = 48000
const CHANNELS = 2
const AUDIO_TOLERANCE = 0.0001
// Independently implemented AAC decoders are not bit-exact. Existing AAC mono/stereo checks
// measured a small first-block transient, not a lost onset (~0.00057 peak in this signal).
// Allow at most -74 dBFS RMS for the first 20 ms, but keep length/lag strictly equal;
// the remainder stays below -80 dBFS RMS and any >0.002 sample error is a failure.
const AUDIO_ONSET_TOLERANCE = 0.0002
const AUDIO_PEAK_TOLERANCE = 0.002
const CUTS = [
  [0, 0.061], [0.037, 0.419], [0.733, 1.011], [2.983, 0.089],
  [3.019, 0.517], [4.999, 0.271], [6.017, 1.293], [8.771, 0.123],
  [9.977, 0.607], [12.811, 1.071], [13.731, 0.219],
].map(([start, duration]) => ({ start, duration }))

export function parseFrameMd5(text) {
  const timeBase = /^#tb 0:\s*(\d+\/\d+)\s*$/m.exec(text)?.[1]
  const size = /^#dimensions 0:\s*(\d+)x(\d+)\s*$/m.exec(text)
  const frames = text.split(/\r?\n/).filter(line => line.trim() && !line.startsWith('#')).map(line => {
    const fields = line.split(',').map(x => x.trim())
    if (fields.length !== 6 || fields.slice(0, 5).some(x => !/^-?\d+$/.test(x)) || !/^[a-f0-9]{32}$/.test(fields[5])) {
      throw new Error('Unexpected framemd5 record: ' + line)
    }
    const [stream, dts, pts, duration, bytes] = fields.slice(0, 5).map(Number)
    return { stream, dts, pts, duration, bytes, md5: fields[5] }
  })
  if (!timeBase || !size || frames.some(row => row.stream !== 0)) throw new Error('Expected one video stream with explicit timebase and dimensions')
  return { timeBase, dimensions: { width: Number(size[1]), height: Number(size[2]) }, frames }
}

export function compareVideo(reference, candidate) {
  const firstMismatch = reference.frames.findIndex((frame, i) => JSON.stringify(frame) !== JSON.stringify(candidate.frames[i]))
  const identical = reference.timeBase === candidate.timeBase &&
    JSON.stringify(reference.dimensions) === JSON.stringify(candidate.dimensions) &&
    reference.frames.length === candidate.frames.length && firstMismatch < 0
  return {
    identical, referenceFrames: reference.frames.length, candidateFrames: candidate.frames.length,
    referenceTimeBase: reference.timeBase, candidateTimeBase: candidate.timeBase,
    referenceDimensions: reference.dimensions, candidateDimensions: candidate.dimensions,
    referenceFirst: reference.frames[0], candidateFirst: candidate.frames[0],
    referenceLast: reference.frames.at(-1), candidateLast: candidate.frames.at(-1),
    firstMismatch: firstMismatch >= 0 ? firstMismatch : reference.frames.length === candidate.frames.length ? null : reference.frames.length,
  }
}

function samples(bytes) {
  if (bytes.length % (CHANNELS * 4)) throw new Error('Truncated stereo float PCM sample')
  return new Float32Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength))
}

export function compareAudio(referenceBytes, candidateBytes) {
  const a = samples(referenceBytes), b = samples(candidateBytes)
  const frames = Math.min(a.length, b.length) / CHANNELS
  const error = (start, end) => {
    let squared = 0, peak = 0, count = 0
    for (let i = start * CHANNELS; i < end * CHANNELS; i++) {
      const delta = a[i] - b[i]
      squared += delta * delta; peak = Math.max(peak, Math.abs(delta)); count++
    }
    return { rmse: count ? Math.sqrt(squared / count) : null, peak: count ? peak : null }
  }
  const all = error(0, frames)
  // Coarse/fine alignment search over a short, nonperiodic chirp section. Positive lag means
  // the candidate contains extra leading samples; negative lag means it starts too late.
  const maxLag = Math.min(2048, Math.floor(frames / 4))
  const score = (lag) => {
    const begin = Math.max(0, -lag)
    const end = Math.min(frames, frames - lag, begin + 4096)
    let squared = 0, count = 0
    for (let i = begin; i < end; i += 4) {
      const delta = a[i * CHANNELS] - b[(i + lag) * CHANNELS]
      squared += delta * delta; count++
    }
    return count ? squared / count : Infinity
  }
  let lag = 0, best = score(0)
  for (let offset = -maxLag; offset <= maxLag; offset += 8) {
    const value = score(offset)
    if (value < best) { best = value; lag = offset }
  }
  const coarse = lag
  for (let offset = Math.max(-maxLag, coarse - 8); offset <= Math.min(maxLag, coarse + 8); offset++) {
    const value = score(offset)
    if (value < best) { best = value; lag = offset }
  }
  const firstNonSilent = (data) => {
    for (let i = 0; i < data.length; i += CHANNELS) {
      if (Math.max(Math.abs(data[i]), Math.abs(data[i + 1])) > 0.0001) return i / CHANNELS
    }
    return null
  }
  const head = error(0, Math.min(frames, Math.round(RATE * 0.02)))
  const remainder = error(Math.min(frames, Math.round(RATE * 0.02)), frames)
  const tail = error(Math.max(0, frames - Math.round(RATE * 0.02)), frames)
  const referenceFirstNonSilentSample = firstNonSilent(a)
  const candidateFirstNonSilentSample = firstNonSilent(b)
  // A fixed -80 dB noise-floor crossing is not a timestamp: two correct AAC decoders can cross
  // it a few samples apart in encoder priming. Keep that raw measurement above, and additionally
  // detect genuinely introduced silence in 1 ms windows where the reference contains a signal.
  const missingOnsetWindows = []
  let audibleOnsetWindows = 0
  for (let begin = 0; begin < Math.min(frames, RATE * 0.05); begin += RATE / 1000) {
    const end = Math.min(frames, begin + RATE / 1000)
    let aSquared = 0, bSquared = 0
    for (let i = begin * CHANNELS; i < end * CHANNELS; i++) { aSquared += a[i] * a[i]; bSquared += b[i] * b[i] }
    const referenceRms = Math.sqrt(aSquared / ((end - begin) * CHANNELS))
    const candidateRms = Math.sqrt(bSquared / ((end - begin) * CHANNELS))
    if (referenceRms >= 0.01) {
      audibleOnsetWindows++
      if (candidateRms < referenceRms * 0.5) missingOnsetWindows.push({ startSample: begin, referenceRms, candidateRms })
    }
  }
  const timingPassed = frames > 0 && a.length === b.length && lag === 0
  const onsetPassed = missingOnsetWindows.length === 0
  const fidelityPassed = all.rmse !== null && all.rmse < AUDIO_TOLERANCE &&
    head.rmse < AUDIO_ONSET_TOLERANCE && tail.rmse < AUDIO_TOLERANCE &&
    (remainder.rmse === null || remainder.rmse < AUDIO_TOLERANCE) && all.peak < AUDIO_PEAK_TOLERANCE
  return {
    referenceSamples: a.length / CHANNELS, candidateSamples: b.length / CHANNELS,
    rmse: all.rmse, peak: all.peak, first20msRmse: head.rmse, after20msRmse: remainder.rmse, last20msRmse: tail.rmse,
    bestLagSamples: lag, bestLagMs: lag * 1000 / RATE, lagSearchRadiusSamples: maxLag,
    referenceFirstNonSilentSample, candidateFirstNonSilentSample,
    audibleOnsetWindows, missingOnsetWindows,
    timingPassed, onsetPassed, fidelityPassed, passed: timingPassed && onsetPassed && fidelityPassed,
  }
}

const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const call = (exe, args, { probe = false } = {}) => {
  const p = spawnSync(exe, [
    '-hide_banner', '-v', 'error', ...(probe ? [] : ['-nostdin', '-xerror']), ...args,
  ], { timeout: 60000, windowsHide: true, maxBuffer: 64 * 1024 * 1024 })
  return { ok: p.status === 0 && !p.error, status: p.status, error: p.error?.message,
    stderr: p.stderr?.toString().slice(-6000) ?? '', bytes: p.stdout ?? Buffer.alloc(0) }
}
const checked = (result, label) => {
  if (!result.ok) throw new Error(`${label}: ${result.error ?? result.stderr} (exit ${result.status})`)
  return result.bytes
}

function describeSource(reference, file) {
  const probe = join(dirname(reference), 'ffprobe.exe')
  const metadata = JSON.parse(checked(call(probe, ['-show_entries',
    'format=start_time,duration:stream=index,codec_name,width,height,time_base,start_time,duration,nb_frames,has_b_frames,sample_rate,channels:stream_side_data=rotation,displaymatrix',
    '-of', 'json', file], { probe: true }), 'source metadata').toString())
  const frames = JSON.parse(checked(call(probe, ['-select_streams', 'v:0', '-show_frames', '-show_entries',
    'frame=pts_time,pict_type,key_frame', '-of', 'json', file], { probe: true }), 'source video frame timing').toString()).frames
  const deltas = [...new Set(frames.slice(1).map((frame, i) => (Number(frame.pts_time) - Number(frames[i].pts_time)).toFixed(6)))]
  return { ...metadata, frames: frames.length, bFrames: frames.filter(x => x.pict_type === 'B').length,
    keyframeTimes: frames.filter(x => x.key_frame === 1).map(x => Number(x.pts_time)), frameDeltaSeconds: deltas }
}

function generate(reference, root, includeRotation) {
  const left = '0.22*(0.65+0.35*sin(2*PI*3.1*t))*(sin(2*PI*(137*t+17*t*t))+0.25*sin(2*PI*731*t))'
  const right = '0.2*(0.6+0.4*sin(2*PI*2.3*t))*(sin(2*PI*(219*t+11*t*t))+0.3*sin(2*PI*1277*t))'
  const made = []
  for (const vfr of [false, true]) {
    const name = vfr ? 'vfr-bframes' : 'cfr-bframes'
    const file = join(root, name + '.mp4')
    checked(call(reference, [
      '-f', 'lavfi', '-i', 'testsrc2=size=240x136:rate=30:duration=14',
      '-f', 'lavfi', '-i', `aevalsrc=${left}|${right}:sample_rate=${RATE}:duration=14`,
      ...(vfr ? ['-vf', "select='not(mod(n,3))+not(mod(n,5))'"] : []),
      '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'fast',
      '-g', '90', '-keyint_min', '90', '-sc_threshold', '0', '-bf', '3', '-b_strategy', '0',
      '-fps_mode:v', 'vfr', '-video_track_timescale', '90000',
      '-c:a', 'aac', '-b:a', '160k', '-ar', String(RATE), '-ac', String(CHANNELS), '-movflags', '+faststart', file,
    ]), name + ' generation')
    made.push({ name, file, vfr })
  }
  const shifted = join(root, 'offset-bframes.mp4')
  checked(call(reference, ['-copyts', '-i', made[0].file, '-map', '0', '-c', 'copy',
    '-output_ts_offset', '5.375', '-avoid_negative_ts', 'disabled', '-movflags', '+faststart', shifted]), 'nonzero start generation')
  made.push({ name: 'offset-bframes', file: shifted, offset: true })
  if (includeRotation) {
    const file = join(root, 'phone-rotation-90.mp4')
    checked(call(reference, ['-display_rotation:v:0', '90', '-i', made[0].file, '-map', '0', '-c', 'copy',
      '-movflags', '+faststart', file]), 'phone rotation metadata generation')
    made.push({ name: 'phone-rotation-90', file, rotation: true })
  }
  return made
}

function decode(binary, file, { start, duration } = {}, system = false, audio = false) {
  return call(binary, [
    ...(start > 0 ? ['-ss', String(start)] : []),
    ...(system ? ['-c:' + (audio ? 'a' : 'v'), audio ? 'aac_mf' : 'h264_mf'] : []),
    '-i', file, '-map', audio ? '0:a:0' : '0:v:0', '-sn', '-dn',
    ...(duration !== undefined ? ['-t', String(duration)] : []),
    ...(audio ? ['-vn', '-c:a', 'pcm_f32le', '-ar', String(RATE), '-ac', String(CHANNELS), '-f', 'f32le', 'pipe:1'] :
      ['-an', '-c:v', 'rawvideo', '-pix_fmt', 'yuv420p', '-fps_mode:v', 'passthrough', '-enc_time_base:v', 'demux', '-f', 'framemd5', 'pipe:1']),
  ])
}

export function main(referencePath, candidatePath, directory, option) {
  if (!referencePath || !candidatePath || !directory || (option !== undefined && option !== '--rotation')) {
    throw new Error('Usage: node test-cut-timing.mjs reference.exe candidate.exe NEW-output-directory [--rotation]')
  }
  const includeRotation = option === '--rotation'
  const reference = resolve(referencePath), candidate = resolve(candidatePath), root = resolve(directory)
  if (existsSync(root)) throw new Error('Evidence output must be a new directory: ' + root)
  const report = { schemaVersion: 1, createdAt: new Date().toISOString(),
    harnessSha256: hash(readFileSync(fileURLToPath(import.meta.url))),
    reference: { path: reference, sha256: hash(readFileSync(reference)) },
    candidate: { path: candidate, sha256: hash(readFileSync(candidate)) },
    method: 'independent synthetic H264 B-frames + modulated stereo AAC; indexed input seeks, no native candidate fallback',
    audio: { rate: RATE, channels: CHANNELS, format: 'f32le', maxRmse: AUDIO_TOLERANCE,
      maxOnset20msRmse: AUDIO_ONSET_TOLERANCE, maxSampleError: AUDIO_PEAK_TOLERANCE },
    coverage: { rotationRequested: includeRotation,
      rotationNote: includeRotation ? 'Real 90 degree autorotation, pixel order and output dimensions must match.' :
        'Rotation pending: run again with --rotation against the full candidate; the small prototype has no transpose filter.' },
    sources: [], results: [], passed: false }
  mkdirSync(root, { recursive: true })
  try {
    for (const fixture of generate(reference, root, includeRotation)) {
      const source = describeSource(reference, fixture.file)
      if (!source.bFrames) throw new Error('Fixture does not actually contain B-frames: ' + fixture.name)
      if (fixture.vfr && Math.max(...source.frameDeltaSeconds.map(Number)) - Math.min(...source.frameDeltaSeconds.map(Number)) < 0.02) {
        throw new Error('VFR fixture has no materially varying frame interval')
      }
      if (fixture.offset && Number(source.format.start_time) < 5) throw new Error('Shifted fixture lost its nonzero container start')
      if (fixture.rotation && !source.streams.some(stream => stream.side_data_list?.some(data => Math.abs(Number(data.rotation)) === 90))) {
        throw new Error('Phone fixture lost its actual 90 degree display matrix')
      }
      report.sources.push({ name: fixture.name, file: fixture.file, sha256: hash(readFileSync(fixture.file)), ...source })
      const full = parseFrameMd5(checked(decode(reference, fixture.file), 'reference full frame sequence').toString())
      if (fixture.rotation && (full.dimensions.width !== 136 || full.dimensions.height !== 240)) {
        throw new Error('Reference did not rotate phone pixels into portrait orientation')
      }
      const timeline = new Map(full.frames.map((frame, index) => [frame.md5, { index, pts: frame.pts, timeBase: full.timeBase }]))
      for (const cut of CUTS) {
        const row = { fixture: fixture.name, ...cut, seekMode: 'indexed-input-relative-to-container-start' }
        const nativeVideo = decode(reference, fixture.file, cut)
        const windowsVideo = decode(candidate, fixture.file, cut, true)
        const nativeAudio = decode(reference, fixture.file, cut, false, true)
        const windowsAudio = decode(candidate, fixture.file, cut, true, true)
        row.commands = Object.fromEntries(Object.entries({ nativeVideo, windowsVideo, nativeAudio, windowsAudio }).map(([key, result]) =>
          [key, { ok: result.ok, status: result.status, error: result.error, stderr: result.stderr }]))
        if (nativeVideo.ok && windowsVideo.ok) {
          const expected = parseFrameMd5(nativeVideo.bytes.toString())
          const actual = parseFrameMd5(windowsVideo.bytes.toString())
          row.video = { ...compareVideo(expected, actual),
            referenceFirstSourceFrame: timeline.get(expected.frames[0]?.md5) ?? null,
            candidateFirstSourceFrame: timeline.get(actual.frames[0]?.md5) ?? null }
        }
        if (nativeAudio.ok && windowsAudio.ok) row.audio = compareAudio(nativeAudio.bytes, windowsAudio.bytes)
        row.passed = Object.values(row.commands).every(x => x.ok) && row.video?.identical === true && row.audio?.passed === true
        report.results.push(row)
        console.log(JSON.stringify({ fixture: row.fixture, start: row.start, duration: row.duration,
          videoExact: row.video?.identical, frames: row.video?.candidateFrames,
          audioSamples: row.audio?.candidateSamples, audioLag: row.audio?.bestLagSamples,
          audioRmse: row.audio?.rmse, onsetRmse: row.audio?.first20msRmse, passed: row.passed }))
      }
    }
    report.passed = report.results.length === CUTS.length * (includeRotation ? 4 : 3) && report.results.every(row => row.passed)
    report.summary = {
      cuts: report.results.length, videoTimingAndPixelsExact: report.results.filter(row => row.video?.identical).length,
      audioSampleCountAndLagExact: report.results.filter(row => row.audio?.timingPassed).length,
      audioOnsetSignalPresent: report.results.filter(row => row.audio?.onsetPassed).length,
      audioFidelityPassed: report.results.filter(row => row.audio?.fidelityPassed).length,
      maxOverallAudioRmse: Math.max(...report.results.map(row => row.audio?.rmse ?? Infinity)),
      maxFirst20msAudioRmse: Math.max(...report.results.map(row => row.audio?.first20msRmse ?? Infinity)),
      maxAudioSampleError: Math.max(...report.results.map(row => row.audio?.peak ?? Infinity)),
    }
  } catch (error) {
    report.fatalError = error.stack ?? String(error)
  } finally {
    writeFileSync(join(root, 'cut-timing-results.json'), JSON.stringify(report, null, 2) + '\n')
  }
  console.log(JSON.stringify({ passed: report.passed, cases: report.results.length,
    failed: report.results.filter(row => !row.passed).length, fatalError: report.fatalError, evidence: join(root, 'cut-timing-results.json') }))
  if (!report.passed) process.exitCode = 1
  return report
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main(...process.argv.slice(2))
