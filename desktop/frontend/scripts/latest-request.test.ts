import assert from 'node:assert/strict'
import { LatestRequest } from '../src/shared/latestRequest.ts'

const gate = new LatestRequest()
const oldRequest = gate.begin()
const newRequest = gate.begin()

assert.equal(gate.isLatest(oldRequest), false)
assert.equal(gate.isLatest(newRequest), true)

console.log('Latest request race tests passed')
