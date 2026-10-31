import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/cryo-research-plugins/plugins/physics-scenarios/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Documents/deepseek-harness/default-workspace/.cryo-state/physics',
  timeoutMs: 120000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(110000) }
const call = async (args) => {
  const result = await tools.get('physics_scenarios').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const thermal = await call({ action: 'thermal', conductivityWmK: 2.0, densityKgM3: 2400, heatCapacityJkgK: 960, meanSurfaceC: 14.2, amplitudeC: 12, periodDays: 1, depthsM: [0, 0.05, 0.1, 0.2], timesDays: [0, 0.25, 0.5, 0.75, 1] })
if (Math.abs(thermal.summary.diffusivityM2PerDay - 0.075) > 0.000001 || thermal.summary.rowCount !== 20) throw new Error('Unexpected thermal scenario')
const ensemble = await call({ action: 'ensemble', scenario: 'thermal', baseParameters: { depthM: 0.2, timeDays: 0.5 }, parameterRanges: { conductivityWmK: [1.6, 2.4], amplitudeC: [10, 14] }, samples: 100, randomState: 42 })
if (ensemble.summary.samples !== 100 || !ensemble.summary.rangeCheck || ensemble.summary.temperatureQuantilesC.p95 <= ensemble.summary.temperatureQuantilesC.p05) throw new Error('Unexpected ensemble result')
console.log(JSON.stringify({ ok: true, thermalBatch: thermal.batchId, diffusivityM2PerDay: thermal.summary.diffusivityM2PerDay, dampingDepthM: thermal.summary.dampingDepthM, ensembleBatch: ensemble.batchId, quantiles: ensemble.summary.temperatureQuantilesC }))
