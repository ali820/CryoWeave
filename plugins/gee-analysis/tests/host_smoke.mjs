import { existsSync } from 'node:fs'
import { apply } from '../index.js'

const tools = new Map()
const ctx = {
  effect: (callback) => callback(),
  tools: { register: (definition) => { tools.set(definition.name, definition); return () => tools.delete(definition.name) } },
}
apply(ctx, {
  pythonPath: 'C:/Users/wsdzk/Desktop/CryoWeave/plugins/gee-analysis/.venv/Scripts/python.exe',
  project: 'ee-my-road',
  stateRoot: 'C:/Users/wsdzk/Desktop/CryoWeave-state/corridor',
  allowedRoots: ['E:/文章同步'],
  timeoutMs: 240000,
})
const exec = { signal: AbortSignal.timeout(230000) }
const call = async (name, args) => {
  const result = await tools.get(name).execute(args, exec)
  const parsed = JSON.parse(result)
  if (!parsed.ok) throw new Error(result)
  return parsed
}

const status = await call('gee_status', {})
const result = await call('gee_corridor_analyze', {
  table: 'state://20261030T152218Z-fb82dc74/units.geojson',
  unitIds: ['seg_0001', 'seg_0002', 'seg_0003'],
  ringIds: ['ring_01'],
  startDate: '2024-06-01',
  endDate: '2024-09-01',
  imageLimit: 10,
  scale: 30,
  stat: 'mean',
})
if (result.summary.outputRows !== 3) throw new Error(`Expected 3 zonal rows, got ${result.summary.outputRows}`)
if (!existsSync(result.paths.csv) || !existsSync(result.paths.manifest)) throw new Error('Missing zonal output files')
console.log(JSON.stringify({ status: status.status, batchId: result.batchId, imageCount: result.summary.imageCount, outputRows: result.summary.outputRows, values: result.rows.map((row) => ({ segment: row.segment_id, ring: row.ring_id, ndvi: row.value, pixels: row.pixel_count })), statePaths: result.statePaths }))
