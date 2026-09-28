import test from 'node:test'
import assert from 'node:assert/strict'
import { compareAudio, compareVideo, parseFrameMd5 } from './test-cut-timing.mjs'

const frames = '#tb 0: 1/90000\n#dimensions 0: 8x4\n0, 0, 0, 3000, 48, 0123456789abcdef0123456789abcdef\n0, 3000, 3000, 3000, 48, abcdef0123456789abcdef0123456789\n'
test('video evidence rejects changed timing even if the pixel hashes match', () => {
  const source = parseFrameMd5(frames)
  assert.equal(compareVideo(source, parseFrameMd5(frames)).identical, true)
  assert.equal(compareVideo(source, parseFrameMd5(frames.replace('0, 3000, 3000,', '0, 6000, 6000,'))).identical, false)
  assert.equal(compareVideo(source, { ...source, frames: source.frames.slice(1) }).identical, false)
  assert.equal(compareVideo(source, parseFrameMd5(frames.replace('8x4', '4x8'))).identical, false)
  assert.throws(() => parseFrameMd5('not a frame record'), /Unexpected/)
})

function signal(lead = 0, count = 6000) {
  const bytes = Buffer.alloc(count * 8)
  for (let i = lead; i < count; i++) for (let channel = 0; channel < 2; channel++) {
    const x = i - lead
    bytes.writeFloatLE(0.2 * Math.sin(x * 0.023 + x * x * 0.000009 + channel), (i * 2 + channel) * 4)
  }
  return bytes
}
test('audio evidence detects sample shift and missing samples rather than only average loudness', () => {
  const source = signal()
  assert.equal(compareAudio(source, source).passed, true)
  const shifted = compareAudio(source, signal(37))
  assert.equal(shifted.bestLagSamples, 37)
  assert.equal(shifted.passed, false)
  assert.equal(compareAudio(source, source.subarray(0, -8)).passed, false)
  assert.throws(() => compareAudio(source, Buffer.alloc(7)), /Truncated/)
})
test('audio evidence catches short missing onsets even in otherwise identical samples', () => {
  const source = signal(), damaged = Buffer.from(source)
  damaged.fill(0, 0, 400 * 8)
  const result = compareAudio(source, damaged)
  assert.equal(result.passed, false)
  assert.ok(result.first20msRmse > 0.01)
  assert.ok(result.candidateFirstNonSilentSample >= 400)
})
test('AAC startup tolerance does not hide later noise or isolated large sample errors', () => {
  const source = signal(), startup = Buffer.from(source)
  for (let i = 0; i < 960 * 2; i++) startup.writeFloatLE(startup.readFloatLE(i * 4) + 0.00012, i * 4)
  assert.equal(compareAudio(source, startup).passed, true)
  const later = Buffer.from(source)
  for (let i = 960 * 2; i < 6000 * 2; i++) later.writeFloatLE(later.readFloatLE(i * 4) + 0.00015, i * 4)
  assert.equal(compareAudio(source, later).fidelityPassed, false)
  const spike = Buffer.from(source)
  spike.writeFloatLE(spike.readFloatLE(2000 * 8) + 0.003, 2000 * 8)
  assert.equal(compareAudio(source, spike).fidelityPassed, false)
})
