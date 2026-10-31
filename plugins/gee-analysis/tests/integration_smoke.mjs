import { apply as applyCorridor } from '../../corridor-analysis/index.js'
import { apply as applyRun } from '../../research-run/index.js'

const stateRoot = 'C:/Users/wsdzk/Desktop/CryoWeave-state'
const corridorRoot = `${stateRoot}/corridor`
const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
applyCorridor(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/corridor-analysis/.venv/Scripts/python.exe',
  geeProject: 'ee-my-road', stateRoot: corridorRoot, allowedRoots: ['E:/文章同步'], timeoutMs: 180000, maxHashBytes: 268435456,
})
applyRun(ctx, { allowedRoots: ['E:/文章同步'], stateRoot, timeoutMs: 120000, maxHashBytes: 268435456 })
const exec = { signal: AbortSignal.timeout(110000) }
const call = async (name, args) => {
  const result = await tools.get(name).execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}

const summarized = await call('corridor_analysis', {
  action: 'summarize',
  table: 'state://20261030T154140Z-d0c69c06/ndvi_zonal.csv',
  valueColumn: 'value',
  pixelCountColumn: 'pixel_count',
  groupBy: 'segment',
})
if (summarized.summary.groups !== 3) throw new Error('Unexpected zonal summary groups')
const created = await call('research_run', {
  action: 'create',
  projectRoot: 'E:/文章同步/16-dk2/5-独库植被过渡带',
  pipeline: 'corridor-ndvi-zonal',
  config: { batchId: '20261030T154140Z-d0c69c06', scale: 30, stat: 'mean' },
  inputs: ['config/r1_median.yml'],
  steps: ['zonal_stats'],
})
const runId = created.run.runId
await call('research_run', { action: 'update_status', runId, status: 'running', step: 'zonal_stats' })
await call('research_run', {
  action: 'register_artifacts', runId,
  artifacts: ['state://corridor/20261030T154140Z-d0c69c06/ndvi_zonal.csv'],
  status: 'complete', step: 'zonal_stats',
})
const verified = await call('research_run', { action: 'verify', runId })
if (verified.auditStatus !== 'current') throw new Error('Registered zonal CSV did not verify')
console.log(JSON.stringify({ ok: true, groups: summarized.summary.groups, pixelCountSum: summarized.rows[0].pixelCountSum, runId, auditStatus: verified.auditStatus }))
