import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { apply } from '../index.js'

const stateRoot = 'C:/Users/wsdzk/Desktop/CryoWeave-state'
const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  allowedRoots: ['E:/文章同步'],
  stateRoot,
  timeoutMs: 120000,
  maxHashBytes: 268435456,
})
const tool = tools.get('research_run')
const exec = { signal: AbortSignal.timeout(110000) }
const call = (args) => tool.execute(args, exec).then(JSON.parse)
const assertOk = (result) => { if (!result.ok) throw new Error(JSON.stringify(result)) }

const created = await call({
  action: 'create',
  projectRoot: 'E:/文章同步/16-dk2/5-独库植被过渡带',
  pipeline: 'validation-gee-ndvi',
  config: { asset: 'projects/ee-my-road/assets/dukuregion', startDate: '2024-06-01', endDate: '2024-10-01', scale: 30, apiKey: 'must-not-persist' },
  inputs: ['config/r1_median.yml'],
  steps: ['analysis'],
})
assertOk(created)
const runId = created.run.runId
if (created.run.config.apiKey !== '[REDACTED]') throw new Error('Sensitive config was not redacted')

const running = await call({ action: 'update_status', runId, status: 'running', step: 'analysis', note: 'Host lifecycle validation' })
assertOk(running)
const runDirectory = join(stateRoot, 'runs', runId)
mkdirSync(runDirectory, { recursive: true })
const artifact = join(runDirectory, 'result.json')
writeFileSync(artifact, JSON.stringify({ meanNDVI: 0.35283914502024716, source: 'validation-only' }), 'utf8')

const registered = await call({ action: 'register_artifacts', runId, artifacts: [`state://runs/${runId}/result.json`], status: 'complete', step: 'analysis' })
assertOk(registered)
const current = await call({ action: 'verify', runId })
assertOk(current)
if (current.auditStatus !== 'current') throw new Error(`Expected current, got ${current.auditStatus}`)

writeFileSync(artifact, JSON.stringify({ meanNDVI: 0.0, source: 'intentional-stale-test' }), 'utf8')
const stale = await call({ action: 'verify', runId })
assertOk(stale)
if (stale.auditStatus !== 'stale') throw new Error(`Expected stale, got ${stale.auditStatus}`)
const listed = await call({ action: 'list', pipeline: 'validation-gee-ndvi', limit: 5 })
assertOk(listed)
console.log(JSON.stringify({ ok: true, runId, lifecycle: 'create>running>complete', current: current.auditStatus, stale: stale.auditStatus, staleCode: stale.findings[0]?.code, redacted: true, listed: listed.total }))
