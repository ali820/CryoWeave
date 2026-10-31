import { existsSync } from 'node:fs'
import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/environmental-indices/.venv/Scripts/python.exe',
  stateRoot: 'C:/Users/wsdzk/Desktop/CryoWeave-state/indices',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 180000,
  maxHashBytes: 268435456,
})
const exec = { signal: AbortSignal.timeout(170000) }
const call = async (args) => {
  const result = await tools.get('environmental_indices').execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}
const common = {
  projectRoot: 'E:/文章同步/16-dk2/1-data/dk_process',
  inputs: { ndvi: 'NDVI/Q1/copyNDVI_2000_Q1.tif', lst: 'LST/Q1/copyLST_2000_Q1_Celsius.tif' },
}
const inspected = await call({ action: 'inspect', ...common })
if (inspected.sources.length !== 2) throw new Error('Expected two raster sources')
const result = await call({ action: 'calculate', ...common, indices: ['fvc', 'lst', 'tvdi'], ndviBins: 10, minBinPoints: 50 })
if (result.validPixels !== 12141 || result.tvdiFit.pointsUsed < 8000) throw new Error('Unexpected valid pixel or fit point count')
if (!existsSync(result.paths.raster) || !existsSync(result.paths.fit) || !existsSync(result.paths.manifest)) throw new Error('Missing index output')
console.log(JSON.stringify({ ok: true, batchId: result.batchId, validPixels: result.validPixels, validFraction: result.validFraction, fitPoints: result.tvdiFit.pointsUsed, dryEdge: result.tvdiFit.dryEdge, wetEdge: result.tvdiFit.wetEdge, stats: result.stats, statePaths: result.statePaths }))
